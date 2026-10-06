from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bankdev_agent.config import ProjectConfig, RepositoryConfig
from bankdev_agent.errors import BankDevError
from bankdev_agent.index_store import IndexStore, SCHEMA_VERSION


DOCUMENT_PARSER_VERSION = "markdown-structure-v4"
MAX_CHUNK_CHARS = 5000
PRIMARY_INTERFACE_RE = re.compile(r"^\|\s*原服务号\s*\|\s*`?(\d{8})`?\s*\|\s*$")
INTERFACE_RE = re.compile(r"(?<!\d)(\d{8})(?!\d)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


class KnowledgeIndexError(BankDevError):
    """Raised when the authorized knowledge index cannot be built."""


@dataclass(frozen=True)
class KnowledgeFile:
    path: str
    data: bytes
    content_hash: str
    git_blob_hash: str
    baseline_blob_hash: str | None


def _stable_id(kind: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{kind}:{digest}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_blob_hash(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


class KnowledgeIndexer:
    """Incrementally index tracked, allowlisted knowledge documents."""

    def __init__(self, config: ProjectConfig, database: Path) -> None:
        self.config = config
        self.store = IndexStore(database)

    def index(self, repo_id: str = "bank_knowledge") -> dict[str, Any]:
        repository = self.config.repository(repo_id)
        if repository.kind != "git_markdown":
            raise KnowledgeIndexError(f"Repository is not a knowledge source: {repo_id}")
        self.store.initialize()
        files = self._files(repository)
        source_revision = self._source_revision(repository, files)
        snapshot_id = _stable_id("docsnapshot", repository.id, source_revision)
        previous = self._active_documents(repository.id)
        current = {item.path: item.content_hash for item in files}
        changes = {
            "added": sorted(set(current) - set(previous)),
            "modified": sorted(path for path in set(current) & set(previous)
                               if current[path] != previous[path]),
            "unchanged": sorted(path for path in set(current) & set(previous)
                                if current[path] == previous[path]),
            "deleted": sorted(set(previous) - set(current)),
        }
        with self.store.transaction() as connection:
            run_id = int(connection.execute(
                "INSERT INTO document_index_runs(started_at,status,repo_id,source_revision) "
                "VALUES (?,'running',?,?)", (_now(), repository.id, source_revision)
            ).lastrowid)
        parsed: list[tuple[KnowledgeFile, dict[str, Any], bool]] = []
        try:
            with self.store.connect() as connection:
                for item in files:
                    cached = connection.execute(
                        "SELECT payload_json FROM document_parse_cache "
                        "WHERE content_hash=? AND parser_version=?",
                        (item.content_hash, DOCUMENT_PARSER_VERSION),
                    ).fetchone()
                    if cached:
                        payload = json.loads(cached["payload_json"])
                        cache_hit = True
                    else:
                        payload = _parse_document(item.data.decode("utf-8-sig", errors="replace"))
                        cache_hit = False
                    parsed.append((item, payload, cache_hit))
            stats = self._replace_snapshot(
                repository, snapshot_id, source_revision, parsed, changes
            )
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE document_index_runs SET finished_at=?,status='complete',stats_json=? "
                    "WHERE run_id=?", (_now(), self.store.json(stats), run_id)
                )
        except Exception as exc:
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE document_index_runs SET finished_at=?,status='failed',stats_json=? "
                    "WHERE run_id=?", (_now(), self.store.json({"error": str(exc)}), run_id)
                )
            if isinstance(exc, BankDevError):
                raise
            raise KnowledgeIndexError(f"Document index run {run_id} failed: {exc}") from exc
        return {
            "ok": True, "schema_version": SCHEMA_VERSION,
            "parser_version": DOCUMENT_PARSER_VERSION, "database": str(self.store.path),
            "run_id": run_id, "snapshot_id": snapshot_id,
            "source_revision": source_revision, "stats": stats,
        }

    def _files(self, repository: RepositoryConfig) -> list[KnowledgeFile]:
        tracked = self._git(repository, "ls-files", "-z").decode(errors="replace").split("\0")
        baseline = self._baseline_blobs(repository)
        result: list[KnowledgeFile] = []
        for relative in sorted(path for path in tracked if path):
            if not repository.is_included(relative) or repository.is_demo(relative):
                continue
            if Path(relative).suffix.lower() not in {".md", ".txt"}:
                continue
            path = repository.resolve_path(relative, expected_kind="file")
            data = path.read_bytes()
            result.append(KnowledgeFile(
                path=relative, data=data,
                content_hash=hashlib.sha256(data).hexdigest(),
                git_blob_hash=_git_blob_hash(data),
                baseline_blob_hash=baseline.get(relative),
            ))
        return result

    def _baseline_blobs(self, repository: RepositoryConfig) -> dict[str, str]:
        output = self._git(
            repository, "ls-tree", "-r", "-z", repository.baseline_commit
        ).decode(errors="replace")
        result: dict[str, str] = {}
        for record in output.split("\0"):
            if "\t" not in record:
                continue
            metadata, path = record.split("\t", 1)
            fields = metadata.split()
            if len(fields) == 3 and fields[1] == "blob":
                result[path] = fields[2]
        return result

    @staticmethod
    def _git(repository: RepositoryConfig, *args: str) -> bytes:
        try:
            return subprocess.run(
                ["git", "-C", str(repository.resolved_root), *args], check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ).stdout
        except subprocess.CalledProcessError as exc:
            raise KnowledgeIndexError(
                f"Git read failed for {repository.id}: {exc.stderr.decode(errors='replace').strip()}"
            ) from exc

    @staticmethod
    def _source_revision(repository: RepositoryConfig, files: list[KnowledgeFile]) -> str:
        digest = hashlib.sha256()
        clean = True
        for item in files:
            digest.update(item.path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(item.content_hash.encode("ascii"))
            digest.update(b"\0")
            clean = clean and item.git_blob_hash == item.baseline_blob_hash
        return (f"git:{repository.baseline_commit}" if clean
                else f"worktree-sha256:{digest.hexdigest()}")

    def _active_documents(self, repo_id: str) -> dict[str, str]:
        self.store.initialize()
        with self.store.connect() as connection:
            return {row["path"]: row["content_hash"] for row in connection.execute(
                "SELECT d.path,d.content_hash FROM documents d "
                "JOIN document_snapshots s ON s.snapshot_id=d.snapshot_id "
                "WHERE s.repo_id=? AND s.active=1", (repo_id,)
            ).fetchall()}

    def _replace_snapshot(
        self, repository: RepositoryConfig, snapshot_id: str, source_revision: str,
        parsed: list[tuple[KnowledgeFile, dict[str, Any], bool]],
        changes: dict[str, list[str]],
    ) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "files_scanned": len(parsed), "files_parsed": 0, "cache_hits": 0,
            "documents": 0, "chunks": 0, "evidence": 0, "conflicts": 0,
            "claims": 0, "semantic_conflicts": 0,
            "changes": {key: len(value) for key, value in changes.items()},
            "change_paths": changes,
        }
        with self.store.transaction() as connection:
            old_chunk_ids = [row["chunk_id"] for row in connection.execute(
                "SELECT c.chunk_id FROM document_chunks c JOIN documents d ON d.doc_id=c.doc_id "
                "WHERE d.snapshot_id=?", (snapshot_id,)
            ).fetchall()]
            connection.executemany(
                "DELETE FROM document_chunks_fts WHERE chunk_id=?",
                [(chunk_id,) for chunk_id in old_chunk_ids],
            )
            connection.execute("DELETE FROM document_snapshots WHERE snapshot_id=?", (snapshot_id,))
            connection.execute("UPDATE document_snapshots SET active=0 WHERE repo_id=?", (repository.id,))
            connection.execute(
                "INSERT INTO document_snapshots VALUES (?,?,?,?,?,1)",
                (snapshot_id, repository.id, source_revision, _now(), len(parsed)),
            )
            document_rows: list[dict[str, Any]] = []
            claim_rows: list[dict[str, Any]] = []
            for item, payload, cache_hit in parsed:
                if cache_hit:
                    stats["cache_hits"] += 1
                else:
                    stats["files_parsed"] += 1
                    connection.execute(
                        "INSERT OR REPLACE INTO document_parse_cache VALUES (?,?,?)",
                        (item.content_hash, DOCUMENT_PARSER_VERSION, self.store.json(payload)),
                    )
                metadata = dict(payload["metadata"])
                metadata["baseline_commit"] = repository.baseline_commit
                metadata["baseline_match"] = item.git_blob_hash == item.baseline_blob_hash
                metadata["primary_interface_ids"] = payload["primary_interface_ids"]
                metadata["interface_ids"] = payload["interface_ids"]
                metadata["systems"] = payload["systems"]
                doc_id = _stable_id("doc", repository.id, source_revision, item.path)
                doc_type = metadata.get("doc_type") or _infer_doc_type(item.path)
                title = metadata.get("title") or payload["title"] or Path(item.path).stem
                status = str(metadata.get("status") or "unspecified")
                version = str(metadata.get("version") or "unversioned")
                material = str(metadata.get("material_nature") or metadata.get("资料性质") or "unspecified")
                confidentiality = str(metadata.get("confidentiality") or "internal")
                connection.execute(
                    "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (doc_id, snapshot_id, repository.id, source_revision, item.path,
                     item.git_blob_hash, item.content_hash, title, doc_type, status,
                     version, material, confidentiality, self.store.json(metadata)),
                )
                logical_keys = payload["primary_interface_ids"] or [str(metadata.get("doc_id") or item.path)]
                for logical_key in logical_keys:
                    connection.execute(
                        "INSERT INTO document_versions VALUES (?,?,?,?,?,?,?,?)",
                        (_stable_id("docversion", snapshot_id, doc_id, logical_key), snapshot_id,
                         doc_id, logical_key, version, status, metadata.get("effective_from"),
                         metadata.get("supersedes")),
                    )
                chunk_evidence: list[tuple[int, int, str]] = []
                for ordinal, chunk in enumerate(payload["chunks"]):
                    chunk_id = _stable_id("chunk", doc_id, str(ordinal), chunk["content_hash"])
                    interface_ids = sorted(set(INTERFACE_RE.findall(chunk["text"])))
                    systems = sorted(set(payload["systems"]) | set(_detect_systems(chunk["text"] + " " + title)))
                    connection.execute(
                        "INSERT INTO document_chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (chunk_id, doc_id, ordinal, chunk["heading_path"], chunk["start_line"],
                         chunk["end_line"], chunk["text"], chunk["content_hash"],
                         self.store.json(interface_ids), self.store.json(systems)),
                    )
                    connection.execute(
                        "INSERT INTO document_chunks_fts(chunk_id,title,heading_path,text) VALUES (?,?,?,?)",
                        (chunk_id, title, chunk["heading_path"], chunk["text"]),
                    )
                    evidence_id = _stable_id("evidence", snapshot_id, item.path,
                                             str(chunk["start_line"]), str(chunk["end_line"]))
                    uri = (f"bankdev://knowledge/{repository.id}/{source_revision}/{item.path}"
                           f"#L{chunk['start_line']}-L{chunk['end_line']}")
                    connection.execute(
                        "INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (evidence_id, snapshot_id, "document", repository.id, source_revision,
                         item.path, chunk["start_line"], chunk["end_line"],
                         chunk["content_hash"], uri, "knowledge_claim",),
                    )
                    chunk_evidence.append((chunk["start_line"], chunk["end_line"], evidence_id))
                    stats["chunks"] += 1
                    stats["evidence"] += 1
                assertion_scope = _assertion_scope(status)
                for interface_id in payload["primary_interface_ids"]:
                    for claim in payload["claims"]:
                        evidence_id = next(
                            (evidence_id for start, end, evidence_id in chunk_evidence
                             if start <= claim["start_line"] <= end),
                            None,
                        )
                        if evidence_id is None:
                            continue
                        claim_id = _stable_id(
                            "docclaim", snapshot_id, item.path, interface_id,
                            claim["field_name"], str(claim["start_line"]),
                            claim["normalized_value"],
                        )
                        row = {
                            "claim_id": claim_id, "snapshot_id": snapshot_id,
                            "doc_id": doc_id, "interface_id": interface_id,
                            "field_name": claim["field_name"],
                            "normalized_value": claim["normalized_value"],
                            "raw_value": claim["raw_value"],
                            "assertion_scope": assertion_scope, "status": status,
                            "evidence_id": evidence_id,
                            "start_line": claim["start_line"],
                            "end_line": claim["end_line"],
                        }
                        connection.execute(
                            "INSERT INTO document_claims VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            tuple(row[key] for key in (
                                "claim_id", "snapshot_id", "doc_id", "interface_id",
                                "field_name", "normalized_value", "raw_value",
                                "assertion_scope", "status", "evidence_id",
                                "start_line", "end_line",
                            )),
                        )
                        claim_rows.append(row)
                        stats["claims"] += 1
                document_rows.append({"doc_id": doc_id, "path": item.path, "status": status,
                                      "version": version, "primary": payload["primary_interface_ids"]})
                stats["documents"] += 1
            conflicts = _detect_conflicts(snapshot_id, document_rows)
            connection.executemany(
                "INSERT INTO document_conflicts VALUES (?,?,?,?,?,?)", conflicts
            )
            stats["conflicts"] = len(conflicts)
            semantic_conflicts = _detect_semantic_conflicts(snapshot_id, claim_rows)
            connection.executemany(
                "INSERT INTO semantic_conflicts VALUES (?,?,?,?,?,?,?,?,?)",
                semantic_conflicts,
            )
            stats["semantic_conflicts"] = len(semantic_conflicts)
        return stats


