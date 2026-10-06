from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from bankdev_agent.config import ProjectConfig
from bankdev_agent.index_store import IndexError, IndexStore


class KnowledgeQuery:
    def __init__(self, config: ProjectConfig, database: Path) -> None:
        self.config = config
        self.store = IndexStore(database)

    def search(
        self, query: str, limit: int = 10, systems: list[str] | None = None,
        include_superseded: bool = False,
    ) -> dict[str, Any]:
        self._require_index()
        term = query.strip()
        if not term:
            raise IndexError("knowledge query must not be empty")
        if not 1 <= limit <= 100:
            raise IndexError("limit must be between 1 and 100")
        requested_systems = set(systems or [])
        allowed_systems = {"dfbm", "dcis", "fineract"}
        if requested_systems - allowed_systems:
            raise IndexError(f"Unknown systems: {sorted(requested_systems - allowed_systems)}")
        with self.store.connect() as connection:
            snapshot = connection.execute(
                "SELECT * FROM document_snapshots WHERE active=1 ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if snapshot is None:
                raise IndexError("Knowledge index is empty; run index-docs first")
            candidates: dict[str, tuple[tuple[float, ...], Any, list[str]]] = {}
            pattern = f"%{term}%"
            like_rows = connection.execute(
                "SELECT c.*,d.title,d.path,d.repo_id,d.source_revision,d.status,d.version,"
                "d.material_nature,d.confidentiality,d.metadata_json,d.snapshot_id "
                "FROM document_chunks c JOIN documents d ON d.doc_id=c.doc_id "
                "JOIN document_snapshots s ON s.snapshot_id=d.snapshot_id "
                "WHERE s.active=1 AND (c.text LIKE ? OR c.heading_path LIKE ? OR d.title LIKE ?) "
                "LIMIT ?", (pattern, pattern, pattern, limit * 30),
            ).fetchall()
            for row in like_rows:
                interface_ids = json.loads(row["interface_ids_json"])
                metadata = json.loads(row["metadata_json"])
                primary_ids = metadata.get("primary_interface_ids", [])
                reasons = []
                if term in primary_ids:
                    rank = (0.0, 0.0)
                    reasons.append("primary_interface_id")
                elif term in interface_ids:
                    rank = (1.0, 0.0)
                    reasons.append("exact_interface_id")
                elif row["title"] == term:
                    rank = (2.0, 0.0)
                    reasons.append("exact_title")
                elif term.casefold() in row["title"].casefold():
                    rank = (3.0, 0.0)
                    reasons.append("title_contains")
                elif term.casefold() in row["heading_path"].casefold():
                    rank = (4.0, 0.0)
                    reasons.append("heading_contains")
                else:
                    rank = (5.0, 0.0)
                    reasons.append("text_contains")
                candidates[row["chunk_id"]] = (rank, row, reasons)
            try:
                fts_term = '"' + term.replace('"', '""') + '"'
                fts_rows = connection.execute(
                    "SELECT c.*,d.title,d.path,d.repo_id,d.source_revision,d.status,d.version,"
                    "d.material_nature,d.confidentiality,d.metadata_json,d.snapshot_id,"
                    "bm25(document_chunks_fts,5.0,3.0,1.0) AS fts_score "
                    "FROM document_chunks_fts JOIN document_chunks c "
                    "ON c.chunk_id=document_chunks_fts.chunk_id "
                    "JOIN documents d ON d.doc_id=c.doc_id "
                    "JOIN document_snapshots s ON s.snapshot_id=d.snapshot_id "
                    "WHERE s.active=1 AND document_chunks_fts MATCH ? ORDER BY fts_score LIMIT ?",
                    (fts_term, limit * 10),
                ).fetchall()
            except sqlite3.OperationalError:
                fts_rows = []
            for row in fts_rows:
                existing = candidates.get(row["chunk_id"])
                if existing:
                    existing[2].append("fts5_trigram")
                else:
                    candidates[row["chunk_id"]] = ((5.0, float(row["fts_score"])), row,
                                                    ["fts5_trigram"])
            results = []
            per_document: dict[str, int] = {}
            for rank, row, reasons in sorted(candidates.values(), key=lambda item: item[0]):
                if not include_superseded and _is_superseded(row["status"]):
                    continue
                row_systems = set(json.loads(row["systems_json"]))
                if requested_systems and not requested_systems.intersection(row_systems):
                    continue
                if per_document.get(row["doc_id"], 0) >= 2:
                    continue
                evidence = connection.execute(
                    "SELECT * FROM evidence WHERE snapshot_id=? AND path=? "
                    "AND start_line=? AND end_line=? AND kind='document'",
                    (row["snapshot_id"], row["path"], row["start_line"], row["end_line"]),
                ).fetchone()
                metadata = json.loads(row["metadata_json"])
                results.append({
                    "chunk_id": row["chunk_id"], "doc_id": row["doc_id"],
                    "title": row["title"], "heading_path": row["heading_path"],
                    "repo_id": row["repo_id"], "source_revision": row["source_revision"],
                    "path": row["path"], "status": row["status"], "version": row["version"],
                    "material_nature": row["material_nature"],
                    "confidentiality": row["confidentiality"],
                    "interface_ids": json.loads(row["interface_ids_json"]),
                    "systems": sorted(row_systems), "match_reasons": reasons,
                    "excerpt": _excerpt(row["text"], term),
                    "baseline_match": bool(metadata.get("baseline_match")),
                    "evidence": self._evidence_payload(evidence, row),
                })
                per_document[row["doc_id"]] = per_document.get(row["doc_id"], 0) + 1
                if len(results) >= limit:
                    break
            conflicts = [dict(row) for row in connection.execute(
                "SELECT conflict_kind,conflict_key,document_ids_json,description "
                "FROM document_conflicts WHERE snapshot_id=? ORDER BY conflict_kind,conflict_key",
                (snapshot["snapshot_id"],),
            ).fetchall()]
            for conflict in conflicts:
                conflict["document_ids"] = json.loads(conflict.pop("document_ids_json"))
            semantic_conflicts = []
            if term.isdigit() and len(term) == 8:
                semantic_conflicts = [dict(row) for row in connection.execute(
                    "SELECT conflict_id,interface_id,field_name,conflict_kind,severity,"
                    "values_json,claim_ids_json,description FROM semantic_conflicts "
                    "WHERE snapshot_id=? AND interface_id=? ORDER BY severity DESC,field_name",
                    (snapshot["snapshot_id"], term),
                ).fetchall()]
                for conflict in semantic_conflicts:
                    conflict["values"] = json.loads(conflict.pop("values_json"))
                    conflict["claim_ids"] = json.loads(conflict.pop("claim_ids_json"))
        return {
            "ok": True, "query": term, "count": len(results), "results": results,
            "snapshot": {"snapshot_id": snapshot["snapshot_id"],
                         "repo_id": snapshot["repo_id"],
                         "source_revision": snapshot["source_revision"],
                         "file_count": snapshot["file_count"]},
            "conflicts": conflicts,
            "semantic_conflicts": semantic_conflicts,
        }

    def _require_index(self) -> None:
        if not self.store.path.is_file():
            raise IndexError(f"Index does not exist: {self.store.path}; run index-docs first")
        self.store.initialize()

    def _evidence_payload(self, evidence: Any, row: Any) -> dict[str, Any]:
        repository = self.config.repository(row["repo_id"])
        absolute = repository.resolved_root / row["path"]
        payload = {
            "evidence_id": evidence["evidence_id"] if evidence else None,
            "uri": evidence["uri"] if evidence else None,
            "classification": evidence["classification"] if evidence else "knowledge_claim",
            "path": row["path"], "start_line": row["start_line"], "end_line": row["end_line"],
            "markdown_link": f"[{absolute.name}:{row['start_line']}](<{absolute}:{row['start_line']}>)",
        }
        return payload


def _is_superseded(status: str) -> bool:
    normalized = status.strip().casefold()
    return normalized in {"superseded", "obsolete", "deprecated", "历史", "废弃", "已废弃"}


def _excerpt(text: str, term: str, max_chars: int = 600) -> str:
    folded = text.casefold()
    position = folded.find(term.casefold())
    if position < 0:
        return text[:max_chars]
    start = max(0, position - max_chars // 3)
    end = min(len(text), start + max_chars)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix
