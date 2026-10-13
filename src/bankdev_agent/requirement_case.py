"""Replay reviewed requirement inputs against explicitly pinned repository versions."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import fields, replace
from pathlib import Path

from bankdev_agent.code_index import CodeIndexer
from bankdev_agent.errors import BankDevError
from bankdev_agent.knowledge_index import KnowledgeIndexer
from bankdev_agent.requirement_analysis import RequirementAnalyzer, RequirementFrame, render_requirement_markdown


class RequirementCaseError(BankDevError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RequirementCaseError(f"Duplicate case field: {key}")
        result[key] = value
    return result


def load_case(path: Path, config):
    if path.stat().st_size > 64000:
        raise RequirementCaseError("Requirement case exceeds 64KB")
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as exc:
        raise RequirementCaseError("Invalid requirement case JSON") from exc
    if not isinstance(data, dict) or set(data) != {"schema_version", "case_id", "snapshots", "requirement"}:
        raise RequirementCaseError("Unexpected requirement case fields")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise RequirementCaseError("Unsupported requirement case version")
    if not isinstance(data["case_id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", data["case_id"]):
        raise RequirementCaseError("Invalid case id")
    snapshots = data["snapshots"]
    if not isinstance(snapshots, dict) or set(snapshots) != {r.id for r in config.repositories}:
        raise RequirementCaseError("Case must pin every configured repository")
    if any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{40}", v) for v in snapshots.values()):
        raise RequirementCaseError("Snapshots must be full commit hashes")
    frame = data["requirement"]
    allowed = {f.name for f in fields(RequirementFrame)}
    if not isinstance(frame, dict) or set(frame) - allowed or "goal" not in frame:
        raise RequirementCaseError("Invalid requirement fields")
    for name, value in frame.items():
        if name == "goal":
            valid = isinstance(value, str) and 0 < len(value.strip()) <= 8000
        else:
            valid = (isinstance(value, list) and len(value) <= 40
                     and all(isinstance(v, str) and 0 < len(v.strip()) <= 4000 for v in value))
        if not valid:
            raise RequirementCaseError(f"Invalid requirement field: {name}")
    requirement = RequirementFrame.parse(frame["goal"], **{k: v for k, v in frame.items() if k != "goal"})
    # Preserve roots, allowlists, dirty-worktree expectations and original config.
    scoped = replace(config, repositories=tuple(
        replace(r, baseline_commit=snapshots[r.id]) for r in config.repositories))
    return data, requirement, scoped


def analyze_case(config, path: Path, database: Path | None = None, *, build_index=False,
                 max_depth=8, max_nodes=200):
    data, frame, scoped = load_case(path, config)
    default_db = config.source_path.parent.parent / "var" / "cases" / data["case_id"] / "index.sqlite3"
    database = (database or default_db).resolve()
    analyzer = RequirementAnalyzer(scoped, database)  # Validate var boundary before writes.
    original_db = (config.source_path.parent.parent / "var" / "bankdev.sqlite3").resolve()
    if database == original_db:
        raise RequirementCaseError("Cases must use a separate index, not var/bankdev.sqlite3")
    if not 0 <= max_depth <= 32 or not 1 <= max_nodes <= 2000:
        raise RequirementCaseError("Invalid graph budget")
    indexing = {}
    if build_index:
        indexing["code"] = CodeIndexer(scoped, database).index()
        indexing["knowledge"] = [KnowledgeIndexer(scoped, database).index(r.id)
                                  for r in scoped.repositories if r.kind == "git_markdown"]
    report = analyzer.analyze(frame, max_depth, max_nodes)
    report["case"] = {"id": data["case_id"], "input_sha256": hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "configured_baselines": {r.id: r.baseline_commit for r in config.repositories},
        "requested_snapshots": data["snapshots"], "indexing": indexing}
    # KnowledgeIndexer follows live controlled documents. Never silently claim they
    # are the pinned commit when the live revision differs from the case request.
    mismatches = [r["repo_id"] for r in report.get("knowledge_snapshots", [])
                  if r["source_revision"] != "git:" + data["snapshots"][r["repo_id"]]]
    if mismatches:
        report["coverage"]["blockers"].append("case_knowledge_revision_mismatch: " + ", ".join(mismatches))
        report.update(status="blocked", ok=False, publishable=False)
    return report


def render_case_markdown(report):
    case = report["case"]
    lines = ["# 需求案例 " + case["id"], "", "输入 SHA-256：`" + case["input_sha256"] + "`", "",
             "| 仓库 | 原配置基线 | 本案例固定版本 |", "| --- | --- | --- |"]
    for repo, commit in case["requested_snapshots"].items():
        lines.append(f"| {repo} | {case['configured_baselines'][repo]} | {commit} |")
    lines.extend(["", "案例固定版本不消除工作树差异；事实核验通过不代表业务验收通过。", "",
                  render_requirement_markdown(report)])
    return "\n".join(lines)