def _parse_document(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    metadata: dict[str, Any] = {}
    body_start = 0
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                body_start = index + 1
                break
            match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*?)\s*$", lines[index])
            if match:
                value = match.group(2).strip().strip('"\'')
                metadata[match.group(1)] = value
    title = ""
    for line in lines:
        match = re.match(r"^#\s+(.+)$", line)
        if match:
            title = match.group(1).strip()
            break
    metadata.setdefault("title", title)
    for line in lines[:80]:
        for label, key in (("版本", "version"), ("状态", "status"), ("资料性质", "material_nature")):
            match = re.match(rf"^-\s*{label}：\s*(.+?)\s*$", line)
            if match and key not in metadata:
                metadata[key] = match.group(1).strip()
    primary_ids = sorted({match.group(1) for line in lines
                          if (match := PRIMARY_INTERFACE_RE.match(line))})
    chunks = _chunk_lines(lines[body_start:], line_offset=body_start)
    return {"title": title, "metadata": metadata,
            "primary_interface_ids": primary_ids,
            "interface_ids": sorted(set(INTERFACE_RE.findall(text))),
            "systems": _detect_systems(text), "chunks": chunks,
            "claims": _extract_interface_claims(lines)}


def _chunk_lines(lines: list[str], line_offset: int = 0) -> list[dict[str, Any]]:
    sections: list[tuple[list[str], int, int, str]] = []
    headings: list[str] = []
    current: list[str] = []
    current_start = line_offset + 1
    current_heading = "文档开头"
    for number, line in enumerate(lines, line_offset + 1):
        match = HEADING_RE.match(line)
        if match:
            if current and any(item.strip() for item in current):
                sections.append((current, current_start, number - 1, current_heading))
            level = len(match.group(1))
            headings = headings[:level - 1]
            headings.append(match.group(2).strip())
            current_heading = " / ".join(headings)
            current = [line]
            current_start = number
        else:
            current.append(line)
    if current and any(item.strip() for item in current):
        sections.append((current, current_start, line_offset + len(lines), current_heading))

    chunks: list[dict[str, Any]] = []
    for section_lines, start, end, heading in sections:
        part: list[str] = []
        part_start = start
        for offset, line in enumerate(section_lines):
            if part and sum(len(item) + 1 for item in part) + len(line) > MAX_CHUNK_CHARS:
                _append_chunk(chunks, part, part_start, start + offset - 1, heading)
                part = [line]
                part_start = start + offset
            else:
                part.append(line)
        if part and any(item.strip() for item in part):
            _append_chunk(chunks, part, part_start, end, heading)
    return chunks


