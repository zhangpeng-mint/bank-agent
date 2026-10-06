from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bankdev_agent.code_index import CodeIndexer
from bankdev_agent.config import load_project_config
from bankdev_agent.index_query import IndexQuery
from bankdev_agent.index_store import IndexStore, SCHEMA_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG = PROJECT_ROOT / "config/repositories.toml"


class Phase4AIndexRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_project_config(CONFIG)
        cls.temporary = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temporary.name) / "code-index.sqlite3"
        cls.first = CodeIndexer(cls.config, cls.database).index(["dfbm"])
        cls.second = CodeIndexer(cls.config, cls.database).index(["dfbm"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_schema_and_incremental_cache(self) -> None:
        self.assertGreater(self.first["stats"]["files_scanned"], 0)
        self.assertEqual(
            self.first["stats"]["files_parsed"], self.first["stats"]["files_scanned"]
        )
        self.assertEqual(self.second["stats"]["files_parsed"], 0)
        self.assertEqual(
            self.second["stats"]["cache_hits"], self.second["stats"]["files_scanned"]
        )
        with IndexStore(self.database).connect() as connection:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()["value"]
            self.assertEqual(int(version), SCHEMA_VERSION)

    def test_search_uses_current_knowledge_contract(self) -> None:
        result = IndexQuery(self.config, self.database).search_interface("01100121")
        self.assertEqual(result["count"], 1)
        match = result["matches"][0]
        self.assertEqual(match["http_method"], "POST")
        self.assertEqual(match["route"], "/dfbm-fbs/v1/loan-products")
        self.assertEqual(match["operation_id"], "queryLoanProducts")

    def test_trace_reconstructs_local_database_flow_only(self) -> None:
        result = IndexQuery(self.config, self.database).trace("01100121")
        labels = {node["label"] for node in result["nodes"]}
        self.assertEqual(result["coverage"]["node_count"], 18)
        self.assertEqual(result["coverage"]["edge_count"], 17)
        self.assertTrue(any("LoanInternalController.queryLoanProducts" in label for label in labels))
        self.assertTrue(any("ProductQueryServiceImpl.queryLoanProducts" in label for label in labels))
        self.assertEqual(sum(node["kind"] == "mapper_statement" for node in result["nodes"]), 7)
        self.assertEqual(sum(node["kind"] == "sql_table" for node in result["nodes"]), 8)
        self.assertEqual({node["repo_id"] for node in result["nodes"]}, {"dfbm"})
        self.assertFalse(any("dcis" in label.lower() or "fineract" in label.lower() for label in labels))


if __name__ == "__main__":
    unittest.main()
