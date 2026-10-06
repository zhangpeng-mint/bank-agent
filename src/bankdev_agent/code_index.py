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


PARSER_VERSION = "phase4b-regex-v4"
SUPPORTED_SUFFIXES = {".java": "java", ".xml": "mybatis_xml", ".yaml": "openapi", ".yml": "openapi"}
CLASS_RE = re.compile(r"\b(class|interface|enum|record)\s+(\w+)([^\{;]*)")
FIELD_RE = re.compile(r"\bprivate\s+final\s+([A-Za-z_$][\w$<>?,. ]*)\s+(\w+)\s*;")
CALL_RE = re.compile(r"\b([a-z][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")
MAPPER_RE = re.compile(r'<mapper\s+namespace="([^"]+)"')
STATEMENT_RE = re.compile(r'<(select|insert|update|delete)\s+[^>]*\bid="([^"]+)"', re.I)
TABLE_PATTERNS = {
    "READS": re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_.$]*)", re.I),
    "WRITES": re.compile(r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([A-Za-z_][A-Za-z0-9_.$]*)", re.I),
}


class CodeIndexError(BankDevError):
    """Raised when a fixed-snapshot source index cannot be built."""


@dataclass(frozen=True)
class GitTreeFile:
    path: str
    blob_hash: str
    size: int


def _stable_id(kind: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:24]
    return f"{kind}:{digest}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_consumer_contract(path: str) -> bool:
    normalized = path.lower()
    return "/contract/" in normalized or normalized.endswith("/fineract-api.yaml")


def _routes_equivalent(left: str, right: str) -> bool:
    def forms(route: str) -> set[str]:
        normalized = "/" + route.strip("/")
        result = {normalized.rstrip("/") or "/"}
        if normalized.startswith("/v1/"):
            result.add(normalized[3:])
        return result

    return bool(forms(left) & forms(right))


