from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass

from bankdev_agent.config import RepositoryConfig
from bankdev_agent.errors import GitInspectionError


@dataclass(frozen=True)
class WorktreeChange:
    status: str
    path: str


@dataclass(frozen=True)
class SnapshotReport:
    repo_id: str
    root: str
    branch: str
    head: str
    origin: str
    upstream: str | None
    ahead: int | None
    behind: int | None
    changes: tuple[WorktreeChange, ...]
    expected_changes: tuple[str, ...]
    unexpected_changes: tuple[str, ...]
    missing_expected_changes: tuple[str, ...]
    mismatches: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not self.changes

    @property
    def ok(self) -> bool:
        return (
            not self.mismatches
            and not self.unexpected_changes
            and not self.missing_expected_changes
        )

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["clean"] = self.clean
        payload["ok"] = self.ok
        return payload


class GitSnapshotInspector:
    """Read-only Git inspection using fixed argument lists and no shell."""

    def inspect(self, repository: RepositoryConfig) -> SnapshotReport:
        self._ensure_git_repository(repository)
        head = self._git(repository, "rev-parse", "HEAD")
        branch = self._git(repository, "branch", "--show-current")
        origin = self._git(repository, "remote", "get-url", "origin")
        upstream = self._git_optional(repository, "rev-parse", "--abbrev-ref", "@{upstream}")
        ahead, behind = self._divergence(repository, upstream)
        changes = self._changes(repository)

        actual_paths = {change.path for change in changes}
        expected_paths = set(repository.preexisting_changes)
        unexpected = tuple(sorted(actual_paths - expected_paths))
        missing = tuple(sorted(expected_paths - actual_paths))

        mismatches: list[str] = []
        if head != repository.baseline_commit:
            mismatches.append(f"HEAD expected {repository.baseline_commit}, got {head}")
        if branch != repository.baseline_branch:
            mismatches.append(f"branch expected {repository.baseline_branch}, got {branch}")
        if origin != repository.origin:
            mismatches.append(f"origin expected {repository.origin}, got {origin}")
        if upstream != repository.upstream:
            mismatches.append(f"upstream expected {repository.upstream}, got {upstream}")
        if ahead not in (None, 0) or behind not in (None, 0):
            mismatches.append(f"upstream divergence expected 0/0, got ahead={ahead}, behind={behind}")

        return SnapshotReport(
            repo_id=repository.id,
            root=str(repository.resolved_root),
            branch=branch,
            head=head,
            origin=origin,
            upstream=upstream,
            ahead=ahead,
            behind=behind,
            changes=changes,
            expected_changes=tuple(sorted(expected_paths)),
            unexpected_changes=unexpected,
            missing_expected_changes=missing,
            mismatches=tuple(mismatches),
        )

    def _ensure_git_repository(self, repository: RepositoryConfig) -> None:
        inside = self._git(repository, "rev-parse", "--is-inside-work-tree")
        if inside != "true":
            raise GitInspectionError(f"Not a Git worktree: {repository.root}")

    def _divergence(
        self, repository: RepositoryConfig, upstream: str | None
    ) -> tuple[int | None, int | None]:
        if upstream is None:
            return None, None
        value = self._git(repository, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        try:
            ahead_text, behind_text = value.split()
            return int(ahead_text), int(behind_text)
        except (ValueError, TypeError) as exc:
            raise GitInspectionError(
                f"Unexpected divergence result for {repository.id}: {value!r}"
            ) from exc

    def _changes(self, repository: RepositoryConfig) -> tuple[WorktreeChange, ...]:
        output = self._git(repository, "status", "--porcelain=v1", "--untracked-files=all")
        changes: list[WorktreeChange] = []
        for line in output.splitlines():
            if len(line) < 4:
                raise GitInspectionError(f"Unexpected git status line for {repository.id}: {line!r}")
            status = line[:2]
            path = line[3:]
            if " -> " in path:
                path = path.rsplit(" -> ", 1)[1]
            if path.startswith('"') and path.endswith('"'):
                raise GitInspectionError(
                    f"Quoted Git path is not supported safely in phase 1: {repository.id}:{path}"
                )
            changes.append(WorktreeChange(status=status, path=path))
        return tuple(changes)

    def _git(self, repository: RepositoryConfig, *args: str) -> str:
        command = [
            "git",
            "-c",
            "core.quotePath=false",
            "-C",
            str(repository.resolved_root),
            *args,
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitInspectionError(f"Git inspection failed for {repository.id}: {exc}") from exc
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "unknown git error"
            raise GitInspectionError(f"Git inspection failed for {repository.id}: {message}")
        # Preserve leading spaces: porcelain v1 uses them as part of the XY status.
        # Removing only line terminators is safe for scalar Git queries as well.
        return result.stdout.rstrip("\r\n")

    def _git_optional(self, repository: RepositoryConfig, *args: str) -> str | None:
        try:
            return self._git(repository, *args)
        except GitInspectionError:
            return None