def _append_chunk(
    chunks: list[dict[str, Any]], lines: list[str], start: int, end: int, heading: str
) -> None:
    text = "\n".join(lines).strip()
    if not text:
        return
    chunks.append({"heading_path": heading, "start_line": start, "end_line": end,
                   "text": text, "content_hash": hashlib.sha256(text.encode()).hexdigest()})


def _infer_doc_type(path: str) -> str:
    mapping = {"03_接口": "interface", "02_需求与规则": "requirement",
               "05_架构": "architecture", "04_数据字典": "data_dictionary"}
    return next((kind for segment, kind in mapping.items() if segment in path), "knowledge")


def _detect_systems(text: str) -> list[str]:
    result = []
    for system, pattern in (("dfbm", r"\bDFBM\b|dfbm-"), ("dcis", r"\bDCIS\b|dcp-dcis"),
                            ("fineract", r"\bFineract\b|dcp-fineract")):
        if re.search(pattern, text, re.I):
            result.append(system)
    return result


def _extract_interface_claims(lines: list[str]) -> list[dict[str, Any]]:
    """Extract conservative, structured interface claims with exact line evidence."""
    claims: list[dict[str, Any]] = []

    def append(field: str, raw: str, normalized: str, line_number: int) -> None:
        claims.append({
            "field_name": field,
            "raw_value": raw.strip(),
            "normalized_value": normalized.strip(),
            "start_line": line_number,
            "end_line": line_number,
        })

    for line_number, line in enumerate(lines, 1):
        contract = re.match(
            r"^\|\s*接口\s*\|\s*`?([A-Za-z]+)\s+([^`|\s]+)`?\s*\|", line
        )
        if contract:
            append("http_method", contract.group(1), contract.group(1).upper(), line_number)
            append("route", contract.group(2), contract.group(2), line_number)
            continue
        request_schema = re.search(r"请求体\s*schema\s*为\s*`([^`]+)`", line, re.I)
        if request_schema:
            append(
                "request_schema", request_schema.group(1),
                request_schema.group(1), line_number,
            )
        success = re.match(r"^\|\s*成功响应\s*\|\s*`?([^`|]+)`?\s*\|", line)
        if success:
            raw = success.group(1).strip()
            status_match = re.search(r"\b([1-5]\d\d)\b", raw)
            if status_match:
                append("success_status", raw, status_match.group(1), line_number)
        downstream = re.match(r"^\|\s*下游依赖\s*\|\s*(.+?)\s*\|\s*$", line)
        if downstream:
            raw = downstream.group(1).strip()
            plain = re.sub(r"[*_`]", "", raw).strip()
            if re.match(r"^(无|none)\b", plain, re.I):
                normalized = "none"
            else:
                systems = _detect_systems(plain)
                normalized = ",".join(systems) if systems else plain.casefold()
            append("downstream_dependency", raw, normalized, line_number)
    return claims