class CodeIndexer:
    """Build a deterministic SQLite index from configured Git commits."""

    def __init__(self, config: ProjectConfig, database: Path) -> None:
        self.config = config
        self.store = IndexStore(database)

    def index(self, repo_ids: list[str] | None = None) -> dict[str, Any]:
        selected = self._select_repositories(repo_ids)
        self.store.initialize()
        started_at = _now()
        repositories_json = self.store.json([repo.id for repo in selected])
        with self.store.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO index_runs(started_at,status,repositories_json) VALUES (?, 'running', ?)",
                (started_at, repositories_json),
            )
            run_id = int(cursor.lastrowid)

        totals = {"files_scanned": 0, "files_parsed": 0, "cache_hits": 0, "symbols": 0,
                  "endpoints": 0, "mapper_statements": 0, "tables": 0, "edges": 0,
                  "unresolved": 0, "cross_repo_edges": 0, "interface_aliases": 0}
        details: list[dict[str, Any]] = []
        try:
            for repository in selected:
                if repository.kind != "java_source":
                    continue
                stats = self._index_repository(repository)
                details.append(stats)
                for key in totals:
                    totals[key] += int(stats.get(key, 0))
            knowledge = next((repo for repo in selected if repo.kind == "git_markdown"), None)
            if knowledge is None:
                knowledge = next(
                    (repo for repo in self.config.repositories if repo.kind == "git_markdown"), None
                )
            if knowledge is not None:
                totals["interface_aliases"] = self._index_aliases(knowledge)
            cross_edges, cross_unresolved = self._derive_cross_repo_edges()
            totals["cross_repo_edges"] = cross_edges
            totals["edges"] += cross_edges
            totals["unresolved"] += cross_unresolved
            stats_payload = {**totals, "repositories": details}
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE index_runs SET finished_at=?, status='complete', stats_json=? WHERE run_id=?",
                    (_now(), self.store.json(stats_payload), run_id),
                )
        except Exception as exc:
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE index_runs SET finished_at=?, status='failed', stats_json=? WHERE run_id=?",
                    (_now(), self.store.json({"error": str(exc)}), run_id),
                )
            if isinstance(exc, BankDevError):
                raise
            raise CodeIndexError(f"Index run {run_id} failed: {exc}") from exc
        return {
            "ok": True,
            "schema_version": SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
            "database": str(self.store.path),
            "run_id": run_id,
            "stats": stats_payload,
        }

    def _select_repositories(self, repo_ids: list[str] | None) -> list[RepositoryConfig]:
        if repo_ids:
            selected = [self.config.repository(repo_id) for repo_id in repo_ids]
        else:
            selected = list(self.config.repositories)
        return selected

    def _index_repository(self, repository: RepositoryConfig) -> dict[str, Any]:
        entries = self._tree_files(repository)
        stats: dict[str, Any] = {
            "repo_id": repository.id,
            "commit": repository.baseline_commit,
            "files_scanned": len(entries),
            "files_parsed": 0,
            "cache_hits": 0,
            "symbols": 0,
            "endpoints": 0,
            "mapper_statements": 0,
            "tables": 0,
            "edges": 0,
            "unresolved": 0,
        }
        with self.store.transaction() as connection:
            self._clear_snapshot(connection, repository)
            for entry in entries:
                language = SUPPORTED_SUFFIXES[Path(entry.path).suffix.lower()]
                cached = connection.execute(
                    "SELECT payload_json FROM parse_cache WHERE blob_hash=? AND language=? AND parser_version=?",
                    (entry.blob_hash, language, PARSER_VERSION),
                ).fetchone()
                if cached is None:
                    text = self._read_blob(repository, entry).decode("utf-8-sig", errors="replace")
                    payload = self._parse(language, text)
                    connection.execute(
                        "INSERT INTO parse_cache(blob_hash,language,parser_version,payload_json) VALUES (?,?,?,?)",
                        (entry.blob_hash, language, PARSER_VERSION, self.store.json(payload)),
                    )
                    cache_hit = 0
                    stats["files_parsed"] += 1
                else:
                    payload = json.loads(cached["payload_json"])
                    cache_hit = 1
                    stats["cache_hits"] += 1
                connection.execute(
                    "INSERT INTO files(repo_id,commit_sha,path,blob_hash,language,byte_size,cache_hit) VALUES (?,?,?,?,?,?,?)",
                    (repository.id, repository.baseline_commit, entry.path, entry.blob_hash,
                     language, entry.size, cache_hit),
                )
                self._insert_payload(connection, repository, entry.path, payload, stats)
            stats["edges"], stats["unresolved"] = self._derive_edges(connection, repository)
            connection.execute(
                "INSERT INTO repository_snapshots(repo_id,commit_sha,indexed_at,file_count) VALUES (?,?,?,?) "
                "ON CONFLICT(repo_id,commit_sha) DO UPDATE SET indexed_at=excluded.indexed_at,file_count=excluded.file_count",
                (repository.id, repository.baseline_commit, _now(), len(entries)),
            )
        return stats

    @staticmethod
    def _clear_snapshot(connection: Any, repository: RepositoryConfig) -> None:
        args = (repository.id, repository.baseline_commit)
        for table in ("edges", "unresolved_relations", "symbols", "endpoints", "mapper_statements", "sql_tables", "files"):
            connection.execute(f"DELETE FROM {table} WHERE repo_id=? AND commit_sha=?", args)

    def _tree_files(self, repository: RepositoryConfig) -> list[GitTreeFile]:
        output = self._git(repository, "ls-tree", "-r", "-l", repository.baseline_commit)
        result: list[GitTreeFile] = []
        for line in output.decode(errors="replace").splitlines():
            if "\t" not in line:
                continue
            metadata, path = line.split("\t", 1)
            fields = metadata.split()
            if len(fields) != 4 or fields[1] != "blob":
                continue
            suffix = Path(path).suffix.lower()
            if suffix in SUPPORTED_SUFFIXES and repository.is_included(path):
                result.append(GitTreeFile(path=path, blob_hash=fields[2], size=int(fields[3])))
        return result

    def _read_blob(self, repository: RepositoryConfig, entry: GitTreeFile) -> bytes:
        candidate = repository.resolved_root / entry.path
        if candidate.is_file() and not candidate.is_symlink():
            data = candidate.read_bytes()
            header = f"blob {len(data)}\0".encode()
            if hashlib.sha1(header + data).hexdigest() == entry.blob_hash:
                return data
        return self._git(repository, "show", f"{repository.baseline_commit}:{entry.path}")

    @staticmethod
    def _git(repository: RepositoryConfig, *args: str) -> bytes:
        try:
            return subprocess.run(
                ["git", "-C", str(repository.resolved_root), *args],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout
        except subprocess.CalledProcessError as exc:
            message = exc.stderr.decode(errors="replace").strip()
            raise CodeIndexError(f"Git read failed for {repository.id}: {message}") from exc

    @staticmethod
    def _parse(language: str, text: str) -> dict[str, Any]:
        if language == "java":
            return _parse_java(text)
        if language == "mybatis_xml":
            return _parse_xml(text)
        if language == "openapi":
            return _parse_openapi(text)
        return {}

    def _insert_payload(
        self, connection: Any, repository: RepositoryConfig, path: str,
        payload: dict[str, Any], stats: dict[str, Any],
    ) -> None:
        commit = repository.baseline_commit
        for symbol in payload.get("symbols", []):
            symbol_id = _stable_id("symbol", repository.id, commit, path,
                                   symbol["qualified_name"], str(symbol["start_line"]))
            connection.execute(
                "INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
                (symbol_id, repository.id, commit, path, symbol["kind"], symbol["name"],
                 symbol["qualified_name"], symbol["start_line"], symbol["end_line"],
                 self.store.json(symbol.get("metadata", {}))),
            )
            stats["symbols"] += 1
        for endpoint in payload.get("endpoints", []):
            endpoint_id = _stable_id("endpoint", repository.id, commit, path, endpoint["method"],
                                     endpoint["route"], endpoint["operation_id"])
            connection.execute(
                "INSERT INTO endpoints VALUES (?,?,?,?,?,?,?,?,?,?)",
                (endpoint_id, repository.id, commit, path, endpoint["method"], endpoint["route"],
                 endpoint["operation_id"], endpoint["start_line"], endpoint["end_line"],
                 self.store.json(endpoint.get("metadata", {}))),
            )
            stats["endpoints"] += 1
        for mapper in payload.get("mappers", []):
            mapper_id = _stable_id("mapper", repository.id, commit, mapper["namespace"], mapper["statement_id"])
            connection.execute(
                "INSERT INTO mapper_statements VALUES (?,?,?,?,?,?,?,?,?,?)",
                (mapper_id, repository.id, commit, path, mapper["namespace"], mapper["statement_id"],
                 mapper["sql_kind"], mapper["start_line"], mapper["end_line"],
                 self.store.json(mapper.get("metadata", {}))),
            )
            stats["mapper_statements"] += 1
            for table in mapper.get("metadata", {}).get("tables", []):
                table_id = _stable_id("table", repository.id, commit, table["name"].lower())
                changed = connection.execute(
                    "INSERT OR IGNORE INTO sql_tables VALUES (?,?,?,?,?,?)",
                    (table_id, repository.id, commit, table["name"].lower(), path, mapper["start_line"]),
                ).rowcount
                stats["tables"] += changed

    def _derive_edges(self, connection: Any, repository: RepositoryConfig) -> tuple[int, int]:
        args = (repository.id, repository.baseline_commit)
        symbol_rows = connection.execute(
            "SELECT * FROM symbols WHERE repo_id=? AND commit_sha=?", args
        ).fetchall()
        methods = [row for row in symbol_rows if row["kind"] == "method"]
        types = [row for row in symbol_rows if row["kind"] in {"class", "interface", "enum", "record"}]
        method_by_owner_name: dict[tuple[str, str], list[Any]] = {}
        implementations: dict[str, list[str]] = {}
        type_names: dict[str, list[str]] = {}
        for row in methods:
            meta = json.loads(row["metadata_json"])
            method_by_owner_name.setdefault((meta.get("owner", ""), row["name"]), []).append(row)
        for row in types:
            metadata = json.loads(row["metadata_json"])
            type_names.setdefault(row["name"], []).append(row["qualified_name"])
            for interface_name in metadata.get("implements", []):
                implementations.setdefault(interface_name, []).append(row["qualified_name"])
        edges: dict[str, tuple[Any, ...]] = {}
        unresolved: dict[str, tuple[Any, ...]] = {}

        def add(source: str, target: str, kind: str, path: str, start: int, end: int,
                metadata: dict[str, Any] | None = None) -> None:
            edge_id = _stable_id("edge", source, target, kind)
            edges[edge_id] = (edge_id, repository.id, repository.baseline_commit, source, target,
                              kind, "confirmed_static", path, start, end,
                              self.store.json(metadata or {}))

        def add_unresolved(source: str, kind: str, expression: str, reason: str,
                           path: str, line: int, candidates: list[str]) -> None:
            unresolved_id = _stable_id("unresolved", source, kind, expression, str(line))
            unresolved[unresolved_id] = (
                unresolved_id, repository.id, repository.baseline_commit, source, kind,
                expression, reason, path, line, line, self.store.json(candidates),
            )

        endpoints = connection.execute(
            "SELECT * FROM endpoints WHERE repo_id=? AND commit_sha=?", args
        ).fetchall()
        for endpoint in endpoints:
            endpoint_meta = json.loads(endpoint["metadata_json"])
            candidates = []
            for method in methods:
                meta = json.loads(method["metadata_json"])
                expected_method = endpoint_meta.get("implementation_method", endpoint["operation_id"])
                expected_owner = endpoint_meta.get("owner")
                owner_matches = (
                    meta.get("owner") == expected_owner if expected_owner
                    else meta.get("owner", "").endswith("Controller")
                )
                if method["name"] == expected_method and owner_matches:
                    candidates.append(method)
            if len(candidates) == 1:
                target = candidates[0]
                add(endpoint["endpoint_id"], target["symbol_id"], "IMPLEMENTED_BY",
                    target["file_path"], target["start_line"], target["end_line"])

        mappers = connection.execute(
            "SELECT * FROM mapper_statements WHERE repo_id=? AND commit_sha=?", args
        ).fetchall()
        for method in methods:
            meta = json.loads(method["metadata_json"])
            owner = meta.get("owner", "")
            owner_type = next((row for row in types if row["qualified_name"] == owner), None)
            owner_meta = json.loads(owner_type["metadata_json"]) if owner_type else {}
            fields = {field["name"]: field["type"].split("<", 1)[0].split(".")[-1]
                      for field in owner_meta.get("fields", [])}
            for call in meta.get("calls", []):
                field_type = fields.get(call["receiver"])
                if not field_type:
                    continue
                if field_type.endswith("Mapper"):
                    candidates = [row for row in mappers
                                  if row["namespace"].split(".")[-1] == field_type
                                  and row["statement_id"] == call["method"]]
                    if len(candidates) == 1:
                        add(method["symbol_id"], candidates[0]["mapper_id"], "CALLS",
                            method["file_path"], call["line"], call["line"],
                            {"receiver_type": field_type})
                    elif len(candidates) != 1:
                        add_unresolved(
                            method["symbol_id"], "CALLS",
                            f"{call['receiver']}.{call['method']}",
                            "mapper_target_not_unique" if candidates else "mapper_target_not_found",
                            method["file_path"], call["line"],
                            [candidate["mapper_id"] for candidate in candidates],
                        )
                    continue
                targets: list[Any] = []
                implementation_names = implementations.get(field_type, [])
                candidate_owners = implementation_names or type_names.get(field_type, [])
                for candidate_owner in candidate_owners:
                    targets.extend(method_by_owner_name.get((candidate_owner, call["method"]), []))
                targets = list({target["symbol_id"]: target for target in targets}.values())
                if len(targets) == 1:
                    add(method["symbol_id"], targets[0]["symbol_id"], "CALLS",
                        method["file_path"], call["line"], call["line"],
                        {"receiver_type": field_type})
                elif field_type.endswith(("Service", "Sao", "Client", "Gateway")):
                    add_unresolved(
                        method["symbol_id"], "CALLS",
                        f"{call['receiver']}.{call['method']}",
                        "multiple_implementations" if targets else "implementation_not_found",
                        method["file_path"], call["line"],
                        [target["symbol_id"] for target in targets],
                    )

        tables = {row["name"]: row for row in connection.execute(
            "SELECT * FROM sql_tables WHERE repo_id=? AND commit_sha=?", args
        ).fetchall()}
        for mapper in mappers:
            metadata = json.loads(mapper["metadata_json"])
            for table in metadata.get("tables", []):
                target = tables.get(table["name"].lower())
                if target:
                    add(mapper["mapper_id"], target["table_id"], table["access"],
                        mapper["file_path"], mapper["start_line"], mapper["end_line"])
        connection.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?,?,?)", edges.values())
        connection.executemany(
            "INSERT INTO unresolved_relations VALUES (?,?,?,?,?,?,?,?,?,?,?)", unresolved.values()
        )
        return len(edges), len(unresolved)

    def _derive_cross_repo_edges(self) -> tuple[int, int]:
        current = {repo.id: repo.baseline_commit for repo in self.config.repositories
                   if repo.kind == "java_source"}
        with self.store.transaction() as connection:
            connection.execute("DELETE FROM edges WHERE kind='INVOKES_REMOTE'")
            connection.execute("DELETE FROM unresolved_relations WHERE relation_kind='INVOKES_REMOTE'")
            endpoints = [row for row in connection.execute("SELECT * FROM endpoints").fetchall()
                         if current.get(row["repo_id"]) == row["commit_sha"]]
            consumers = [row for row in endpoints if _is_consumer_contract(row["file_path"])]
            providers = [row for row in endpoints if not _is_consumer_contract(row["file_path"])]
            methods = [row for row in connection.execute(
                "SELECT * FROM symbols WHERE kind='method'"
            ).fetchall() if current.get(row["repo_id"]) == row["commit_sha"]]
            field_types: dict[tuple[str, str], str] = {}
            for type_row in connection.execute(
                "SELECT * FROM symbols WHERE kind IN ('class','interface','record','enum')"
            ).fetchall():
                if current.get(type_row["repo_id"]) != type_row["commit_sha"]:
                    continue
                for field in json.loads(type_row["metadata_json"]).get("fields", []):
                    field_types[(type_row["qualified_name"], field["name"])] = (
                        field["type"].split("<", 1)[0].split(".")[-1]
                    )
            edges: dict[str, tuple[Any, ...]] = {}
            unresolved: dict[str, tuple[Any, ...]] = {}

            def link(source: Any, signature: Any, call: dict[str, Any]) -> None:
                candidates = [provider for provider in providers
                              if provider["repo_id"] != source["repo_id"]
                              and provider["http_method"] == signature["http_method"]
                              and provider["operation_id"] == signature["operation_id"]
                              and _routes_equivalent(signature["route"], provider["route"])]
                expression = f"{signature['http_method']} {signature['route']} ({signature['operation_id']})"
                if len(candidates) == 1:
                    target = candidates[0]
                    edge_id = _stable_id("edge", source["symbol_id"], target["endpoint_id"], "INVOKES_REMOTE")
                    edges[edge_id] = (
                        edge_id, source["repo_id"], source["commit_sha"], source["symbol_id"],
                        target["endpoint_id"], "INVOKES_REMOTE", "confirmed_static",
                        source["file_path"], call["line"], call["line"],
                        self.store.json({
                            "signature": expression,
                            "consumer_contract": signature["file_path"],
                            "target_repo": target["repo_id"],
                            "reason": "consumer contract and provider operation signature match",
                            "extractor": f"cross-repo-signature@{PARSER_VERSION}",
                            "confidence": 1.0,
                        }),
                    )
                else:
                    unresolved_id = _stable_id(
                        "unresolved", source["symbol_id"], "INVOKES_REMOTE", expression, str(call["line"])
                    )
                    unresolved[unresolved_id] = (
                        unresolved_id, source["repo_id"], source["commit_sha"], source["symbol_id"],
                        "INVOKES_REMOTE", expression,
                        "provider_not_found" if not candidates else "provider_not_unique",
                        source["file_path"], call["line"], call["line"],
                        self.store.json([candidate["endpoint_id"] for candidate in candidates]),
                    )

            for source in methods:
                metadata = json.loads(source["metadata_json"])
                call_signatures: list[tuple[Any, dict[str, Any]]] = []
                for call in metadata.get("calls", []):
                    receiver_type = field_types.get((metadata.get("owner", ""), call["receiver"]), "")
                    if not receiver_type.endswith("Api"):
                        continue
                    for contract in consumers:
                        if (contract["repo_id"] == source["repo_id"]
                                and contract["operation_id"] == call["method"]):
                            call_signatures.append((contract, call))
                for remote_call in metadata.get("remote_calls", []):
                    for contract in consumers:
                        if (contract["repo_id"] == source["repo_id"]
                                and contract["http_method"] == remote_call["http_method"]
                                and _routes_equivalent(contract["route"], remote_call["route"])):
                            call_signatures.append((contract, remote_call))
                seen: set[tuple[str, int]] = set()
                for signature, call in call_signatures:
                    key = (signature["endpoint_id"], call["line"])
                    if key not in seen:
                        seen.add(key)
                        link(source, signature, call)
            connection.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?,?,?)", edges.values())
            connection.executemany(
                "INSERT INTO unresolved_relations VALUES (?,?,?,?,?,?,?,?,?,?,?)", unresolved.values()
            )
        return len(edges), len(unresolved)

    def _index_aliases(self, repository: RepositoryConfig) -> int:
        aliases: list[tuple[Any, ...]] = []
        if repository.content_root is None:
            return 0
        for path in repository.content_root.rglob("*.md"):
            relative = repository.relative_name(path)
            if not repository.is_included(relative) or repository.is_demo(relative):
                continue
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            ids = [(number, match.group(1)) for number, line in enumerate(lines, 1)
                   if (match := re.match(r"^\| 原服务号 \| `?(\d{8})`? \|$", line))]
            contracts = [(number, match.group(1), match.group(2)) for number, line in enumerate(lines, 1)
                         if (match := re.match(r"^\| 接口 \| `([A-Z]+) ([^`]+)` \|$", line))]
            for id_line, interface_id in ids:
                if not contracts:
                    continue
                contract_line, method, route = min(contracts, key=lambda item: abs(item[0] - id_line))
                source_revision = "sha256:" + hashlib.sha256(
                    "\n".join(lines).encode("utf-8")
                ).hexdigest()
                aliases.append((interface_id, repository.id, source_revision, method, route,
                                relative, min(id_line, contract_line), max(id_line, contract_line), "current"))
        with self.store.transaction() as connection:
            connection.execute("DELETE FROM interface_aliases WHERE repo_id=?", (repository.id,))
            connection.executemany("INSERT INTO interface_aliases VALUES (?,?,?,?,?,?,?,?,?)", aliases)
        return len(aliases)


