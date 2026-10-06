from __future__ import annotations

import unittest
from pathlib import Path

from bankdev_agent.config import load_project_config
from bankdev_agent.interface_analysis import InterfaceAnalyzer, render_markdown


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Interface01100121AnalysisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        config = load_project_config(PROJECT_ROOT / "config/repositories.toml")
        cls.result = InterfaceAnalyzer(config).analyze(
            "01100121",
            PROJECT_ROOT / "tests/golden/cases/01100121/case.json",
        )

    def test_reconstructs_current_entry_and_data_path(self) -> None:
        current = self.result["current_code"]
        self.assertEqual("POST", current["contract"]["method"])
        self.assertEqual("/dfbm-fbs/v1/loan-products", current["contract"]["path"])
        self.assertEqual("queryLoanProducts", current["contract"]["operation_id"])
        self.assertEqual("LoanInternalController", current["controller"]["class"])
        self.assertEqual("ProductQueryService", current["service_interface"]["class"])
        self.assertEqual("ProductQueryServiceImpl", current["service_implementation"]["class"])
        self.assertEqual(7, len(current["mappers"]))
        self.assertEqual(
            {
                "financial_product",
                "loan_product",
                "loan_product_term_plan",
                "loan_product_fee_rule",
                "loan_product_usage_config",
                "loan_product_applicability_rule",
                "loan_product_recommendation_rule",
                "loan_product_bank_mapping",
            },
            set(current["tables"]),
        )

    def test_does_not_invent_dcis_or_fineract_edges(self) -> None:
        downstream = self.result["current_code"]["downstream"]
        self.assertEqual([], downstream["confirmed_remote_edges"])
        self.assertEqual([], downstream["method_scope_remote_hits"])
        self.assertEqual([], downstream["dfbm_product_scope_remote_hits"])
        self.assertEqual({"dcis": [], "fineract": []}, downstream["cross_repo_literal_hits"])
        self.assertEqual("scoped_absence", downstream["classification"])

    def test_detects_legacy_drift_and_separates_source_categories(self) -> None:
        comparison = self.result["comparison"]
        self.assertEqual("aligned", comparison["knowledge_vs_current_code"])
        self.assertTrue(comparison["historical_contract_drift_detected"])
        self.assertEqual("GET /internal/v1/loan-products", comparison["historical_contract"])
        self.assertTrue(comparison["separation_complete"])
        knowledge = self.result["knowledge"]
        self.assertTrue(knowledge["knowledge_facts"])
        self.assertTrue(knowledge["historical_observations"])
        self.assertTrue(knowledge["confirmed_decisions"])
        self.assertTrue(knowledge["pending_decisions"])

    def test_every_graph_item_has_clickable_evidence_and_benchmark_passes(self) -> None:
        for item in self.result["graph"]["nodes"] + self.result["graph"]["edges"]:
            self.assertTrue(item["evidence"])
            for evidence in item["evidence"]:
                self.assertIn("](<", evidence["markdown_link"])
                self.assertEqual(40, len(evidence["commit"]))
        self.assertTrue(self.result["benchmark"]["ok"])
        markdown = render_markdown(self.result)
        self.assertIn("Golden 验收", markdown)
        self.assertIn("LoanInternalController.queryLoanProducts", markdown)


if __name__ == "__main__":
    unittest.main()
