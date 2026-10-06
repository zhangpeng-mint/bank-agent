from __future__ import annotations

import fnmatch
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from bankdev_agent.errors import ConfigurationError, ScopeViolation


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_REQUIRED_DENIED_OPERATIONS = {
    "write",
    "delete",
    "execute_project_code",
    "connect_database",
    "call_project_service",
    "push",
}
_ALLOWED_READ_OPERATIONS = {"stat", "read", "search", "git_read"}


def _require(data: dict[str, Any], key: str, expected_type: type, context: str) -> Any:
    if key not in data:
        raise ConfigurationError(f"Missing {context}.{key}")
    value = data[key]
    if not isinstance(value, expected_type):
        raise ConfigurationError(
            f"{context}.{key} must be {expected_type.__name__}, got {type(value).__name__}"
        )
    return value


def _string_tuple(data: dict[str, Any], key: str, context: str) -> tuple[str, ...]:
    value = _require(data, key, list, context)
    if not all(isinstance(item, str) and item for item in value):
        raise ConfigurationError(f"{context}.{key} must contain non-empty strings")
    return tuple(value)


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


@dataclass(frozen=True)
class SafetyConfig:
    allowed_operations: tuple[str, ...]
    denied_operations: tuple[str, ...]
    secrets_policy: str


@dataclass(frozen=True)
class RepositoryConfig:
    id: str
    display_name: str
    role: str
    kind: str
    root: Path
    origin: str
    baseline_branch: str
    baseline_commit: str
    upstream: str
    worktree_policy: str
    preexisting_changes: tuple[str, ...]
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    content_root: Path | None = None
    demo_paths: tuple[str, ...] = ()
    demo_authority: str | None = None
    query_command: tuple[str, ...] = ()
    follow_symlinks: bool = False

    @property
    def resolved_root(self) -> Path:
        return self.root.resolve(strict=True)

    def relative_name(self, path: Path) -> str:
        try:
            return path.relative_to(self.resolved_root).as_posix()
        except ValueError as exc:
            raise ScopeViolation(f"Path is outside repository {self.id}: {path}") from exc

    def is_included(self, relative_path: str) -> bool:
        normalized = PurePosixPath(relative_path).as_posix()
        return _matches(normalized, self.include) and not _matches(normalized, self.exclude)

    def is_demo(self, relative_path: str) -> bool:
        return _matches(PurePosixPath(relative_path).as_posix(), self.demo_paths)

    def resolve_path(
        self,
        relative_path: str,
        *,
        enforce_patterns: bool = True,
        expected_kind: str | None = None,
    ) -> Path:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ScopeViolation("Path must be a non-empty relative string")

        pure = PurePosixPath(relative_path)
        if pure.is_absolute() or ".." in pure.parts:
            raise ScopeViolation(f"Absolute paths and parent traversal are forbidden: {relative_path}")

        normalized = pure.as_posix()
        if normalized in {".", ""}:
            candidate = self.root
        else:
            candidate = self.root.joinpath(*pure.parts)

        if not candidate.exists():
            raise ScopeViolation(f"Path does not exist in repository {self.id}: {relative_path}")

        self._reject_symlink_components(candidate)
        resolved = candidate.resolve(strict=True)
        root = self.resolved_root
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ScopeViolation(f"Resolved path escapes repository {self.id}: {relative_path}") from exc

        if enforce_patterns and not self.is_included(normalized):
            raise ScopeViolation(f"Path is outside configured include/exclude rules: {self.id}:{normalized}")

        if expected_kind == "file" and not resolved.is_file():
            raise ScopeViolation(f"Expected file: {self.id}:{normalized}")
        if expected_kind == "directory" and not resolved.is_dir():
            raise ScopeViolation(f"Expected directory: {self.id}:{normalized}")
        return resolved

    def _reject_symlink_components(self, candidate: Path) -> None:
        if self.follow_symlinks:
            return
        root = self.root
        if root.is_symlink():
            raise ScopeViolation(f"Repository root may not be a symlink: {root}")
        current = root
        relative = candidate.relative_to(root)
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ScopeViolation(f"Symlink traversal is forbidden: {current}")


@dataclass(frozen=True)
class ProjectConfig:
    source_path: Path
    schema_version: int
    access_mode: str
    allow_arbitrary_paths: bool
    follow_symlinks: bool
    index_untracked_files: bool
    safety: SafetyConfig
    repositories: tuple[RepositoryConfig, ...]

    def repository(self, repo_id: str) -> RepositoryConfig:
        for repository in self.repositories:
            if repository.id == repo_id:
                return repository
        raise ConfigurationError(f"Unknown repository id: {repo_id}")


