from __future__ import annotations

import copy
import json
import tempfile
import unittest

from bankdev_agent.model_analysis import AssistedRequirementAnalyzer
from bankdev_agent.model_gateway import Completion, ModelGateway, MockBackend
from bankdev_agent.requirement_analysis import RequirementAnalyzer, RequirementFrame, render_requirement_markdown
from bankdev_agent.config import load_project_config
from bankdev_agent.code_index import CodeIndexer
from bankdev_agent.knowledge_index import KnowledgeIndexer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class Phase7ModelAnalysisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_project_config(ROOT / "config/repositories.toml")
        cls.temporary = tempfile.TemporaryDirectory(dir=ROOT / "var")
        database = Path(cls.temporary.name) / "phase7.sqlite3"
        CodeIndexer(cls.config, database).index()
        KnowledgeIndexer(cls.config, database).index()
        cls.analyzer = RequirementAnalyzer(cls.config, database)
        cls.frame = RequirementFrame.parse("分析 01100121 贷款产品查询", non_goals=["不修改业务代码"],
                                          acceptance_criteria=["定位契约与静态链路"])
        cls.baseline = cls.analyzer.analyze(cls.frame)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_mock_end_to_end_preserves_verified_facts_and_user_frame(self):
        report = AssistedRequirementAnalyzer(self.analyzer, ModelGateway(MockBackend()), "verified-facts").analyze(self.frame)
        self.assertEqual(report["mode"], "model_assisted")
        self.assertEqual(report["status"], self.baseline["status"])
        self.assertEqual(report["requirement"], self.baseline["requirement"])
        facts = lambda r: [c for c in r["claims"] if c["type"] == "fact"]
        self.assertEqual(facts(report), facts(self.baseline))
        self.assertTrue(report["verification"]["ok"])
        self.assertTrue(report["model_assistance"]["selected_fact_ids"])
        self.assertEqual(len(report["model_assistance"]["events"]), 2)
        self.assertIn("模型辅助与审计", render_requirement_markdown(report))

    def test_requirement_only_context_sends_no_repository_facts(self):
        class Recording(MockBackend):
            def __init__(self): self.payloads = []
            def complete(self, task, system, payload):
                self.payloads.append(copy.deepcopy(payload))
                return super().complete(task, system, payload)
        backend = Recording()
        report = AssistedRequirementAnalyzer(self.analyzer, ModelGateway(backend)).analyze(self.frame)
        self.assertEqual(backend.payloads[1]["facts"], [])
        body = json.dumps(backend.payloads)
        self.assertNotIn("/Users/", body)
        self.assertNotIn("LoanInternalController", body)
        self.assertEqual(report["model_assistance"]["context_fact_count"], 0)

    def test_model_failure_returns_template_with_unchanged_facts(self):
        class Invalid(MockBackend):
            def complete(self, *args): return Completion("broken", {})
        report = AssistedRequirementAnalyzer(self.analyzer, ModelGateway(Invalid())).analyze(self.frame)
        self.assertEqual(report["mode"], "template_with_model_fallback")
        self.assertEqual(report["claims"], self.baseline["claims"])
        self.assertEqual(report["status"], self.baseline["status"])

    def test_forged_draft_references_rejected(self):
        class Forged(MockBackend):
            def complete(self, task, system, payload):
                if task == "intake": return super().complete(task, system, payload)
                return Completion('{"selected_fact_ids":["FakeController"],"suggestions":[]}', {})
        report = AssistedRequirementAnalyzer(self.analyzer, ModelGateway(Forged()), "verified-facts").analyze(self.frame)
        self.assertEqual(report["claims"], self.baseline["claims"])
        self.assertEqual(report["model_assistance"]["status"], "fallback")

    def test_model_cannot_promote_partial_to_complete(self):
        report = AssistedRequirementAnalyzer(self.analyzer, ModelGateway(MockBackend()), "verified-facts").analyze(self.frame, max_depth=0)
        self.assertEqual(report["status"], "partial")
        self.assertFalse(report["ok"])
        self.assertTrue(report["verification"]["ok"])

    def test_intake_proposals_do_not_fill_user_decisions_or_acceptance(self):
        report = AssistedRequirementAnalyzer(self.analyzer, ModelGateway(MockBackend())).analyze(RequirementFrame.parse("01100121"))
        self.assertEqual(report["requirement"]["acceptance_criteria"], [])
        self.assertEqual(report["user_decisions"], [])
        self.assertEqual(report["status"], "partial")
