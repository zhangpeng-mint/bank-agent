from __future__ import annotations

import hashlib
import http.client
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bankdev_agent.config import load_project_config
from bankdev_agent.errors import BankDevError
from bankdev_agent.index_store import IndexStore
from bankdev_agent.web import WebApplication, WebError, make_server

ROOT = Path(__file__).resolve().parents[2]


class WebTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "var")
        self.directory = Path(self.temporary.name)
        config = load_project_config(ROOT / "config/repositories.toml")
        self.knowledge = self.directory / "knowledge"
        (self.knowledge / "02_knowledge/03_接口").mkdir(parents=True)
        repo = replace(config.repository("bank_knowledge"), root=self.knowledge,
                       content_root=self.knowledge / "02_knowledge")
        self.config = replace(config, repositories=tuple(repo if r.id == repo.id else r for r in config.repositories))
        self.database = self.directory / "web.sqlite3"
        IndexStore(self.database).initialize()
        self.app = WebApplication(self.config, self.database)
        self.server = make_server(self.app, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.close()
        self.temporary.cleanup()

    def request(self, path, method="GET", body=None, headers=None, authenticated=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        sent = {"Content-Type": "application/json"}
        if authenticated:
            sent["X-BankDev-Token"] = self.app.token
        sent.update(headers or {})
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        status, result, returned_headers = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return status, result, returned_headers

    def fixture_document(self):
        path = self.knowledge / "02_knowledge/03_接口/demo.md"
        data = b"# Synthetic\n\n<script>alert('xss')</script>\n"
        path.write_bytes(data)
        with IndexStore(self.database).connect() as con:
            con.execute("INSERT INTO document_snapshots VALUES ('active','bank_knowledge','git:synthetic','now',1,1)")
            con.execute("INSERT INTO document_snapshots VALUES ('old','bank_knowledge','git:old','before',1,0)")
            for sid in ("active", "old"):
                con.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    'doc-'+sid, sid, 'bank_knowledge', 'git:synthetic' if sid=='active' else 'git:old',
                    '02_knowledge/03_接口/demo.md', 'blob', hashlib.sha256(data).hexdigest(), 'Demo', 'interface',
                    'current', '1', 'synthetic', 'public', '{}'))
        return path

    def wait_job(self, job_id):
        for _ in range(100):
            job = self.app.job(job_id)
            if job["status"] in {"completed", "failed"}:
                return job
            time.sleep(.01)
        self.fail("job did not finish")

    def test_static_page_and_security_headers(self):
        code, body, headers = self.request("/", authenticated=False)
        self.assertEqual(code, 200)
        self.assertIn(b"BankDev", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn(b"api-key", body)

    def test_api_requires_session_token_and_custom_bootstrap_header(self):
        self.assertEqual(self.request("/api/status", authenticated=False)[0], 403)
        self.assertEqual(self.request("/api/session", authenticated=False)[0], 403)
        code, body, _ = self.request("/api/session", headers={"X-BankDev-Web":"1"}, authenticated=False)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["token"], self.app.token)

    def test_foreign_origin_host_and_fetch_site_rejected(self):
        for headers in ({"Origin":"https://example.org"}, {"Host":"attacker.example"},
                        {"Sec-Fetch-Site":"cross-site"}):
            self.assertEqual(self.request("/api/status", headers=headers)[0], 403)
        self.assertEqual(self.request("/", headers={"Host":"attacker.example"})[0], 403)

    def test_arbitrary_files_and_unissued_evidence_unavailable(self):
        for path in ("/../../config/repositories.toml", "/config/repositories.toml", "/var/bankdev.sqlite3"):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request("/api/evidence/not-issued")[0], 400)

    def test_post_shape_size_and_operation_validation(self):
        for payload in ({"action":"shell","arguments":{"command":"rm"}}, [],
                        {"action":"snapshots","arguments":{},"extra":True}):
            self.assertEqual(self.request("/api/jobs","POST",json.dumps(payload))[0],400)
        self.assertEqual(self.request("/api/jobs","POST","x"*48001)[0],400)
        self.assertEqual(self.request("/api/jobs","POST","{}",{"Content-Type":"text/plain"})[0],400)

    def test_status_counts_active_documents_only(self):
        self.fixture_document()
        self.assertEqual(self.app.status()["counts"]["documents"], 1)
        with patch.dict('os.environ', {"BANKDEV_MODEL_API_KEY":"synthetic-secret"}):
            status = self.app.status()
        self.assertTrue(status["model_key_available"])
        self.assertNotIn("synthetic-secret", json.dumps(status))

    def test_async_job_export_and_no_presentation_fields_in_export(self):
        self.fixture_document()
        self.app.execute = lambda action,args: {"repo_id":"bank_knowledge","path":"02_knowledge/03_接口/demo.md","start_line":3,"end_line":3}
        code, body, _ = self.request('/api/jobs','POST',json.dumps({'action':'search-knowledge','arguments':{'query':'demo'}}))
        self.assertEqual(code,202)
        job = self.wait_job(json.loads(body)["id"])
        self.assertEqual(job["status"],"completed")
        self.assertIn("web_evidence_id",job["result"])
        exported = self.app.export(job["id"],"json")
        self.assertNotIn("web_evidence_id",exported)
        with self.assertRaises(WebError):
            self.app.export(job["id"],"markdown")

    def test_evidence_preview_pinned_and_stale_document_rejected(self):
        path = self.fixture_document()
        self.app.execute = lambda action,args: {"repo_id":"bank_knowledge","path":"02_knowledge/03_接口/demo.md","start_line":3,"end_line":3}
        job=self.wait_job(self.app.submit('search-knowledge',{})['id'])
        key=job['result']['web_evidence_id']
        source=self.app.source(key)
        self.assertEqual(source['snapshot'],'git:synthetic')
        self.assertIn("<script>",source['lines'][2])
        path.write_text('changed\n')
        with self.assertRaises(WebError):
            self.app.source(key)

    def test_failed_and_concurrent_jobs(self):
        gate=threading.Event()
        self.app.execute=lambda action,args: gate.wait(2) or {}
        job_id=self.app.submit('snapshots',{})['id']
        try:
            with self.assertRaises(WebError): self.app.submit('snapshots',{})
        finally:
            gate.set()
        self.wait_job(job_id)
        def fail(action,args): raise WebError('synthetic failure')
        self.app.execute=fail
        failed=self.wait_job(self.app.submit('snapshots',{})['id'])
        self.assertEqual(failed['status'],'failed')
        self.assertEqual(failed['error'],'synthetic failure')

    def test_invalid_graph_and_model_arguments(self):
        for action,args in [('trace',{'query':'a','max_depth':33}),
                            ('trace',{'query':'a','max_nodes':True}),
                            ('search-code',{'query':['bad']}),
                            ('analyze-requirement',{'goal':'demo','backend':'unknown'})]:
            with self.assertRaises(BankDevError): self.app.execute(action,args)
