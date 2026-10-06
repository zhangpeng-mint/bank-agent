from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bankdev_agent.config import ProjectConfig, RepositoryConfig
from bankdev_agent.errors import GoldenVerificationError, ScopeViolation
from bankdev_agent.git_snapshot import GitSnapshotInspector, SnapshotReport


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class GoldenReport:
    case_id: str
    checks: tuple[CheckResult, ...]
    snapshots: tuple[SnapshotReport, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks) and all(
            snapshot.ok for snapshot in self.snapshots
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "ok": self.ok,
            "checks": [asdict(check) for check in self.checks],
            "snapshots": [snapshot.to_dict() for snapshot in self.snapshots],
        }


class GoldenVerifier:
    def __init__(self, config: ProjectConfig) -> None:
        self.config = config
        self.git = GitSnapshotInspector()

    def verify(self, case_path: str | Path) -> GoldenReport:
        case = self._load_case(case_path)
        checks: list[CheckResult] = []
        snapshots: list[SnapshotReport] = []

        case_id = self._required_string(case, "case_id", "case")
        snapshot_expectations = self._required_dict(case, "snapshots", "case")
        for repo_id, expected_commit in snapshot_expectations.items():
            if not isinstance(repo_id, str) or not isinstance(expected_commit, str):
                raise GoldenVerificationError("case.snapshots must map strings to commit strings")
            repository = self.config.repository(repo_id)
            report = self.git.inspect(repository)
            snapshots.append(report)
            checks.append(
                CheckResult(
                    name=f"snapshot:{repo_id}",
                    ok=report.ok and report.head == expected_commit,
                    detail=(
                        f"HEAD={report.head}; expected={expected_commit}; "
                        f"changes={[change.path for change in report.changes]}; "
                        f"mismatches={list(report.mismatches)}"
                    ),
                )
            )

        evidence_items = self._required_list(case, "expected_nodes", "case")
        evidence_items += self._required_list(case, "knowledge_evidence", "case")
        node_ids: set[str] = set()
        evidence_paths: dict[str, set[str]] = {}
        modified_evidence_paths: dict[str, set[str]] = {}
        for index, item in enumerate(evidence_items):
            if not isinstance(item, dict):
                raise GoldenVerificationError(f"Evidence item {index} must be an object")
            if "id" in item:
                node_id = self._required_string(item, "id", f"evidence[{index}]")
                if node_id in node_ids:
                    raise GoldenVerificationError(f"Duplicate expected node id: {node_id}")
                node_ids.add(node_id)
            result = self._verify_evidence(item, index)
            checks.append(result)
            repo_id = self._required_string(item, "repo_id", f"evidence[{index}]")
            relative_path = self._required_string(item, "path", f"evidence[{index}]")
            evidence_paths.setdefault(repo_id, set()).add(relative_path)
            worktree_state = item.get("worktree_state", "committed")
            if worktree_state not in {"committed", "modified_expected"}:
                raise GoldenVerificationError(
                    f"evidence[{index}].worktree_state must be committed or modified_expected"
                )
            if worktree_state == "modified_expected":
                modified_evidence_paths.setdefault(repo_id, set()).add(relative_path)

        for index, edge in enumerate(self._required_list(case, "expected_confirmed_edges", "case")):
            if not isinstance(edge, dict):
                raise GoldenVerificationError(f"Edge {index} must be an object")
            source = self._required_string(edge, "from", f"edge[{index}]")
            target = self._required_string(edge, "to", f"edge[{index}]")
            classification = self._required_string(edge, "classification", f"edge[{index}]")
            ok = source in node_ids and target in node_ids and classification == "confirmed_static"
            checks.append(
                CheckResult(
                    name=f"edge:{source}->{target}",
                    ok=ok,
                    detail="edge endpoints exist and classification is confirmed_static" if ok else "invalid edge",
                )
            )

        mapper_paths = self._required_list(case, "expected_mapper_evidence", "case")
        mapper_text = []
        dfbm = self.config.repository("dfbm")
        for relative_path in mapper_paths:
            if not isinstance(relative_path, str):
                raise GoldenVerificationError("expected_mapper_evidence entries must be strings")
            try:
                path = dfbm.resolve_path(relative_path, expected_kind="file")
                mapper_text.append(path.read_text(encoding="utf-8"))
                checks.append(CheckResult(f"mapper:{relative_path}", True, "exists and is in scope"))
            except (OSError, UnicodeError, ScopeViolation) as exc:
                checks.append(CheckResult(f"mapper:{relative_path}", False, str(exc)))

        combined_mapper_text = "\n".join(mapper_text)
        for table in self._required_list(case, "expected_tables", "case"):
            if not isinstance(table, str):
                raise GoldenVerificationError("expected_tables entries must be strings")
            present = table in combined_mapper_text
            checks.append(
                CheckResult(
                    name=f"table:{table}",
                    ok=present,
                    detail="found in mapper SQL" if present else "not found in mapper SQL",
                )
            )

        for index, absence in enumerate(self._required_list(case, "scoped_absences", "case")):
            if not isinstance(absence, dict):
                raise GoldenVerificationError(f"scoped_absences[{index}] must be an object")
            checks.append(self._verify_scoped_absence(absence, index))

        for index, conflict in enumerate(self._required_list(case, "known_conflicts", "case")):
            if not isinstance(conflict, dict):
                raise GoldenVerificationError(f"known_conflicts[{index}] must be an object")
            field = self._required_string(conflict, "field", f"known_conflicts[{index}]")
            knowledge_value = self._required_string(
                conflict, "knowledge_value", f"known_conflicts[{index}]"
            )
            current_value = self._required_string(
                conflict, "current_code_value", f"known_conflicts[{index}]"
            )
            classification = self._required_string(
                conflict, "classification", f"known_conflicts[{index}]"
            )
            required_report = conflict.get("required_report") is True
            ok = (
                knowledge_value != current_value
                and classification == "document_code_conflict"
                and required_report
            )
            checks.append(
                CheckResult(
                    name=f"known_conflict:{field}",
                    ok=ok,
                    detail=(
                        f"classification={classification}; required_report={required_report}; "
                        f"knowledge={knowledge_value}; current={current_value}"
                    ),
                )
            )

        for index, conflict in enumerate(case.get("resolved_conflicts", [])):
            if not isinstance(conflict, dict):
                raise GoldenVerificationError(f"resolved_conflicts[{index}] must be an object")
            field = self._required_string(conflict, "field", f"resolved_conflicts[{index}]")
            knowledge_value = self._required_string(
                conflict, "knowledge_value", f"resolved_conflicts[{index}]"
            )
            current_value = self._required_string(
                conflict, "current_code_value", f"resolved_conflicts[{index}]"
            )
            classification = self._required_string(
                conflict, "classification", f"resolved_conflicts[{index}]"
            )
            ok = knowledge_value == current_value and classification == "resolved_by_current_code"
            checks.append(
                CheckResult(
                    name=f"resolved_conflict:{field}",
                    ok=ok,
                    detail=(
                        f"classification={classification}; knowledge={knowledge_value}; "
                        f"current={current_value}"
                    ),
                )
            )

        self._check_evidence_worktree_state(
            snapshots,
            evidence_paths,
            modified_evidence_paths,
            checks,
        )
        return GoldenReport(case_id=case_id, checks=tuple(checks), snapshots=tuple(snapshots))

    def _verify_evidence(self, item: dict[str, Any], index: int) -> CheckResult:
        context = f"evidence[{index}]"
        repo_id = self._required_string(item, "repo_id", context)
        relative_path = self._required_string(item, "path", context)
        expected_hash = self._required_string(item, "sha256", context)
        start_line = self._required_int(item, "start_line", context)
        end_line = self._required_int(item, "end_line", context)
        must_contain = item.get("must_contain", [])
        if not isinstance(must_contain, list) or not all(isinstance(value, str) for value in must_contain):
            raise GoldenVerificationError(f"{context}.must_contain must be a string list")

        try:
            repository = self.config.repository(repo_id)
            path = repository.resolve_path(relative_path, expected_kind="file")
            content = path.read_bytes()
            actual_hash = hashlib.sha256(content).hexdigest()
            text = content.decode("utf-8")
            lines = text.splitlines()
            if not (1 <= start_line <= end_line <= len(lines)):
                return CheckResult(
                    name=f"evidence:{repo_id}:{relative_path}",
                    ok=False,
                    detail=f"line range {start_line}-{end_line} outside 1-{len(lines)}",
                )
            excerpt = "\n".join(lines[start_line - 1 : end_line])
            missing = [needle for needle in must_contain if needle not in excerpt]
            ok = actual_hash == expected_hash and not missing
            return CheckResult(
                name=f"evidence:{repo_id}:{relative_path}",
                ok=ok,
                detail=f"sha256={actual_hash}; missing_markers={missing}",
            )
        except (OSError, UnicodeError, ScopeViolation) as exc:
            return CheckResult(f"evidence:{repo_id}:{relative_path}", False, str(exc))

    def _verify_scoped_absence(self, item: dict[str, Any], index: int) -> CheckResult:
        context = f"scoped_absences[{index}]"
        repo_id = self._required_string(item, "repo_id", context)
        relative_root = self._required_string(item, "relative_root", context)
        suffixes = self._required_string_list(item, "file_suffixes", context)
        literals = self._required_string_list(item, "forbidden_literals", context)
        repository = self.config.repository(repo_id)

        try:
            root = repository.resolve_path(
                relative_root,
                enforce_patterns=False,
                expected_kind="directory",
            )
            hits: list[str] = []
            files_scanned = 0
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix not in suffixes:
                    continue
                relative_path = repository.relative_name(path)
                if not repository.is_included(relative_path):
                    continue
                repository.resolve_path(relative_path, expected_kind="file")
                files_scanned += 1
                text = path.read_text(encoding="utf-8", errors="replace")
                for literal in literals:
                    if literal in text:
                        hits.append(f"{relative_path}:{literal}")
                        if len(hits) >= 20:
                            break
                if len(hits) >= 20:
                    break
            return CheckResult(
                name=f"scoped_absence:{repo_id}:{relative_root}",
                ok=not hits,
                detail=f"files_scanned={files_scanned}; hits={hits}",
            )
        except (OSError, ScopeViolation) as exc:
            return CheckResult(f"scoped_absence:{repo_id}:{relative_root}", False, str(exc))

    def _check_evidence_worktree_state(
        self,
        snapshots: list[SnapshotReport],
        evidence_paths: dict[str, set[str]],
        modified_evidence_paths: dict[str, set[str]],
        checks: list[CheckResult],
    ) -> None:
        for snapshot in snapshots:
            changed = {change.path for change in snapshot.changes}
            overlap = changed & evidence_paths.get(snapshot.repo_id, set())
            allowed = modified_evidence_paths.get(snapshot.repo_id, set())
            unexpected_modified = overlap - allowed
            missing_modified = allowed - changed
            checks.append(
                CheckResult(
                    name=f"evidence_worktree:{snapshot.repo_id}",
                    ok=not unexpected_modified and not missing_modified,
                    detail=(
                        f"expected modified evidence={sorted(allowed)}; "
                        f"unexpected modified evidence={sorted(unexpected_modified)}; "
                        f"missing expected modifications={sorted(missing_modified)}"
                    ),
                )
            )

    @staticmethod
    def _load_case(case_path: str | Path) -> dict[str, Any]:
        path = Path(case_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise GoldenVerificationError(f"Golden case is not a file: {path}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise GoldenVerificationError(f"Cannot load golden case {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise GoldenVerificationError("Golden case root must be an object")
        if value.get("schema_version") != "1.0":
            raise GoldenVerificationError(f"Unsupported golden schema: {value.get('schema_version')!r}")
        return value

    @staticmethod
    def _required_string(data: dict[str, Any], key: str, context: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise GoldenVerificationError(f"{context}.{key} must be a non-empty string")
        return value

    @staticmethod
    def _required_int(data: dict[str, Any], key: str, context: str) -> int:
        value = data.get(key)
        if not isinstance(value, int):
            raise GoldenVerificationError(f"{context}.{key} must be an integer")
        return value

    @staticmethod
    def _required_list(data: dict[str, Any], key: str, context: str) -> list[Any]:
        value = data.get(key)
        if not isinstance(value, list):
            raise GoldenVerificationError(f"{context}.{key} must be a list")
        return value.copy()

    @staticmethod
    def _required_dict(data: dict[str, Any], key: str, context: str) -> dict[str, Any]:
        value = data.get(key)
        if not isinstance(value, dict):
            raise GoldenVerificationError(f"{context}.{key} must be an object")
        return value

    @staticmethod
    def _required_string_list(data: dict[str, Any], key: str, context: str) -> tuple[str, ...]:
        value = data.get(key)
        if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
            raise GoldenVerificationError(f"{context}.{key} must be a non-empty string list")
        return tuple(value)