def _parse_java(text: str) -> dict[str, Any]:
    package_match = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", text)
    package = package_match.group(1) if package_match else ""
    symbols: list[dict[str, Any]] = []
    endpoints: list[dict[str, Any]] = []
    classes = list(CLASS_RE.finditer(text))
    for class_match in classes:
        kind, name, tail = class_match.groups()
        start_line = text.count("\n", 0, class_match.start()) + 1
        implements_match = re.search(r"\bimplements\s+([^\{]+)", tail)
        implements = []
        if implements_match:
            implements = [part.strip().split("<", 1)[0].split(".")[-1]
                          for part in implements_match.group(1).split(",")]
        qualified = f"{package}.{name}" if package else name
        symbols.append({"kind": kind, "name": name, "qualified_name": qualified,
                        "start_line": start_line, "end_line": start_line,
                        "metadata": {"implements": implements, "fields": []}})
    if not classes:
        return {"symbols": [], "endpoints": []}
    owner = symbols[0]
    class_annotations = text[max(0, classes[0].start() - 1200):classes[0].start()]
    class_paths = re.findall(r'(?m)^\s*@Path\("([^"]*)"\)', class_annotations)
    class_route = class_paths[-1] if class_paths else ""
    owner["metadata"]["fields"] = [
        {"type": match.group(1).strip(), "name": match.group(2)} for match in FIELD_RE.finditer(text)
    ]
    declaration_pattern = re.compile(
        r"(?m)^[ \t]*(?:(?:public|protected|private|static|final|abstract|default|synchronized)\s+)+"
        r"(?:<[^>]+>\s+)?[A-Za-z_$][\w$<>, ?.\[\]]*?\s+([A-Za-z_$][\w$]*)\s*\("
    )
    if owner["kind"] == "interface":
        declaration_pattern = re.compile(
            r"(?m)^[ \t]*(?:(?:public|default|static|abstract)\s+)*"
            r"[A-Za-z_$][\w$<>, ?.\[\]]*?\s+([A-Za-z_$][\w$]*)\s*\("
        )
    previous_end = classes[0].end()
    for match in declaration_pattern.finditer(text):
        name = match.group(1)
        paren_start = match.end() - 1
        depth = 1
        position = paren_start + 1
        while position < len(text) and depth:
            depth += (text[position] == "(") - (text[position] == ")")
            position += 1
        if depth:
            continue
        while position < len(text) and text[position] not in "{;":
            position += 1
        if position >= len(text):
            continue
        terminator = text[position]
        start_line = text.count("\n", 0, match.start()) + 1
        end_offset = position + 1
        if terminator == "{":
            depth = 1
            position += 1
            while position < len(text) and depth:
                depth += (text[position] == "{") - (text[position] == "}")
                position += 1
            end_offset = position
        end_line = text.count("\n", 0, end_offset) + 1
        body = text[match.start():end_offset]
        annotation_start = max(previous_end, text.rfind("\n}", previous_end, match.start()) + 2)
        annotation_text = text[annotation_start:match.start()]
        calls = [{"receiver": call.group(1), "method": call.group(2),
                  "line": start_line + body.count("\n", 0, call.start())}
                 for call in CALL_RE.finditer(body)]
        remote_calls = []
        for remote in re.finditer(
            r"\b(postJson|getJson)\s*\(\s*[^,]+,\s*\"([^\"]+)\"", body, re.S
        ):
            remote_calls.append({
                "http_method": "POST" if remote.group(1) == "postJson" else "GET",
                "route": remote.group(2),
                "line": start_line + body.count("\n", 0, remote.start()),
            })
        symbols.append({"kind": "method", "name": name,
                        "qualified_name": f"{owner['qualified_name']}.{name}",
                        "start_line": start_line, "end_line": end_line,
                        "metadata": {"owner": owner["qualified_name"], "calls": calls,
                                     "remote_calls": remote_calls}})
        http_annotations = re.findall(r"(?m)^\s*@(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", annotation_text)
        if http_annotations:
            method_paths = re.findall(r'(?m)^\s*@Path\("([^"]*)"\)', annotation_text)
            method_route = method_paths[-1] if method_paths else ""
            route = "/" + "/".join(
                part.strip("/") for part in (class_route, method_route) if part.strip("/")
            )
            operation_ids = re.findall(r'operationId\s*=\s*"([^"]+)"', annotation_text)
            operation_id = operation_ids[-1] if operation_ids else name
            annotation_line = text.count("\n", 0, annotation_start) + 1
            endpoints.append({
                "route": route or "/", "method": http_annotations[-1],
                "operation_id": operation_id, "start_line": annotation_line,
                "end_line": end_line,
                "metadata": {"owner": owner["qualified_name"],
                             "implementation_method": name, "source": "jaxrs_annotation"},
            })
        previous_end = end_offset
    return {"symbols": symbols, "endpoints": endpoints}