def load_project_config(path: str | Path) -> ProjectConfig:
    source_path = Path(path).expanduser().resolve(strict=True)
    if not source_path.is_file():
        raise ConfigurationError(f"Configuration is not a file: {source_path}")

    try:
        with source_path.open("rb") as stream:
            raw = tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"Invalid TOML in {source_path}: {exc}") from exc

    schema_version = _require(raw, "schema_version", int, "config")
    access_mode = _require(raw, "access_mode", str, "config")
    allow_arbitrary_paths = _require(raw, "allow_arbitrary_paths", bool, "config")
    follow_symlinks = _require(raw, "follow_symlinks", bool, "config")
    index_untracked_files = _require(raw, "index_untracked_files", bool, "config")

    if schema_version != 1:
        raise ConfigurationError(f"Unsupported schema_version: {schema_version}")
    if access_mode != "read_only":
        raise ConfigurationError("Only access_mode='read_only' is accepted")
    if allow_arbitrary_paths:
        raise ConfigurationError("allow_arbitrary_paths must remain false")
    if follow_symlinks:
        raise ConfigurationError("follow_symlinks must remain false")
    if index_untracked_files:
        raise ConfigurationError("index_untracked_files must remain false in phase 1")

    safety_raw = _require(raw, "safety", dict, "config")
    safety = SafetyConfig(
        allowed_operations=_string_tuple(safety_raw, "allowed_operations", "safety"),
        denied_operations=_string_tuple(safety_raw, "denied_operations", "safety"),
        secrets_policy=_require(safety_raw, "secrets_policy", str, "safety"),
    )
    if not set(safety.allowed_operations).issubset(_ALLOWED_READ_OPERATIONS):
        raise ConfigurationError("safety.allowed_operations contains a non-read operation")
    missing_denials = _REQUIRED_DENIED_OPERATIONS - set(safety.denied_operations)
    if missing_denials:
        raise ConfigurationError(f"Missing required denied operations: {sorted(missing_denials)}")

    repositories_raw = _require(raw, "repositories", list, "config")
    repositories = tuple(
        _load_repository(item, index, follow_symlinks)
        for index, item in enumerate(repositories_raw)
    )
    ids = [repository.id for repository in repositories]
    if len(ids) != len(set(ids)):
        raise ConfigurationError("Repository ids must be unique")
    if not repositories:
        raise ConfigurationError("At least one repository must be configured")

    return ProjectConfig(
        source_path=source_path,
        schema_version=schema_version,
        access_mode=access_mode,
        allow_arbitrary_paths=allow_arbitrary_paths,
        follow_symlinks=follow_symlinks,
        index_untracked_files=index_untracked_files,
        safety=safety,
        repositories=repositories,
    )


def _load_repository(raw: Any, index: int, follow_symlinks: bool) -> RepositoryConfig:
    context = f"repositories[{index}]"
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{context} must be a table")

    root_text = _require(raw, "root", str, context)
    root = Path(root_text).expanduser()
    if not root.is_absolute():
        raise ConfigurationError(f"{context}.root must be absolute")
    if not root.exists() or not root.is_dir():
        raise ConfigurationError(f"{context}.root is not an existing directory: {root}")
    if root.is_symlink() and not follow_symlinks:
        raise ConfigurationError(f"{context}.root may not be a symlink")

    baseline_commit = _require(raw, "baseline_commit", str, context)
    if not _COMMIT_RE.fullmatch(baseline_commit):
        raise ConfigurationError(f"{context}.baseline_commit must be a 40-character lowercase SHA")

    content_root: Path | None = None
    if "content_root" in raw:
        content_root = Path(_require(raw, "content_root", str, context)).expanduser()
        if not content_root.is_absolute() or not content_root.is_dir():
            raise ConfigurationError(f"{context}.content_root must be an existing absolute directory")
        try:
            content_root.resolve(strict=True).relative_to(root.resolve(strict=True))
        except ValueError as exc:
            raise ConfigurationError(f"{context}.content_root must be inside root") from exc

    return RepositoryConfig(
        id=_require(raw, "id", str, context),
        display_name=_require(raw, "display_name", str, context),
        role=_require(raw, "role", str, context),
        kind=_require(raw, "kind", str, context),
        root=root,
        origin=_require(raw, "origin", str, context),
        baseline_branch=_require(raw, "baseline_branch", str, context),
        baseline_commit=baseline_commit,
        upstream=_require(raw, "upstream", str, context),
        worktree_policy=_require(raw, "worktree_policy", str, context),
        preexisting_changes=_string_tuple(raw, "preexisting_changes", context),
        include=_string_tuple(raw, "include", context),
        exclude=_string_tuple(raw, "exclude", context),
        content_root=content_root,
        demo_paths=tuple(raw.get("demo_paths", [])),
        demo_authority=raw.get("demo_authority"),
        query_command=tuple(raw.get("query_command", [])),
        follow_symlinks=follow_symlinks,
    )

