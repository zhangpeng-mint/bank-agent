"""Deterministic requirement orchestration; no model or business-service access."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bankdev_agent.config import ProjectConfig
from bankdev_agent.errors import BankDevError, ScopeViolation
from bankdev_agent.git_snapshot import GitSnapshotInspector
from bankdev_agent.index_query import IndexQuery
from bankdev_agent.index_store import IndexError, IndexStore
from bankdev_agent.knowledge_conflicts import KnowledgeConflictQuery
from bankdev_agent.knowledge_index import KnowledgeIndexer
from bankdev_agent.knowledge_query import KnowledgeQuery


STEPS = ("Intake", "ScopeCheck", "SearchKnowledge", "LocateInterfaces", "TraceCode",
         "AnalyzeImpact", "DetectConflicts", "CoverageGate", "DraftReport", "VerifyClaims")


@dataclass
class RequirementFrame:
    goal: str
    non_goals: list[str] = field(default_factory=list)
    business_objects: list[str] = field(default_factory=list)
    interface_ids: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    user_decisions: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str, **fields: Any) -> RequirementFrame:
        if not text.strip():
            raise IndexError("requirement must not be empty")
        frame = cls(text.strip(), **fields)
        frame.interface_ids = sorted(set(frame.interface_ids + re.findall(r"(?<!\d)\d{8}(?!\d)", text)))
        if any(not re.fullmatch(r"\d{8}", value) for value in frame.interface_ids):
            raise IndexError("interface ids must contain exactly eight digits")
        return frame


@dataclass
class Claim:
    text: str
    type: str
    category: str
    evidence_ids: list[str] = field(default_factory=list)
    binding: dict[str, str] = field(default_factory=dict)
    confidence_reason: str = "确定性索引模板；不代表运行时观察。"


@dataclass
class Evidence:
    evidence_id: str
    repo_id: str
    snapshot: str
    path: str
    start_line: int
    end_line: int
    content_hash: str
    markdown_link: str


class ClaimVerifier:
    """Rebuild allowed statements from source bindings, then validate live citations.

    Arbitrary prose with a valid citation is deliberately not accepted as a fact.
    This MVP supports only the templates below, not general semantic entailment.
    """

    def __init__(self, config: ProjectConfig, database: Path):
        self.config, self.store = config, IndexStore(database)

    def materialize(self, binding: dict[str, str]) -> tuple[Claim, Evidence]:
        kind, entity = binding["kind"], binding["id"]
        with self.store.connect() as con:
            if kind == "knowledge":
                row = con.execute(
                    "SELECT c.*,d.repo_id,d.source_revision,d.path,d.content_hash,s.active "
                    "FROM document_claims c JOIN documents d ON c.doc_id=d.doc_id "
                    "JOIN document_snapshots s ON c.snapshot_id=s.snapshot_id WHERE c.claim_id=?", (entity,)
                ).fetchone()
                if row is None or not row["active"]:
                    raise IndexError("missing or inactive knowledge entity")
                reference = con.execute("SELECT * FROM evidence WHERE evidence_id=?", (row["evidence_id"],)).fetchone()
                if reference is None or reference["snapshot_id"] != row["snapshot_id"]:
                    raise IndexError("missing or stale knowledge evidence")
                if (reference["path"] != row["path"] or reference["repo_id"] != row["repo_id"]
                        or reference["source_revision"] != row["source_revision"]
                        or not reference["start_line"] <= row["start_line"] <= row["end_line"] <= reference["end_line"]):
                    raise IndexError("knowledge evidence does not cover assertion")
                repo, revision, path = row["repo_id"], row["source_revision"], row["path"]
                start, end, digest = row["start_line"], row["end_line"], row["content_hash"]
                evidence_id = row["evidence_id"]
                # A shared chunk may support multiple fields; use its full coordinates.
                start, end = reference["start_line"], reference["end_line"]
                category = "historical_facts" if row["assertion_scope"] == "historical" else "knowledge_facts"
                text = (f"知识记载 {row['interface_id']}：{row['field_name']} = {row['normalized_value']}"
                        f"（文档状态：{row['status']}；作用域：{row['assertion_scope']}）")
            else:
                tables = {"endpoint": ("endpoints", "endpoint_id"), "symbol": ("symbols", "symbol_id"),
                          "mapper": ("mapper_statements", "mapper_id"), "table": ("sql_tables", "table_id"),
                          "edge": ("edges", "edge_id")}
                if kind not in tables:
                    raise IndexError("unsupported entity kind")
                table, key = tables[kind]
                row = con.execute(f"SELECT * FROM {table} WHERE {key}=?", (entity,)).fetchone()
                if row is None:
                    raise IndexError("unknown code entity")
                repo, revision = row["repo_id"], row["commit_sha"]
                if revision != self.config.repository(repo).baseline_commit:
                    raise IndexError("stale code snapshot")
                if kind == "edge":
                    path, start, end = row["evidence_path"], row["evidence_start_line"], row["evidence_end_line"]
                    text = f"静态关系 {row['source_id']} → {row['target_id']}：{row['kind']} ({row['classification']})"
                elif kind == "table":
                    path, start, end = row["first_file_path"], row["first_line"], row["first_line"]
                    text = f"SQL 引用表：{row['name']}"
                else:
                    path, start, end = row["file_path"], row["start_line"], row["end_line"]
                    text = (f"代码入口：{row['http_method']} {row['route']} ({row['operation_id']})" if kind == "endpoint"
                            else f"代码符号：{row['qualified_name']}" if kind == "symbol"
                            else f"Mapper：{row['namespace']}.{row['statement_id']}")
                file_row = con.execute("SELECT blob_hash FROM files WHERE repo_id=? AND commit_sha=? AND path=?",
                                       (repo, revision, path)).fetchone()
                if file_row is None:
                    raise IndexError("entity file not indexed")
                digest = file_row["blob_hash"]
                evidence_id = "code:" + hashlib.sha256(f"{repo}:{revision}:{path}:{start}:{end}".encode()).hexdigest()[:24]
                category = "code_facts"
        repository = self.config.repository(repo)
        absolute = repository.resolve_path(path, expected_kind="file")
        data = absolute.read_bytes()
        actual = (hashlib.sha256(data).hexdigest() if kind == "knowledge" else
                  hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest())
        if actual != digest:
            raise IndexError(f"stale evidence content: {repo}:{path}")
        if not 1 <= start <= end <= len(data.decode("utf-8-sig", errors="replace").splitlines()):
            raise IndexError("invalid evidence line range")
        if kind == "knowledge":
            excerpt = "\n".join(data.decode("utf-8-sig", errors="replace").splitlines()[start - 1:end]).strip()
            if hashlib.sha256(excerpt.encode()).hexdigest() != reference["content_hash"]:
                raise IndexError("stale knowledge chunk hash")
        evidence = Evidence(evidence_id, repo, revision, path, start, end, digest,
                            f"[{absolute.name}:{start}](<{absolute}:{start}>)")
        return Claim(text, "fact", category, [evidence_id], binding), evidence

    def verify(self, claims: list[dict], ledger: dict[str, dict]) -> dict:
        errors = []
        for position, claim in enumerate(claims):
            try:
                if claim["type"] not in {"fact", "inference", "question", "recommendation"}:
                    raise IndexError("unknown claim type")
                if claim["type"] != "fact":
                    continue
                if not claim.get("evidence_ids"):
                    raise IndexError("fact has no evidence")
                expected, evidence = self.materialize(claim["binding"])
                if any(claim.get(key) != asdict(expected)[key] for key in ("text", "category", "evidence_ids")):
                    raise IndexError("fact does not match bound entity/template")
                if ledger.get(evidence.evidence_id) != asdict(evidence):
                    raise IndexError("evidence ledger missing or altered")
            except (BankDevError, OSError, KeyError, TypeError) as exc:
                errors.append({"claim_index": position, "reason": str(exc)})
        return {"ok": not errors, "errors": errors, "fact_count": sum(c.get("type") == "fact" for c in claims)}


class RequirementAnalyzer:
    def __init__(self, config: ProjectConfig, database: Path):
        root = (config.source_path.parent.parent / "var").resolve()
        if not database.resolve().is_relative_to(root):
            raise ScopeViolation("Requirement index must stay under BankDev Agent var/")
        self.config, self.database = config, database

    def analyze(self, frame: RequirementFrame, max_depth: int = 8, max_nodes: int = 200) -> dict:
        if not 0 <= max_depth <= 32 or not 1 <= max_nodes <= 2000:
            raise IndexError("invalid graph budget")
        query = IndexQuery(self.config, self.database)
        verifier = ClaimVerifier(self.config, self.database)
        report: dict[str, Any] = {
            "schema_version": 1, "mode": "deterministic_template", "requirement": asdict(frame),
            "snapshots": [], "scope": [], "steps": [], "claims": [], "evidence_ledger": {},
            "searches": [], "interfaces": [], "traces": [], "impacts": [], "conflicts": [],
            "code_alignment": [], "unresolved": [], "user_decisions": frame.user_decisions,
            "open_questions": list(frame.open_questions), "limitations": [
                "仅分析配置白名单及所列 snapshot；未命中不表示绝对不存在。",
                "静态图不证明运行时调用；历史知识记载不自动升级为历史实测。",
                "自由文本只保留原文并抽取八位接口号，其余需求字段由用户显式提供。",
                "事实核验限定于索引模板；影响项是候选，未自动确认业务变更范围。"],
        }
        blockers: list[str] = []
        gaps: list[str] = []
        def step(name: str, status: str = "completed") -> None:
            report["steps"].append({"name": name, "status": status})
        def attempt(label: str, action: Any) -> Any:
            try:
                return action()
            except (BankDevError, OSError) as exc:
                gaps.append(f"{label}: {exc}")
                return None
        bound: set[tuple[str, str]] = set()
        def bind(kind: str, entity: str) -> None:
            if (kind, entity) in bound:
                return
            bound.add((kind, entity))
            try:
                claim, evidence = verifier.materialize({"kind": kind, "id": entity})
                report["claims"].append(asdict(claim))
                report["evidence_ledger"][evidence.evidence_id] = asdict(evidence)
            except (BankDevError, OSError) as exc:
                blockers.append(f"invalid_evidence {entity}: {exc}")
        step("Intake")
        for repo in self.config.repositories:
            snapshot = attempt(repo.id, lambda: GitSnapshotInspector().inspect(repo))
            report["snapshots"].append(snapshot.to_dict() if snapshot else {"repo_id": repo.id, "ok": False})
            report["scope"].append({"repo_id": repo.id, "root": str(repo.root), "include": repo.include,
                                    "exclude": repo.exclude, "baseline_commit": repo.baseline_commit})
            if snapshot and not snapshot.ok:
                gaps.append(f"snapshot_mismatch: {repo.id}")
        step("ScopeCheck")
        if self.database.is_file():
            with IndexStore(self.database).connect() as con:
                # Query APIs handle schema migration; initialize before reading v4 metadata.
                IndexStore(self.database).initialize()
                active = con.execute("SELECT * FROM document_snapshots WHERE active=1 ORDER BY repo_id").fetchall()
            report["knowledge_snapshots"] = [dict(row) for row in active]
            if not active:
                gaps.append("missing_knowledge_index")
            for row in active:
                repo = self.config.repository(row["repo_id"])
                indexer = KnowledgeIndexer(self.config, self.database)
                files = attempt("knowledge_freshness", lambda: indexer._files(repo))
                if files is None or indexer._source_revision(repo, files) != row["source_revision"] or len(files) != row["file_count"]:
                    blockers.append(f"stale_knowledge_snapshot: {repo.id}; run index-docs")
        terms = list(dict.fromkeys(frame.interface_ids + frame.terms + frame.business_objects)) or [frame.goal]
        if len(terms) > 20:
            gaps.append("query_budget_exhausted: only first 20 queries analyzed")
        terms = terms[:20]
        for term in terms:
            found = attempt("knowledge " + term, lambda: KnowledgeQuery(self.config, self.database).search(term))
            if found:
                report["searches"].append(found)
                if not found["count"]:
                    gaps.append(f"knowledge_not_found_in_scope: {term}")
        step("SearchKnowledge")
        endpoints: dict[str, dict] = {}
        for term in terms:
            found = attempt("interface " + term, lambda: query.search_interface(term))
            if found:
                missing = {r.id for r in self.config.repositories if r.kind == "java_source"} - found["snapshots"].keys()
                gaps.extend(f"missing_code_index: {repo}" for repo in sorted(missing))
                report["interfaces"].append(found)
                for endpoint in found["matches"]:
                    endpoints[endpoint["endpoint_id"]] = endpoint
                if not found["count"]:
                    gaps.append(f"endpoint_not_found_in_scope: {term}")
                if found["count"] >= 10:
                    gaps.append(f"interface_candidate_limit: {term}")
        step("LocateInterfaces")
        for entity in sorted(endpoints):
            traced = attempt("trace " + entity, lambda: query.trace(entity, max_depth, max_nodes))
            if not traced:
                continue
            report["traces"].append(traced)
            for node in traced["nodes"]:
                kind = {"endpoint": "endpoint", "mapper_statement": "mapper", "sql_table": "table"}.get(node["kind"], "symbol")
                bind(kind, node["id"])
            for edge in traced["edges"]:
                bind("edge", edge["id"])
            report["unresolved"].extend(traced["unresolved"])
            if traced["coverage"]["truncated"]:
                gaps.append(f"trace_truncated: {entity}")
        step("TraceCode")
        for entity in sorted(endpoints):
            impact = attempt("impact " + entity, lambda: query.impact(entity, max_depth, max_nodes))
            if impact:
                report["impacts"].append(impact)
                for node in impact["direct_impacts"] + impact["transitive_candidates"]:
                    kind = {"endpoint": "endpoint", "mapper_statement": "mapper", "sql_table": "table"}.get(node["kind"], "symbol")
                    bind(kind, node["id"])
                report["unresolved"].extend(impact["unresolved"])
                if impact["coverage"]["truncated"]:
                    gaps.append(f"impact_truncated: {entity}")
        step("AnalyzeImpact")
        ids = set(frame.interface_ids)
        for search in report["searches"]:
            for hit in search["results"]:
                ids.update(hit["interface_ids"])
        # Explicit interface scope wins over incidental references in search results.
        ids = set(frame.interface_ids) or ids
        for interface_id in sorted(ids)[:20]:
            result = attempt("conflicts " + interface_id, lambda: KnowledgeConflictQuery(self.config, self.database).report(interface_id))
            if not result:
                continue
            report["conflicts"].extend(result["conflicts"])
            report["code_alignment"].extend(result["code_alignment"])
            for claim in result["claims"]:
                bind("knowledge", claim["claim_id"])
            if not result["claims"]:
                gaps.append(f"no_structured_claims: {interface_id}")
            if not result["ok"]:
                blockers.append(f"blocking_conflict: {interface_id}")
            if not result["code_alignment"] or any(a["status"] != "aligned" for a in result["code_alignment"]):
                gaps.append(f"unresolved_code_alignment: {interface_id}")
            aligned_ids = {e["endpoint_id"] for a in result["code_alignment"] for e in a["code_evidence"]}
            located_ids = {e["endpoint_id"] for search in report["interfaces"] if search["query"] == interface_id for e in search["matches"]}
            if located_ids and not located_ids.issubset(aligned_ids):
                gaps.append(f"interface_alias_not_confirmed_by_current_knowledge: {interface_id}")
        if not ids:
            gaps.append("no_interface_id_for_field_conflict_check")
        if len(ids) > 20:
            gaps.append("conflict_query_budget_exhausted")
        step("DetectConflicts")
        if report["unresolved"]:
            gaps.append("unresolved_static_relations")
        if not endpoints:
            gaps.append("no_endpoint_located")
        if not frame.acceptance_criteria:
            gaps.append("acceptance_criteria_unspecified")
            report["open_questions"].append("请确认本次需求的验收条件。")
        if not frame.non_goals:
            gaps.append("non_goals_unspecified")
            report["open_questions"].append("请确认本次需求的非目标。")
        if frame.open_questions:
            gaps.append("user_questions_pending")
        report["coverage"] = {"gaps": sorted(set(gaps)), "budgets": {"max_depth": max_depth, "max_nodes_per_graph": max_nodes,
                               "max_queries": 20}, "endpoint_count": len(endpoints), "blockers": blockers}
        step("CoverageGate", "blocked" if blockers else "partial" if gaps else "completed")
        for gap in sorted(set(gaps)):
            report["open_questions"].append(f"需补充或复核（限定本报告 snapshot/路径/预算）：{gap}")
        report["claims"].extend(asdict(Claim(q, "question", "open_questions", confidence_reason="用户输入缺失或覆盖门禁未满足。"))
                                for q in report["open_questions"])
        report["claims"].append(asdict(Claim("建议依据所列入口、静态调用图和验收条件人工复核变更与回归范围。",
                                             "recommendation", "recommendations")))
        step("DraftReport")
        report["verification"] = verifier.verify(report["claims"], report["evidence_ledger"])
        if not report["verification"]["ok"]:
            blockers.append("claim_verification_failed")
        step("VerifyClaims", "completed" if report["verification"]["ok"] else "blocked")
        report["status"] = "blocked" if blockers else "partial" if gaps else "complete"
        report["ok"] = report["status"] == "complete"
        report["publishable"] = not blockers
        return report


def render_requirement_markdown(report: dict) -> str:
    lines = ["# 需求分析报告", "", f"状态：{report['status']}；模式：{report['mode']}", "",
             "## RequirementFrame", "", "```json", json.dumps(report["requirement"], ensure_ascii=False, indent=2), "```"]
    for title, key in (("四仓 snapshot", "snapshots"), ("扫描路径与范围", "scope"), ("工作流", "steps")):
        lines.extend(["", f"## {title}", "", "```json", json.dumps(report[key], ensure_ascii=False, indent=2), "```"])
    lines.extend(["", "## 知识索引 snapshot", "", "```json",
                  json.dumps(report.get("knowledge_snapshots", []), ensure_ascii=False, indent=2), "```"])
    for title, category in (("知识事实", "knowledge_facts"), ("当前代码事实", "code_facts"), ("历史事实（知识记载）", "historical_facts"),
                            ("推断", "inferences"), ("建议", "recommendations"), ("待确认问题", "open_questions")):
        lines.extend(["", f"## {title}", ""])
        selected = [c for c in report["claims"] if c["category"] == category]
        for claim in selected:
            links = [report["evidence_ledger"][eid]["markdown_link"] for eid in claim["evidence_ids"] if eid in report["evidence_ledger"]]
            lines.append(f"- [{claim['type']}] {claim['text']} {' '.join(links)}")
            if claim.get("source") == "model":
                lines.append(f"  模型建议，待人工复核。依据：{claim['confidence_reason']} 反证/限制：{claim['countercondition']}")
        if not selected:
            lines.append("本次未形成有证据的条目。")
    for title, key in (("用户决策（输入原文）", "user_decisions"), ("冲突", "conflicts"), ("代码对齐", "code_alignment"),
                       ("影响候选", "impacts"), ("覆盖范围", "coverage"), ("unresolved", "unresolved"),
                       ("核验结果", "verification"), ("限制", "limitations")):
        lines.extend(["", f"## {title}", "", "```json", json.dumps(report[key], ensure_ascii=False, indent=2), "```"])
    lines.extend(["", "## 可点击证据", ""])
    for eid, item in sorted(report["evidence_ledger"].items()):
        lines.append(f"- `{eid}` {item['markdown_link']} — snapshot `{item['snapshot']}`；hash `{item['content_hash']}`")
    if "model_assistance" in report:
        lines.extend(["", "## 模型辅助与审计（候选，不构成用户决策）", "", "```json",
                      json.dumps(report["model_assistance"], ensure_ascii=False, indent=2), "```"])
    return "\n".join(lines) + "\n"