def _parse_openapi(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    endpoints: list[dict[str, Any]] = []
    current_route: tuple[str, int, int] | None = None
    current_method: tuple[str, int, int] | None = None
    for number, line in enumerate(lines, 1):
        route = re.match(r"^(\s{2})(/[^:]+):\s*$", line)
        if route:
            current_route = (route.group(2), number, len(route.group(1)))
            current_method = None
            continue
        method = re.match(r"^(\s+)(get|post|put|patch|delete|head|options):\s*$", line, re.I)
        if method and current_route and len(method.group(1)) > current_route[2]:
            current_method = (method.group(2).upper(), number, len(method.group(1)))
            continue
        operation = re.match(r"^(\s+)operationId:\s*([A-Za-z_$][\w$]*)\s*$", line)
        if operation and current_route and current_method and len(operation.group(1)) > current_method[2]:
            refs: list[str] = []
            end = number
            for lookahead in range(number, min(len(lines), number + 80)):
                candidate = lines[lookahead]
                indent = len(candidate) - len(candidate.lstrip())
                if candidate.strip() and indent <= current_method[2] and lookahead + 1 > number:
                    break
                refs.extend(re.findall(r"#/components/schemas/([A-Za-z_$][\w$]*)", candidate))
                end = lookahead + 1
            endpoints.append({"route": current_route[0], "method": current_method[0],
                              "operation_id": operation.group(2), "start_line": current_route[1],
                              "end_line": end, "metadata": {"schema_refs": sorted(set(refs))}})
    return {"endpoints": endpoints}


def _parse_xml(text: str) -> dict[str, Any]:
    namespace_match = MAPPER_RE.search(text)
    if namespace_match is None:
        return {"mappers": []}
    namespace = namespace_match.group(1)
    mappers: list[dict[str, Any]] = []
    for match in STATEMENT_RE.finditer(text):
        kind = match.group(1).lower()
        closing = re.search(rf"</{kind}\s*>", text[match.end():], re.I)
        end_offset = match.end() + closing.end() if closing else match.end()
        sql = text[match.start():end_offset]
        tables: list[dict[str, str]] = []
        for access, pattern in TABLE_PATTERNS.items():
            for table_match in pattern.finditer(sql):
                item = {"name": table_match.group(1), "access": access}
                if item not in tables:
                    tables.append(item)
        start_line = text.count("\n", 0, match.start()) + 1
        end_line = text.count("\n", 0, end_offset) + 1
        mappers.append({"namespace": namespace, "statement_id": match.group(2), "sql_kind": kind,
                        "start_line": start_line, "end_line": end_line,
                        "metadata": {"tables": tables}})
    return {"mappers": mappers}
