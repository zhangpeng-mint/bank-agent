from __future__ import annotations

import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bankdev_agent.code_index import CodeIndexer
from bankdev_agent.cli import main
from bankdev_agent.config import load_project_config
from bankdev_agent.errors import ScopeViolation
from bankdev_agent.index_store import IndexStore
from bankdev_agent.knowledge_index import KnowledgeIndexer
from bankdev_agent.knowledge_conflicts import KnowledgeConflictQuery
from bankdev_agent.requirement_analysis import (
    ClaimVerifier, RequirementAnalyzer, RequirementFrame, STEPS, render_requirement_markdown,
)


ROOT = Path(__file__).resolve().parents[2]


class Phase6RequirementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_project_config(ROOT / "config/repositories.toml")
        cls.temporary = tempfile.TemporaryDirectory(dir=ROOT / "var")
        cls.database = Path(cls.temporary.name) / "phase6.sqlite3"
        CodeIndexer(cls.config, cls.database).index()
        KnowledgeIndexer(cls.config, cls.database).index()
        cls.analyzer = RequirementAnalyzer(cls.config, cls.database)
        cls.frame = RequirementFrame.parse("分析 01100121 贷款产品查询", non_goals=["不修改业务代码"],
                                          acceptance_criteria=["定位当前契约和静态链路"])
        cls.report = cls.analyzer.analyze(cls.frame)
        cls.verifier = ClaimVerifier(cls.config, cls.database)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_end_to_end_and_determinism(self):
        report = self.report
        self.assertEqual(report["status"], "complete", report["coverage"])
        self.assertEqual([s["name"] for s in report["steps"]], list(STEPS))
        self.assertEqual(len(report["snapshots"]), 4)
        self.assertEqual(report["knowledge_snapshots"][0]["source_revision"],
                         "git:f2cfbada05011c0d0f15f607feb4826d254b2526")
        self.assertEqual(report, self.analyzer.analyze(self.frame))
        knowledge = [c for c in report["claims"] if c["category"] == "knowledge_facts"]
        self.assertEqual(len(knowledge), 5)
        self.assertTrue(any("POST /dfbm-fbs/v1/loan-products" in c["text"] for c in report["claims"]))
        nodes = report["traces"][0]["nodes"]
        self.assertEqual({n["repo_id"] for n in nodes}, {"dfbm"})
        self.assertEqual(sum(n["kind"] == "mapper_statement" for n in nodes), 7)
        self.assertEqual(report["code_alignment"][0]["status"], "aligned")
        self.assertTrue(report["verification"]["ok"])

    def test_all_fact_evidence_is_bound(self):
        for claim in self.report["claims"]:
            if claim["type"] == "fact":
                self.assertTrue(claim["evidence_ids"])
                for eid in claim["evidence_ids"]:
                    self.assertIn(eid, self.report["evidence_ledger"])
        rendered = render_requirement_markdown(self.report)
        for title in ("四仓 snapshot", "知识事实", "当前代码事实", "历史事实", "用户决策", "冲突", "覆盖范围", "unresolved", "待确认问题", "可点击证据"):
            self.assertIn(title, rendered)
        self.assertIn("](</Users/", rendered)

    def test_fabricated_entity_and_arbitrary_fact_text_are_blocked(self):
        for mutation in ("entity", "text", "category"):
            claims = copy.deepcopy(self.report["claims"])
            if mutation == "entity":
                claims[0]["binding"]["id"] = "FakeLoanController"
            elif mutation == "text":
                claims[0]["text"] = "代码调用 FakeLoanController 并直连 Fineract"
            else:
                claims[0]["category"] = "historical_facts"
            self.assertFalse(self.verifier.verify(claims, self.report["evidence_ledger"])["ok"])

    def test_missing_and_invalid_evidence_are_blocked(self):
        claims = copy.deepcopy(self.report["claims"])
        claims[0]["evidence_ids"] = []
        self.assertFalse(self.verifier.verify(claims, self.report["evidence_ledger"])["ok"])
        ledger = copy.deepcopy(self.report["evidence_ledger"])
        eid = self.report["claims"][0]["evidence_ids"][0]
        for key, value in (("snapshot", "stale"), ("start_line", 999999), ("content_hash", "bad")):
            changed = copy.deepcopy(ledger)
            changed[eid][key] = value
            self.assertFalse(self.verifier.verify(self.report["claims"], changed)["ok"])

    def test_stale_index_file_hash_blocks_without_touching_source(self):
        with IndexStore(self.database).connect() as con:
            row = con.execute("SELECT * FROM files WHERE repo_id='dfbm' AND path LIKE '%dfbm-fbs-api.yaml'").fetchone()
            con.execute("UPDATE files SET blob_hash='invalid' WHERE repo_id=? AND path=?", (row["repo_id"], row["path"]))
        try:
            report = self.analyzer.analyze(self.frame)
            self.assertEqual(report["status"], "blocked")
            self.assertFalse(report["publishable"])
        finally:
            with IndexStore(self.database).connect() as con:
                con.execute("UPDATE files SET blob_hash=? WHERE repo_id=? AND path=?", (row["blob_hash"], row["repo_id"], row["path"]))

    def test_partial_depth_and_node_budget(self):
        for kwargs in ({"max_depth": 0}, {"max_nodes": 1}):
            report = self.analyzer.analyze(self.frame, **kwargs)
            self.assertEqual(report["status"], "partial")
            self.assertTrue(any("trace_truncated" in g for g in report["coverage"]["gaps"]))
            self.assertTrue(report["verification"]["ok"])

    def test_unknown_interface_is_scoped_partial(self):
        report = self.analyzer.analyze(RequirementFrame.parse("分析 99999999"))
        self.assertEqual(report["status"], "partial")
        self.assertFalse(report["ok"])
        self.assertFalse(any(c["type"] == "fact" for c in report["claims"]))
        self.assertIn("endpoint_not_found_in_scope: 99999999", report["coverage"]["gaps"])

    def test_missing_index_and_missing_intake_fields_are_partial(self):
        report = RequirementAnalyzer(self.config, Path(self.temporary.name) / "absent.sqlite3").analyze(self.frame)
        self.assertEqual(report["status"], "partial")
        self.assertFalse((Path(self.temporary.name) / "absent.sqlite3").exists())
        report = self.analyzer.analyze(RequirementFrame.parse("01100121"))
        self.assertIn("acceptance_criteria_unspecified", report["coverage"]["gaps"])

    def test_frame_does_not_invent_user_decisions(self):
        frame = RequirementFrame.parse("01100121", user_decisions=["保留当前 POST"])
        self.assertEqual(frame.user_decisions, ["保留当前 POST"])
        self.assertEqual(frame.business_objects, [])
        self.assertEqual(frame.acceptance_criteria, [])

    def test_outside_var_database_rejected(self):
        with self.assertRaises(ScopeViolation):
            RequirementAnalyzer(self.config, ROOT / "outside.sqlite3")

    def test_high_conflict_blocks_but_historical_drift_does_not(self):
        result = KnowledgeConflictQuery(self.config, self.database).report("01100121")
        for severity in ("high", "info"):
            changed = copy.deepcopy(result)
            changed["conflicts"] = [{"severity": severity, "conflict_kind": "active_field_conflict" if severity == "high" else "historical_drift"}]
            changed["ok"] = severity != "high"
            with patch.object(KnowledgeConflictQuery, "report", return_value=changed):
                report = self.analyzer.analyze(self.frame)
            self.assertEqual(report["status"], "blocked" if severity == "high" else "complete")

    def test_historical_claim_stays_out_of_current_facts(self):
        with IndexStore(self.database).connect() as con:
            row = con.execute("SELECT * FROM document_claims WHERE interface_id='01100121' LIMIT 1").fetchone()
            con.execute("UPDATE document_claims SET assertion_scope='historical' WHERE claim_id=?", (row["claim_id"],))
        try:
            claim, evidence = self.verifier.materialize({"kind": "knowledge", "id": row["claim_id"]})
            self.assertEqual(claim.category, "historical_facts")
            self.assertIn("知识记载", claim.text)
        finally:
            with IndexStore(self.database).connect() as con:
                con.execute("UPDATE document_claims SET assertion_scope=? WHERE claim_id=?", (row["assertion_scope"], row["claim_id"]))

    def test_stale_knowledge_snapshot_is_blocking(self):
        with IndexStore(self.database).connect() as con:
            row = con.execute("SELECT * FROM document_snapshots WHERE active=1").fetchone()
            con.execute("UPDATE document_snapshots SET source_revision='stale' WHERE snapshot_id=?", (row["snapshot_id"],))
        try:
            report = self.analyzer.analyze(self.frame)
            self.assertEqual(report["status"], "blocked")
        finally:
            with IndexStore(self.database).connect() as con:
                con.execute("UPDATE document_snapshots SET source_revision=? WHERE snapshot_id=?", (row["source_revision"], row["snapshot_id"]))

    def test_cli_json_markdown_and_partial_exit(self):
        arguments = ["--config", str(ROOT / "config/repositories.toml"), "analyze-requirement", "01100121",
                     "--db", str(self.database), "--non-goal", "不修改代码", "--acceptance", "定位静态链"]
        output = Path(self.temporary.name) / "report.md"
        with contextlib.redirect_stdout(io.StringIO()):
            main(arguments + ["--format", "markdown", "--output", str(output)])
        self.assertIn("# 需求分析报告", output.read_text())
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), self.assertRaises(SystemExit) as raised:
            main(arguments + ["--max-depth", "0"])
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(json.loads(stream.getvalue())["status"], "partial")