def _assertion_scope(status: str) -> str:
    normalized = status.strip().casefold()
    historical_markers = ("历史", "废弃", "superseded", "obsolete", "deprecated")
    return "historical" if any(marker in normalized for marker in historical_markers) else "current"


def _detect_semantic_conflicts(
    snapshot_id: str, claims: list[dict[str, Any]]
) -> list[tuple[str, ...]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for claim in claims:
        grouped.setdefault((claim["interface_id"], claim["field_name"]), []).append(claim)
    conflicts: list[tuple[str, ...]] = []
    for (interface_id, field_name), items in sorted(grouped.items()):
        current = [item for item in items if item["assertion_scope"] == "current"]
        historical = [item for item in items if item["assertion_scope"] == "historical"]
        current_values = sorted({item["normalized_value"] for item in current})
        historical_values = sorted({item["normalized_value"] for item in historical})
        if len(current_values) > 1:
            selected = current
            kind, severity = "active_field_conflict", "high"
            values = current_values
            description = (
                f"Interface {interface_id} has conflicting current values for "
                f"{field_name}: {', '.join(values)}"
            )
        elif current_values and any(value not in current_values for value in historical_values):
            selected = current + historical
            kind, severity = "historical_drift", "info"
            values = sorted(set(current_values + historical_values))
            description = (
                f"Interface {interface_id} historical {field_name} differs from current value: "
                f"{', '.join(values)}"
            )
        else:
            continue
        claim_ids = sorted(item["claim_id"] for item in selected)
        conflicts.append((
            _stable_id("semanticconflict", snapshot_id, interface_id, field_name, kind),
            snapshot_id, interface_id, field_name, kind, severity,
            json.dumps(values, ensure_ascii=False),
            json.dumps(claim_ids, ensure_ascii=False), description,
        ))
    return conflicts


def _detect_conflicts(snapshot_id: str, documents: list[dict[str, Any]]) -> list[tuple[str, ...]]:
    by_key: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        if document["status"].lower() in {"superseded", "obsolete", "历史", "废弃"}:
            continue
        for key in document["primary"]:
            by_key.setdefault(key, []).append(document)
    conflicts = []
    for key, items in by_key.items():
        if len(items) < 2:
            continue
        doc_ids = sorted(item["doc_id"] for item in items)
        conflicts.append((
            _stable_id("docconflict", snapshot_id, "multiple_active_versions", key), snapshot_id,
            "multiple_active_versions", key, json.dumps(doc_ids, ensure_ascii=False),
            f"Interface {key} has {len(items)} non-superseded primary documents",
        ))
    return conflicts
