"""Loopback-only UI adapter over the existing analysis services (standard library)."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from bankdev_agent.code_index import CodeIndexer
from bankdev_agent.errors import BankDevError
from bankdev_agent.git_snapshot import GitSnapshotInspector
from bankdev_agent.index_query import IndexQuery
from bankdev_agent.index_store import IndexStore
from bankdev_agent.knowledge_index import KnowledgeIndexer
from bankdev_agent.knowledge_query import KnowledgeQuery
from bankdev_agent.knowledge_conflicts import KnowledgeConflictQuery
from bankdev_agent.model_analysis import AssistedRequirementAnalyzer
from bankdev_agent.model_gateway import DEFAULT_MODEL, MockBackend, ModelGateway, ZhipuBackend
from bankdev_agent.requirement_analysis import RequirementAnalyzer, RequirementFrame, render_requirement_markdown


class WebError(BankDevError):
    pass


def text_field(data, key, default="", maximum=4000):
    value = data.get(key, default)
    if not isinstance(value, str) or len(value) > maximum or "\0" in value:
        raise WebError(f"无效参数：{key}")
    return value.strip()


def number_field(data, key, default, low, high):
    value = data.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise WebError(f"参数 {key} 必须在 {low}–{high} 之间")
    return value


class WebApplication:
    ACTIONS = {"search-knowledge", "search-interface", "search-code", "trace", "impact",
               "detect-conflicts", "analyze-requirement", "snapshots", "index-code", "index-docs"}

    def __init__(self, config, database):
        RequirementAnalyzer(config, database)  # Reuse var/ scope enforcement.
        self.config, self.database = config, database
        self.token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.evidence = {}
        self.lock = threading.RLock()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bankdev-web")

    def close(self):
        self.worker.shutdown(wait=True)

    def status(self):
        counts = {"files": 0, "symbols": 0, "endpoints": 0, "documents": 0, "document_chunks": 0}
        indexed = []
        error = None
        if self.database.is_file():
            try:
                with sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=1) as con:
                    con.row_factory = sqlite3.Row
                    code_repositories = [r for r in self.config.repositories if r.kind == "java_source"]
                    scope = " OR ".join("(repo_id=? AND commit_sha=?)" for _ in code_repositories) or "0"
                    parameters = [v for r in code_repositories for v in (r.id, r.baseline_commit)]
                    for table in ("files", "symbols", "endpoints"):
                        counts[table] = con.execute(f"SELECT count(*) FROM {table} WHERE {scope}", parameters).fetchone()[0]
                    counts["documents"] = con.execute("SELECT count(*) FROM documents d JOIN document_snapshots s ON d.snapshot_id=s.snapshot_id WHERE s.active=1").fetchone()[0]
                    counts["document_chunks"] = con.execute("SELECT count(*) FROM document_chunks c JOIN documents d ON c.doc_id=d.doc_id JOIN document_snapshots s ON d.snapshot_id=s.snapshot_id WHERE s.active=1").fetchone()[0]
                    indexed = [dict(row) for row in con.execute("SELECT repo_id,commit_sha,file_count FROM repository_snapshots ORDER BY repo_id")]
            except sqlite3.Error:
                error = "索引暂不可读，请完成索引更新后刷新。"
        with self.lock:
            jobs = [{k: v for k, v in job.items() if k in {"id", "action", "status", "error"}}
                    for job in reversed(list(self.jobs.values()))]
        return {"counts": counts, "indexed": indexed, "index_exists": self.database.is_file(), "error": error,
                "repositories": [{"id": r.id, "name": r.display_name, "kind": r.kind,
                                  "commit": r.baseline_commit} for r in self.config.repositories],
                "model_key_available": bool(os.environ.get("BANKDEV_MODEL_API_KEY")),
                "default_model": DEFAULT_MODEL, "jobs": jobs}

    def submit(self, action, arguments):
        if not isinstance(action, str) or action not in self.ACTIONS or not isinstance(arguments, dict):
            raise WebError("不支持的操作")
        with self.lock:
            if any(j["status"] in {"queued", "running"} for j in self.jobs.values()):
                raise WebError("已有任务正在执行，请稍后再试。")
            if len(self.jobs) >= 20:
                old_id = next(iter(self.jobs))
                self.jobs.pop(old_id)
                self.evidence = {key: entry for key, entry in self.evidence.items() if entry["job"] != old_id}
            job_id = secrets.token_hex(12)
            self.jobs[job_id] = {"id": job_id, "action": action, "status": "queued"}
            self.worker.submit(self._run, job_id, action, copy.deepcopy(arguments))
            return {"id": job_id, "status": "queued"}

    def _run(self, job_id, action, arguments):
        with self.lock:
            self.jobs[job_id]["status"] = "running"
        try:
            result = self.execute(action, arguments)
            with self.lock:
                presentation = copy.deepcopy(result)
                self._decorate(presentation, job_id)
                self.jobs[job_id].update(status="completed", result=result, presentation=presentation)
        except (BankDevError, OSError, ValueError, sqlite3.Error) as exc:
            with self.lock:
                self.jobs[job_id].update(status="failed", error=str(exc))
        except Exception:
            # Never expose stack traces, environment values or provider response bodies.
            with self.lock:
                self.jobs[job_id].update(status="failed", error="任务执行失败，请检查本机配置和索引。")

    def execute(self, action, args):
        query = IndexQuery(self.config, self.database)
        term = text_field(args, "query", maximum=200)
        if action.startswith("search-"):
            if not term:
                raise WebError("请输入检索词")
            if action == "search-knowledge":
                return KnowledgeQuery(self.config, self.database).search(term, limit=20)
            if action == "search-interface":
                return query.search_interface(term, limit=20)
            repo = text_field(args, "repo", maximum=40)
            return query.search_code(term, limit=30, repo_ids=[repo] if repo else None)
        if action in {"trace", "impact"}:
            if not term:
                raise WebError("请输入接口号、operationId 或符号")
            depth = number_field(args, "max_depth", 8, 0, 32)
            nodes = number_field(args, "max_nodes", 150, 1, 500)
            if action == "impact":
                return query.impact(term, max_depth=depth, max_nodes=nodes)
            return query.trace(term, max_depth=depth, max_nodes=nodes,
                               direction=text_field(args, "direction", "downstream", 20))
        if action == "detect-conflicts":
            return KnowledgeConflictQuery(self.config, self.database).report(term or None)
        if action == "snapshots":
            reports = [GitSnapshotInspector().inspect(repo).to_dict() for repo in self.config.repositories]
            return {"ok": all(r["ok"] for r in reports), "snapshots": reports}
        if action == "index-code":
            return CodeIndexer(self.config, self.database).index()
        if action == "index-docs":
            return KnowledgeIndexer(self.config, self.database).index()
        if action == "analyze-requirement":
            fields = {}
            for key in ("non_goals", "business_objects", "interface_ids", "terms", "constraints",
                        "acceptance_criteria", "open_questions", "user_decisions"):
                fields[key] = [v.strip() for v in text_field(args, key).splitlines() if v.strip()]
            frame = RequirementFrame.parse(text_field(args, "goal", maximum=8000), **fields)
            analyzer = RequirementAnalyzer(self.config, self.database)
            backend = text_field(args, "backend", "template", 20)
            if backend == "template":
                return analyzer.analyze(frame)
            if backend not in {"mock", "zhipu"}:
                raise WebError("不支持的模型后端")
            context = text_field(args, "context", "requirement-only", 30)
            if context not in {"requirement-only", "verified-facts"}:
                raise WebError("不支持的模型上下文")
            provider = MockBackend() if backend == "mock" else ZhipuBackend(text_field(args, "model", DEFAULT_MODEL, 80))
            return AssistedRequirementAnalyzer(analyzer, ModelGateway(provider), context).analyze(frame)
        raise WebError("不支持的操作")

    def job(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise WebError("任务不存在或已过期")
            job = self.jobs[job_id]
            result = copy.deepcopy({k: v for k, v in job.items() if k not in {"result", "presentation"}})
            if "presentation" in job:
                result["result"] = copy.deepcopy(job["presentation"])
            return result

    def _decorate(self, value, job_id, repo_id=None):
        if isinstance(value, list):
            for child in value:
                self._decorate(child, job_id, repo_id)
        elif isinstance(value, dict):
            repo_id = value.get("repo_id", repo_id)
            path, start = value.get("path"), value.get("start_line")
            if isinstance(path, str) and type(start) is int:
                candidate_repo = repo_id or ("bank_knowledge" if path.startswith("02_knowledge/") else None)
                if candidate_repo:
                    try:
                        repository = self.config.repository(candidate_repo)
                        repository.resolve_path(path, expected_kind="file")
                        end = value.get("end_line", start)
                        if type(end) is not int or start < 1 or end < start:
                            raise WebError("无效证据范围")
                        identity = f"{job_id}:{candidate_repo}:{path}:{start}:{end}"
                        key = hashlib.sha256(identity.encode()).hexdigest()[:32]
                        reference = {"job": job_id, "repo_id": candidate_repo, "path": path,
                                     "start_line": start, "end_line": end}
                        if repository.kind == "git_markdown":
                            with IndexStore(self.database).connect() as con:
                                row = con.execute("SELECT d.content_hash,d.source_revision FROM documents d JOIN document_snapshots s "
                                                  "ON d.snapshot_id=s.snapshot_id WHERE s.active=1 AND d.repo_id=? AND d.path=?",
                                                  (candidate_repo, path)).fetchone()
                            if row is None:
                                raise WebError("知识证据不在活动索引内")
                            reference.update(content_hash=row["content_hash"], source_revision=row["source_revision"])
                        self.evidence[key] = reference
                        value["web_evidence_id"] = key
                    except BankDevError:
                        pass
            for child in list(value.values()):
                self._decorate(child, job_id, repo_id)

    def source(self, key):
        with self.lock:
            reference = self.evidence.get(key)
            if reference is None:
                raise WebError("证据不存在或会话已过期，请重新运行分析。")
            reference = dict(reference)
        repository = self.config.repository(reference["repo_id"])
        path = repository.resolve_path(reference["path"], expected_kind="file")
        if repository.kind == "java_source":
            revision = repository.baseline_commit
            content = GitSnapshotInspector()._git(repository, "show", f"{revision}:{reference['path']}")
        else:
            data = path.read_bytes()
            with IndexStore(self.database).connect() as con:
                row = con.execute("SELECT d.content_hash,d.source_revision FROM documents d JOIN document_snapshots s "
                                  "ON d.snapshot_id=s.snapshot_id WHERE s.active=1 AND d.repo_id=? AND d.path=?",
                                  (repository.id, reference["path"])).fetchone()
            if (row is None or row["source_revision"] != reference["source_revision"]
                    or row["content_hash"] != reference["content_hash"]
                    or hashlib.sha256(data).hexdigest() != row["content_hash"]):
                raise WebError("知识文件已变化，请更新知识索引并重新分析。")
            content, revision = data.decode("utf-8-sig", errors="replace"), row["source_revision"]
        lines = content.splitlines()
        if reference["start_line"] > len(lines):
            raise WebError("证据行号失效，请重新索引。")
        start = max(1, reference["start_line"] - 3)
        end = min(len(lines), reference["end_line"] + 3, start + 199)
        return {**reference, "snapshot": revision, "display_start": start, "display_end": end,
                "truncated": end < reference["end_line"], "lines": lines[start - 1:end]}

    def export(self, job_id, fmt):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job["status"] != "completed":
                raise WebError("任务尚未完成或已过期")
            result = job["result"]
        if fmt == "json":
            return json.dumps(result, ensure_ascii=False, indent=2)
        if fmt == "markdown" and job["action"] == "analyze-requirement":
            return render_requirement_markdown(result)
        raise WebError("此结果不支持该导出格式")


def make_server(app, port=8765):
    assets = Path(__file__).parent / "web_static"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Query text, API headers and evidence never enter access logs.

        def send(self, status, body, content_type="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self, session=False):
            host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != host or self.headers.get("Origin", f"http://{host}") != f"http://{host}":
                return False
            if self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}:
                return False
            if session:
                return self.headers.get("X-BankDev-Web") == "1"
            return secrets.compare_digest(self.headers.get("X-BankDev-Token", ""), app.token)

        def do_GET(self):
            parsed = urlsplit(self.path)
            if not parsed.path.startswith("/api/"):
                if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                    return self.send(403, {"error": "仅允许本机访问"})
                names = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
                if parsed.path not in names:
                    return self.send(404, {"error": "页面不存在"})
                file = assets / names[parsed.path]
                return self.send(200, file.read_bytes(), {".html": "text/html", ".js": "application/javascript", ".css": "text/css"}[file.suffix] + "; charset=utf-8")
            if not self.allowed(session=parsed.path == "/api/session"):
                return self.send(403, {"error": "本机会话校验失败，请刷新页面。"})
            try:
                if parsed.path == "/api/session":
                    return self.send(200, {"token": app.token})
                if parsed.path == "/api/status":
                    return self.send(200, app.status())
                parts = parsed.path.strip("/").split("/")
                if len(parts) == 3 and parts[1] == "jobs":
                    return self.send(200, app.job(parts[2]))
                if len(parts) == 3 and parts[1] == "evidence":
                    return self.send(200, app.source(parts[2]))
                if len(parts) == 3 and parts[1] == "export":
                    fmt = parse_qs(parsed.query).get("format", ["json"])[0]
                    return self.send(200, app.export(parts[2], fmt).encode(), "text/plain; charset=utf-8")
                return self.send(404, {"error": "接口不存在"})
            except (BankDevError, OSError, sqlite3.Error) as exc:
                return self.send(400, {"error": str(exc)})

        def do_POST(self):
            if not self.allowed():
                return self.send(403, {"error": "本机会话校验失败，请刷新页面。"})
            if self.path != "/api/jobs":
                return self.send(404, {"error": "接口不存在"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 48000 or self.headers.get("Content-Type") != "application/json":
                    raise WebError("请求必须为不超过 48KB 的 JSON")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or set(payload) != {"action", "arguments"}:
                    raise WebError("无效请求")
                return self.send(202, app.submit(payload["action"], payload["arguments"]))
            except (BankDevError, ValueError, UnicodeError) as exc:
                return self.send(400, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def serve(config, database, port=8765):
    if not 1 <= port <= 65535:
        raise WebError("端口必须在 1–65535 之间")
    app = WebApplication(config, database.resolve())
    server = make_server(app, port)
    print(f"BankDev Agent Web: http://127.0.0.1:{server.server_port}", flush=True)
    print("仅本机访问。按 Ctrl+C 停止；运行中的任务会完成后退出。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
