from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

from bankdev_agent.config import ProjectConfig
from bankdev_agent.index_store import IndexError, IndexStore


class IndexQuery:
    def __init__(self, config: ProjectConfig, database: Path) -> None:
        self.config = config
        self.store = IndexStore(database)

    def search_interface(self, query: str, limit: int = 10) -> dict[str, Any]:
        self._require_index()
        if limit < 1 or limit > 100:
            raise IndexError("limit must be between 1 and 100")
        term = query.strip()
        if not term:
            raise IndexError("interface query must not be empty")
        with self.store.connect() as connection:
            current = self._current_snapshots(connection)
            matches: list[dict[str, Any]] = []
            if term.isdigit():
                aliases = connection.execute(
                    "SELECT * FROM interface_aliases WHERE interface_id=? ORDER BY document_path",
                    (term,),
                ).fetchall()
                for alias in aliases:
                    for endpoint in connection.execute(
                        "SELECT * FROM endpoints WHERE http_method=? AND route=?",
                        (alias["http_method"], alias["route"]),
                    ).fetchall():
                        if current.get(endpoint["repo_id"]) != endpoint["commit_sha"]:
                            continue
                        matches.append(self._endpoint_payload(endpoint, alias))
            pattern = f"%{term}%"
            for endpoint in connection.execute(
                "SELECT * FROM endpoints WHERE operation_id LIKE ? OR route LIKE ? "
                "ORDER BY operation_id, route LIMIT ?",
                (pattern, pattern, limit * 3),
            ).fetchall():
                if current.get(endpoint["repo_id"]) != endpoint["commit_sha"]:
                    continue
                if any(item["endpoint_id"] == endpoint["endpoint_id"] for item in matches):
                    continue
                matches.append(self._endpoint_payload(endpoint, None))
            matches = matches[:limit]
        return {
            "ok": True,
            "query": term,
            "count": len(matches),
            "matches": matches,
            "snapshots": current,
        }

    def search_code(
        self, query: str, limit: int = 20, repo_ids: list[str] | None = None,
        kinds: list[str] | None = None,
    ) -> dict[str, Any]:
        self._require_index()
        if not 1 <= limit <= 100:
            raise IndexError("limit must be between 1 and 100")
        term = query.strip()
        if not term:
            raise IndexError("code query must not be empty")
        requested_repos = set(repo_ids or [])
        unknown = requested_repos - {repo.id for repo in self.config.repositories}
        if unknown:
            raise IndexError(f"Unknown repository ids: {sorted(unknown)}")
        requested_kinds = set(kinds or [])
        with self.store.connect() as connection:
            current = self._current_snapshots(connection)
            pattern = f"%{term}%"
            candidates: list[tuple[int, dict[str, Any]]] = []
            for row in connection.execute(
                "SELECT symbol_id AS node_id,name,qualified_name,repo_id,commit_sha,kind "
                "FROM symbols WHERE name LIKE ? OR qualified_name LIKE ? LIMIT ?",
                (pattern, pattern, limit * 10),
            ).fetchall():
                if current.get(row["repo_id"]) != row["commit_sha"]:
                    continue
                if requested_repos and row["repo_id"] not in requested_repos:
                    continue
                if requested_kinds and row["kind"] not in requested_kinds:
                    continue
                score = 0 if row["qualified_name"] == term else 1 if row["name"] == term else 2
                node = self._load_node(connection, row["node_id"])
                if node:
                    candidates.append((score, node))
            for row in connection.execute(
                "SELECT endpoint_id AS node_id,operation_id AS name,route AS qualified_name,"
                "repo_id,commit_sha,'endpoint' AS kind FROM endpoints "
                "WHERE operation_id LIKE ? OR route LIKE ? LIMIT ?",
                (pattern, pattern, limit * 5),
            ).fetchall():
                if current.get(row["repo_id"]) != row["commit_sha"]:
                    continue
                if requested_repos and row["repo_id"] not in requested_repos:
                    continue
                if requested_kinds and "endpoint" not in requested_kinds:
                    continue
                score = 0 if row["qualified_name"] == term else 1 if row["name"] == term else 2
                node = self._load_node(connection, row["node_id"])
                if node:
                    candidates.append((score, node))
            unique: dict[str, tuple[int, dict[str, Any]]] = {}
            for item in candidates:
                unique.setdefault(item[1]["id"], item)
            matches = [item[1] for item in sorted(
                unique.values(), key=lambda value: (value[0], value[1]["repo_id"], value[1]["label"])
            )[:limit]]
        return {"ok": True, "query": term, "count": len(matches), "matches": matches,
                "snapshots": current}

    def trace(
        self, start: str, max_depth: int = 8, max_nodes: int = 200,
        direction: str = "downstream",
    ) -> dict[str, Any]:
        self._require_index()
        if not 0 <= max_depth <= 32:
            raise IndexError("max_depth must be between 0 and 32")
        if not 1 <= max_nodes <= 2000:
            raise IndexError("max_nodes must be between 1 and 2000")
        if direction not in {"downstream", "upstream", "both"}:
            raise IndexError("direction must be downstream, upstream or both")
        with self.store.connect() as connection:
            current = self._current_snapshots(connection)
            roots = self._resolve_start(connection, start.strip(), current)
            if not roots:
                raise IndexError(f"No indexed node matches: {start}")
            queue = deque((node_id, 0) for node_id in roots)
            seen: set[str] = set()
            edge_rows: list[Any] = []
            truncated = False
            while queue:
                node_id, depth = queue.popleft()
                if node_id in seen:
                    continue
                if len(seen) >= max_nodes:
                    truncated = True
                    break
                seen.add(node_id)
                if depth >= max_depth:
                    condition = ("source_id=?" if direction == "downstream" else
                                 "target_id=?" if direction == "upstream" else
                                 "(source_id=? OR target_id=?)")
                    arguments = (node_id, node_id) if direction == "both" else (node_id,)
                    for edge in connection.execute("SELECT * FROM edges WHERE " + condition, arguments):
                        neighbor = (edge["source_id"] if direction == "upstream" else
                                    edge["target_id"] if edge["source_id"] == node_id else edge["source_id"])
                        if current.get(edge["repo_id"]) == edge["commit_sha"] and neighbor not in seen:
                            truncated = True
                    continue
                if direction == "downstream":
                    edge_query, edge_args = "SELECT * FROM edges WHERE source_id=? ORDER BY kind,target_id", (node_id,)
                elif direction == "upstream":
                    edge_query, edge_args = "SELECT * FROM edges WHERE target_id=? ORDER BY kind,source_id", (node_id,)
                else:
                    edge_query = "SELECT * FROM edges WHERE source_id=? OR target_id=? ORDER BY kind,target_id"
                    edge_args = (node_id, node_id)
                for edge in connection.execute(edge_query, edge_args).fetchall():
                    if current.get(edge["repo_id"]) != edge["commit_sha"]:
                        continue
                    edge_rows.append(edge)
                    neighbor = (edge["source_id"] if direction == "upstream"
                                else edge["target_id"] if direction == "downstream"
                                else edge["target_id"] if edge["source_id"] == node_id
                                else edge["source_id"])
                    if neighbor not in seen:
                        queue.append((neighbor, depth + 1))
            nodes = [self._load_node(connection, node_id) for node_id in seen]
            nodes = sorted((node for node in nodes if node is not None), key=lambda item: item["id"])
            edges = [self._edge_payload(row) for row in edge_rows
                     if row["source_id"] in seen and row["target_id"] in seen]
            unresolved = [self._unresolved_payload(row) for row in connection.execute(
                "SELECT * FROM unresolved_relations ORDER BY repo_id,evidence_path,evidence_start_line"
            ).fetchall() if row["source_id"] in seen
                          and current.get(row["repo_id"]) == row["commit_sha"]]
        return {
            "ok": True,
            "start": start,
            "roots": roots,
            "direction": direction,
            "nodes": nodes,
            "edges": edges,
            "unresolved": unresolved,
            "coverage": {"node_count": len(nodes), "edge_count": len(edges),
                         "unresolved_count": len(unresolved),
                         "max_depth": max_depth, "truncated": truncated},
            "snapshots": current,
            "limitations": [
                "关系来自固定 Git snapshot 的确定性静态规则，不代表运行时调用证明。",
                "反射、动态 SQL、运行时路由和未配置源码不会被自动补边。",
            ],
        }

    def impact(
        self, start: str, max_depth: int = 8, max_nodes: int = 200,
        include_tests: bool = False,
    ) -> dict[str, Any]:
        traced = self.trace(start, max_depth=max_depth, max_nodes=max_nodes, direction="upstream")
        roots = set(traced["roots"])
        distances = {root: 0 for root in roots}
        frontier = list(roots)
        while frontier:
            target = frontier.pop(0)
            for edge in traced["edges"]:
                if edge["target"] != target or edge["source"] in distances:
                    continue
                distances[edge["source"]] = distances[target] + 1
                frontier.append(edge["source"])
        direct, transitive = [], []
        for node in traced["nodes"]:
            distance = distances.get(node["id"])
            if not distance:
                continue
            path = node["evidence"]["path"]
            if not include_tests and "/test/" in path:
                continue
            item = {**node, "distance": distance,
                    "impact_class": "direct" if distance == 1 else "transitive_candidate"}
            (direct if distance == 1 else transitive).append(item)
        return {
            "ok": True, "start": start, "roots": traced["roots"],
            "direct_impacts": direct, "transitive_candidates": transitive,
            "unresolved": traced["unresolved"], "coverage": traced["coverage"],
            "snapshots": traced["snapshots"],
            "warning": "反向静态依赖表示潜在变更影响；传递项必须结合具体变更内容复核。",
        }

    def _require_index(self) -> None:
        if not self.store.path.is_file():
            raise IndexError(f"Index does not exist: {self.store.path}; run index-code first")
        self.store.initialize()

    def _current_snapshots(self, connection: Any) -> dict[str, str]:
        indexed: dict[str, set[str]] = {}
        for row in connection.execute(
            "SELECT repo_id,commit_sha FROM repository_snapshots"
        ).fetchall():
            indexed.setdefault(row["repo_id"], set()).add(row["commit_sha"])
        configured = {repo.id: repo.baseline_commit for repo in self.config.repositories
                      if repo.kind == "java_source"}
        stale = {repo_id: {"configured": commit, "indexed": indexed.get(repo_id)}
                 for repo_id, commit in configured.items()
                 if repo_id in indexed and commit not in indexed[repo_id]}
        if stale:
            raise IndexError(f"Index contains stale repository snapshots: {stale}")
        return {repo_id: commit for repo_id, commit in configured.items()
                if commit in indexed.get(repo_id, set())}

    def _resolve_start(self, connection: Any, start: str, current: dict[str, str]) -> list[str]:
        if not start:
            raise IndexError("trace start must not be empty")
        if start.isdigit():
            aliases = connection.execute(
                "SELECT http_method,route FROM interface_aliases WHERE interface_id=?", (start,)
            ).fetchall()
            result = []
            for alias in aliases:
                result.extend(row["endpoint_id"] for row in connection.execute(
                    "SELECT endpoint_id,repo_id,commit_sha FROM endpoints WHERE http_method=? AND route=?",
                    (alias["http_method"], alias["route"]),
                ).fetchall() if current.get(row["repo_id"]) == row["commit_sha"])
            if result:
                return sorted(set(result))
        for table, id_column in (("endpoints", "endpoint_id"), ("symbols", "symbol_id"),
                                 ("mapper_statements", "mapper_id"), ("sql_tables", "table_id")):
            rows = connection.execute(
                f"SELECT {id_column},repo_id,commit_sha FROM {table} WHERE {id_column}=?", (start,)
            ).fetchall()
            result = [row[id_column] for row in rows if current.get(row["repo_id"]) == row["commit_sha"]]
            if result:
                return result
        rows = connection.execute(
            "SELECT endpoint_id,repo_id,commit_sha FROM endpoints WHERE operation_id=?", (start,)
        ).fetchall()
        result = [row["endpoint_id"] for row in rows
                  if current.get(row["repo_id"]) == row["commit_sha"]]
        if result:
            return result
        rows = connection.execute(
            "SELECT symbol_id,repo_id,commit_sha FROM symbols WHERE name=? OR qualified_name=?",
            (start, start),
        ).fetchall()
        return [row["symbol_id"] for row in rows
                if current.get(row["repo_id"]) == row["commit_sha"]]

    def _endpoint_payload(self, endpoint: Any, alias: Any | None) -> dict[str, Any]:
        result = {
            "endpoint_id": endpoint["endpoint_id"],
            "repo_id": endpoint["repo_id"],
            "commit": endpoint["commit_sha"],
            "http_method": endpoint["http_method"],
            "route": endpoint["route"],
            "operation_id": endpoint["operation_id"],
            "evidence": self._evidence(endpoint["repo_id"], endpoint["file_path"],
                                       endpoint["start_line"], endpoint["end_line"]),
        }
        if alias is not None:
            result["interface_id"] = alias["interface_id"]
            result["knowledge_evidence"] = self._evidence(
                alias["repo_id"], alias["document_path"], alias["start_line"], alias["end_line"]
            )
        return result

    def _load_node(self, connection: Any, node_id: str) -> dict[str, Any] | None:
        row = connection.execute("SELECT * FROM endpoints WHERE endpoint_id=?", (node_id,)).fetchone()
        if row:
            return {"id": node_id, "kind": "endpoint",
                    "label": f"{row['http_method']} {row['route']} ({row['operation_id']})",
                    "repo_id": row["repo_id"], "commit": row["commit_sha"],
                    "evidence": self._evidence(row["repo_id"], row["file_path"], row["start_line"], row["end_line"])}
        row = connection.execute("SELECT * FROM symbols WHERE symbol_id=?", (node_id,)).fetchone()
        if row:
            return {"id": node_id, "kind": row["kind"], "label": row["qualified_name"],
                    "repo_id": row["repo_id"], "commit": row["commit_sha"],
                    "metadata": json.loads(row["metadata_json"]),
                    "evidence": self._evidence(row["repo_id"], row["file_path"], row["start_line"], row["end_line"])}
        row = connection.execute("SELECT * FROM mapper_statements WHERE mapper_id=?", (node_id,)).fetchone()
        if row:
            return {"id": node_id, "kind": "mapper_statement",
                    "label": f"{row['namespace']}.{row['statement_id']}", "repo_id": row["repo_id"],
                    "commit": row["commit_sha"], "sql_kind": row["sql_kind"],
                    "evidence": self._evidence(row["repo_id"], row["file_path"], row["start_line"], row["end_line"])}
        row = connection.execute("SELECT * FROM sql_tables WHERE table_id=?", (node_id,)).fetchone()
        if row:
            return {"id": node_id, "kind": "sql_table", "label": row["name"],
                    "repo_id": row["repo_id"], "commit": row["commit_sha"],
                    "evidence": self._evidence(row["repo_id"], row["first_file_path"], row["first_line"], row["first_line"])}
        return None

    def _edge_payload(self, row: Any) -> dict[str, Any]:
        return {"id": row["edge_id"], "source": row["source_id"], "target": row["target_id"],
                "kind": row["kind"], "classification": row["classification"],
                "metadata": json.loads(row["metadata_json"]),
                "evidence": self._evidence(row["repo_id"], row["evidence_path"],
                                           row["evidence_start_line"], row["evidence_end_line"])}

    def _unresolved_payload(self, row: Any) -> dict[str, Any]:
        return {"id": row["unresolved_id"], "source": row["source_id"],
                "kind": row["relation_kind"], "expression": row["expression"],
                "reason": row["reason"], "candidates": json.loads(row["candidates_json"]),
                "evidence": self._evidence(row["repo_id"], row["evidence_path"],
                                           row["evidence_start_line"], row["evidence_end_line"])}

    def _evidence(self, repo_id: str, path: str, start: int, end: int) -> dict[str, Any]:
        repository = self.config.repository(repo_id)
        absolute = repository.resolved_root / path
        return {"repo_id": repo_id, "path": path, "start_line": start, "end_line": end,
                "markdown_link": f"[{absolute.name}:{start}](<{absolute}:{start}>)"}
