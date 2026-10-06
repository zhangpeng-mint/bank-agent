from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bankdev_agent.code_index import CodeIndexer
from bankdev_agent.config import load_project_config
from bankdev_agent.knowledge_conflicts import KnowledgeConflictQuery
from bankdev_agent.knowledge_index import (
    KnowledgeIndexer,
    _detect_semantic_conflicts,
    _parse_document,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG = PROJECT_ROOT / "config/repositories.toml"


class Phase5BSemanticConflictRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_project_config(CONFIG)
        cls.temporary = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temporary.name) / "phase5b.sqlite3"
        CodeIndexer(cls.config, cls.database).index(["dfbm"])
        cls.index_result = KnowledgeIndexer(cls.config, cls.database).index()
        cls.report = KnowledgeConflictQuery(cls.config, cls.database).report("01100121")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_extracts_structured_claims_with_line_coordinates(self) -> None:
        payload = _parse_document(
            "# 接口\n\n| 原服务号 | 01100121 |\n| 接口 | `POST /v1/products` |\n"
            "| 成功响应 | `200 OK` |\n| 下游依赖 | **无** |\n\n"
            "请求体 schema 为 `ProductQueryRequest`。\n"
        )
        claims = {item["field_name"]: item for item in payload["claims"]}
        self.assertEqual(claims["http_method"]["normalized_value"], "POST")
        self.assertEqual(claims["route"]["normalized_value"], "/v1/products")
        self.assertEqual(claims["success_status"]["normalized_value"], "200")
        self.assertEqual(claims["downstream_dependency"]["normalized_value"], "none")
        self.assertEqual(
            claims["request_schema"]["normalized_value"], "ProductQueryRequest"
        )
        self.assertEqual(claims["route"]["start_line"], 4)

    def test_active_field_conflict_is_blocking(self) -> None:
        claims = [
            self._claim("a", "current", "GET"),
            self._claim("b", "current", "POST"),
        ]
        conflicts = _detect_semantic_conflicts("snapshot:test", claims)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0][4], "active_field_conflict")
        self.assertEqual(conflicts[0][5], "high")

    def test_historical_difference_is_non_blocking_drift(self) -> None:
        claims = [
            self._claim("current", "current", "POST"),
            self._claim("old", "historical", "GET"),
        ]
        conflicts = _detect_semantic_conflicts("snapshot:test", claims)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0][4], "historical_drift")
        self.assertEqual(conflicts[0][5], "info")

    def test_01100121_claims_match_current_code(self) -> None:
        self.assertTrue(self.report["ok"])
        self.assertEqual(self.report["summary"]["blocking_conflicts"], 0)
        fields = {
            item["field_name"]: item["normalized_value"]
            for item in self.report["claims"]
        }
        self.assertEqual(fields["http_method"], "POST")
        self.assertEqual(fields["route"], "/dfbm-fbs/v1/loan-products")
        self.assertEqual(fields["request_schema"], "LoanProductQueryRequest")
        self.assertEqual(fields["downstream_dependency"], "none")
        self.assertEqual(self.report["code_alignment"][0]["status"], "aligned")
        self.assertEqual(
            self.report["code_alignment"][0]["code_evidence"][0]["operation_id"],
            "queryLoanProducts",
        )

    def test_schema_v4_persists_claims_and_conflicts(self) -> None:
        self.assertEqual(self.index_result["schema_version"], 4)
        self.assertGreaterEqual(self.index_result["stats"]["claims"], 5)
        with KnowledgeConflictQuery(self.config, self.database).store.connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertIn("document_claims", tables)
        self.assertIn("semantic_conflicts", tables)

    @staticmethod
    def _claim(claim_id: str, scope: str, value: str) -> dict[str, str]:
        return {
            "claim_id": claim_id,
            "doc_id": "doc:" + claim_id,
            "interface_id": "01100121",
            "field_name": "http_method",
            "normalized_value": value,
            "assertion_scope": scope,
        }


if __name__ == "__main__":
    unittest.main()
