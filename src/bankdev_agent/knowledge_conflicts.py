from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from bankdev_agent.config import ProjectConfig
from bankdev_agent.index_store import IndexError, IndexStore


INTERFACE_ID_RE = re.compile(r"^\d{8}$")


class KnowledgeConflictQuery:
    """Report deterministic field claims, semantic conflicts and code alignment."""

    def __init__(self, config: ProjectConfig, database: Path) -> None:
        self.config = config
        self.store = IndexStore(database)

    def report(self, interface_id: str | None = None) -> dict[str, Any]:
        self._require_index()
        if interface_id is not None and not INTERFACE_ID_RE.fullmatch(interface_id):
            raise IndexError("interface id must contain exactly eight digits")
        with self.store.connect() as connection:
            snapshot = connection.execute(
                "SELECT * FROM document_snapshots WHERE active=1 ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if snapshot is None:
                raise IndexError("Knowledge index is empty; run index-docs first")
            where = "c.snapshot_id=?"
            parameters: list[Any] = [snapshot["snapshot_id"]]
            if interface_id:
                where += " AND c.interface_id=?"
                parameters.append(interface_id)
            rows = connection.execute(
                "SELECT c.*,d.path,d.title,d.version,e.uri,e.classification "
                "FROM document_claims c JOIN documents d ON d.doc_id=c.doc_id "
                "JOIN evidence e ON e.evidence_id=c.evidence_id "
                f"WHERE {where} ORDER BY c.interface_id,c.field_name,c.assertion_scope,d.path,c.start_line",
                parameters,
            ).fetchall()
            claims = [self._claim_payload(row) for row in rows]
            conflict_where = "snapshot_id=?"
            conflict_parameters: list[Any] = [snapshot["snapshot_id"]]
            if interface_id:
                conflict_where += " AND interface_id=?"
                conflict_parameters.append(interface_id)
            conflict_rows = connection.execute(
                "SELECT * FROM semantic_conflicts WHERE " + conflict_where
                + " ORDER BY severity DESC,interface_id,field_name",
                conflict_parameters,
            ).fetchall()
            conflicts = [self._conflict_payload(row) for row in conflict_rows]
            code_alignment = self._code_alignment(connection, claims)
        return {
            "ok": not any(item["severity"] == "high" for item in conflicts),
            "interface_id": interface_id,
            "snapshot": {
                "snapshot_id": snapshot["snapshot_id"],
                "source_revision": snapshot["source_revision"],
                "file_count": snapshot["file_count"],
            },
            "summary": {
                "claims": len(claims),
                "current_claims": sum(item["assertion_scope"] == "current" for item in claims),
                "historical_claims": sum(
                    item["assertion_scope"] == "historical" for item in claims
                ),
                "conflicts": len(conflicts),
                "blocking_conflicts": sum(item["severity"] == "high" for item in conflicts),
                "code_alignments": len(code_alignment),
            },
            "claims": claims,
            "conflicts": conflicts,
            "code_alignment": code_alignment,
        }

    def _code_alignment(self, connection: Any, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_document: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        for claim in claims:
            if claim["assertion_scope"] != "current":
                continue
            by_document.setdefault((claim["interface_id"], claim["doc_id"]), {})[
                claim["field_name"]
            ] = claim
        results: list[dict[str, Any]] = []
        current_commits = {
            repository.id: repository.baseline_commit
            for repository in self.config.repositories
            if repository.kind == "java_source"
        }
        for (interface_id, doc_id), fields in sorted(by_document.items()):
            if "http_method" not in fields or "route" not in fields:
                continue
            method = fields["http_method"]["normalized_value"]
            route = fields["route"]["normalized_value"]
            endpoint_rows = connection.execute(
                "SELECT * FROM endpoints WHERE http_method=? AND route=? ORDER BY repo_id,file_path",
                (method, route),
            ).fetchall()
            endpoint_rows = [
                row for row in endpoint_rows
                if current_commits.get(row["repo_id"]) == row["commit_sha"]
            ]
            status = "aligned" if endpoint_rows else "unresolved_no_exact_endpoint"
            results.append({
                "interface_id": interface_id,
                "doc_id": doc_id,
                "document_contract": {"http_method": method, "route": route},
                "status": status,
                "interpretation": (
                    "The current document contract exactly matches an indexed endpoint."
                    if endpoint_rows else
                    "No exact current-code endpoint was found; this is unresolved, not proof of drift."
                ),
                "knowledge_evidence": [
                    fields["http_method"]["evidence"], fields["route"]["evidence"]
                ],
                "code_evidence": [self._endpoint_payload(row) for row in endpoint_rows],
            })
        return results

    def _claim_payload(self, row: Any) -> dict[str, Any]:
        repository = self.config.repository("bank_knowledge")
        absolute = repository.resolved_root / row["path"]
        return {
            "claim_id": row["claim_id"], "doc_id": row["doc_id"],
            "interface_id": row["interface_id"], "field_name": row["field_name"],
            "normalized_value": row["normalized_value"], "raw_value": row["raw_value"],
            "assertion_scope": row["assertion_scope"], "status": row["status"],
            "title": row["title"], "version": row["version"], "path": row["path"],
            "start_line": row["start_line"], "end_line": row["end_line"],
            "evidence": {
                "evidence_id": row["evidence_id"], "uri": row["uri"],
                "classification": row["classification"],
                "markdown_link": f"[{absolute.name}:{row['start_line']}](<{absolute}:{row['start_line']}>)",
            },
        }

    @staticmethod
    def _conflict_payload(row: Any) -> dict[str, Any]:
        return {
            "conflict_id": row["conflict_id"], "interface_id": row["interface_id"],
            "field_name": row["field_name"], "conflict_kind": row["conflict_kind"],
            "severity": row["severity"], "values": json.loads(row["values_json"]),
            "claim_ids": json.loads(row["claim_ids_json"]),
            "description": row["description"],
        }

    def _endpoint_payload(self, row: Any) -> dict[str, Any]:
        repository = self.config.repository(row["repo_id"])
        absolute = repository.resolved_root / row["file_path"]
        return {
            "endpoint_id": row["endpoint_id"], "repo_id": row["repo_id"],
            "commit_sha": row["commit_sha"], "http_method": row["http_method"],
            "route": row["route"], "operation_id": row["operation_id"],
            "path": row["file_path"], "start_line": row["start_line"],
            "end_line": row["end_line"],
            "markdown_link": f"[{absolute.name}:{row['start_line']}](<{absolute}:{row['start_line']}>)",
        }

    def _require_index(self) -> None:
        if not self.store.path.is_file():
            raise IndexError(f"Index does not exist: {self.store.path}; run index-docs first")
        self.store.initialize()
