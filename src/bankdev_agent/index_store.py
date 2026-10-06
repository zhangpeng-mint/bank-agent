from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from bankdev_agent.errors import BankDevError


SCHEMA_VERSION = 4


class IndexError(BankDevError):
    """Raised when the local derived index is invalid or unavailable."""


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    repositories_json TEXT NOT NULL,
    stats_json TEXT
);

CREATE TABLE IF NOT EXISTS repository_snapshots (
    repo_id TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    file_count INTEGER NOT NULL,
    PRIMARY KEY (repo_id, commit_sha)
);

CREATE TABLE IF NOT EXISTS files (
    repo_id TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    path TEXT NOT NULL,
    blob_hash TEXT NOT NULL,
    language TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    cache_hit INTEGER NOT NULL CHECK (cache_hit IN (0, 1)),
    PRIMARY KEY (repo_id, commit_sha, path)
);
CREATE INDEX IF NOT EXISTS idx_files_blob ON files(blob_hash, language);

CREATE TABLE IF NOT EXISTS parse_cache (
    blob_hash TEXT NOT NULL,
    language TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (blob_hash, language, parser_version)
);

CREATE TABLE IF NOT EXISTS symbols (
    symbol_id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    file_path TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_lookup
    ON symbols(repo_id, commit_sha, name, kind);
CREATE INDEX IF NOT EXISTS idx_symbols_qualified
    ON symbols(repo_id, commit_sha, qualified_name);

CREATE TABLE IF NOT EXISTS endpoints (
    endpoint_id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    file_path TEXT NOT NULL,
    http_method TEXT NOT NULL,
    route TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_endpoints_operation
    ON endpoints(repo_id, commit_sha, operation_id);
CREATE INDEX IF NOT EXISTS idx_endpoints_route
    ON endpoints(repo_id, commit_sha, route, http_method);

CREATE TABLE IF NOT EXISTS mapper_statements (
    mapper_id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    file_path TEXT NOT NULL,
    namespace TEXT NOT NULL,
    statement_id TEXT NOT NULL,
    sql_kind TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mapper_lookup
    ON mapper_statements(repo_id, commit_sha, namespace, statement_id);

CREATE TABLE IF NOT EXISTS sql_tables (
    table_id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    name TEXT NOT NULL,
    first_file_path TEXT NOT NULL,
    first_line INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_table_lookup
    ON sql_tables(repo_id, commit_sha, name);

CREATE TABLE IF NOT EXISTS edges (
    edge_id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    classification TEXT NOT NULL,
    evidence_path TEXT NOT NULL,
    evidence_start_line INTEGER NOT NULL,
    evidence_end_line INTEGER NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);

CREATE TABLE IF NOT EXISTS unresolved_relations (
    unresolved_id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    source_id TEXT NOT NULL,
    relation_kind TEXT NOT NULL,
    expression TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_path TEXT NOT NULL,
    evidence_start_line INTEGER NOT NULL,
    evidence_end_line INTEGER NOT NULL,
    candidates_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_unresolved_source ON unresolved_relations(source_id);

CREATE TABLE IF NOT EXISTS interface_aliases (
    interface_id TEXT NOT NULL,
    repo_id TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    http_method TEXT NOT NULL,
    route TEXT NOT NULL,
    document_path TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (interface_id, repo_id, source_revision, document_path)
);
CREATE INDEX IF NOT EXISTS idx_interface_alias ON interface_aliases(interface_id);

CREATE TABLE IF NOT EXISTS document_index_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    repo_id TEXT NOT NULL,
    source_revision TEXT,
    stats_json TEXT
);

CREATE TABLE IF NOT EXISTS document_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    created_at TEXT NOT NULL,
    file_count INTEGER NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    UNIQUE(repo_id, source_revision)
);
CREATE INDEX IF NOT EXISTS idx_document_snapshot_active
    ON document_snapshots(repo_id, active);

CREATE TABLE IF NOT EXISTS document_parse_cache (
    content_hash TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(content_hash, parser_version)
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES document_snapshots(snapshot_id) ON DELETE CASCADE,
    repo_id TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    path TEXT NOT NULL,
    git_blob_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    status TEXT NOT NULL,
    version TEXT NOT NULL,
    material_nature TEXT NOT NULL,
    confidentiality TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    UNIQUE(snapshot_id, path)
);
CREATE INDEX IF NOT EXISTS idx_documents_active_lookup
    ON documents(repo_id, path, status);

CREATE TABLE IF NOT EXISTS document_versions (
    version_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES document_snapshots(snapshot_id) ON DELETE CASCADE,
    doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    logical_key TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    effective_from TEXT,
    supersedes TEXT,
    UNIQUE(snapshot_id, logical_key, version, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_document_versions_key
    ON document_versions(snapshot_id, logical_key, status);

CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    heading_path TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    interface_ids_json TEXT NOT NULL,
    systems_json TEXT NOT NULL,
    UNIQUE(doc_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_document_chunks_doc ON document_chunks(doc_id, ordinal);

CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5(
    chunk_id UNINDEXED,
    title,
    heading_path,
    text,
    tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES document_snapshots(snapshot_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    repo_id TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    path TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    uri TEXT NOT NULL,
    classification TEXT NOT NULL,
    UNIQUE(snapshot_id, path, start_line, end_line, kind)
);

CREATE TABLE IF NOT EXISTS document_conflicts (
    conflict_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES document_snapshots(snapshot_id) ON DELETE CASCADE,
    conflict_kind TEXT NOT NULL,
    conflict_key TEXT NOT NULL,
    document_ids_json TEXT NOT NULL,
    description TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_document_conflicts_snapshot
    ON document_conflicts(snapshot_id, conflict_kind);

CREATE TABLE IF NOT EXISTS document_claims (
    claim_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES document_snapshots(snapshot_id) ON DELETE CASCADE,
    doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    interface_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    raw_value TEXT NOT NULL,
    assertion_scope TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_document_claims_interface
    ON document_claims(snapshot_id, interface_id, field_name, assertion_scope);

CREATE TABLE IF NOT EXISTS semantic_conflicts (
    conflict_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES document_snapshots(snapshot_id) ON DELETE CASCADE,
    interface_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    conflict_kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    values_json TEXT NOT NULL,
    claim_ids_json TEXT NOT NULL,
    description TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_semantic_conflicts_interface
    ON semantic_conflicts(snapshot_id, interface_id, field_name);
"""


class IndexStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.path)
        except sqlite3.Error as exc:
            raise IndexError(f"Cannot open index {self.path}: {exc}") from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA_SQL)
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) in {1, 2, 3} and SCHEMA_VERSION == 4:
                connection.execute(
                    "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) != SCHEMA_VERSION:
                raise IndexError(
                    f"Unsupported index schema {row['value']}; expected {SCHEMA_VERSION}"
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def load_json(value: str) -> Any:
        return json.loads(value)
