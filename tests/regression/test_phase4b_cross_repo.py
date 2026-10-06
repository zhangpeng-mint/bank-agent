from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from bankdev_agent.code_index import CodeIndexer
from bankdev_agent.config import load_project_config
from bankdev_agent.index_query import IndexQuery


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG = PROJECT_ROOT / "config/repositories.toml"
FINERACT_CLIENT_RESOURCE = (
    "fineract-provider/src/main/java/org/apache/fineract/portfolio/client/api/ClientsApiResource.java"
)


class Phase4BCrossRepositoryRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        base = load_project_config(CONFIG)
        repositories = tuple(
            replace(repository, include=(FINERACT_CLIENT_RESOURCE,))
            if repository.id == "fineract" else repository
            for repository in base.repositories
        )
        cls.config = replace(base, repositories=repositories)
        cls.temporary = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temporary.name) / "phase4b.sqlite3"
        cls.index_report = CodeIndexer(cls.config, cls.database).index()
        cls.query = IndexQuery(cls.config, cls.database)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_search_code_returns_ranked_clickable_symbols(self) -> None:
        result = self.query.search_code("FineractPersonalLoanClient", repo_ids=["dcis"])
        self.assertGreater(result["count"], 0)
        self.assertEqual(result["matches"][0]["label"],
                         "com.wefi.dcis.mis.client.FineractPersonalLoanClient")
        self.assertTrue(result["matches"][0]["evidence"]["markdown_link"].startswith("["))

    def test_open_account_trace_crosses_all_three_repositories(self) -> None:
        result = self.query.trace("createLoanClient", max_depth=20, max_nodes=300)
        self.assertEqual({node["repo_id"] for node in result["nodes"]},
                         {"dfbm", "dcis", "fineract"})
        remote = [edge for edge in result["edges"] if edge["kind"] == "INVOKES_REMOTE"]
        self.assertEqual(len(remote), 2)
        self.assertEqual(
            {edge["metadata"]["signature"] for edge in remote},
            {"POST /dcp-dcis/v1/loan-accounts (openAccount)",
             "POST /clients (createClient)"},
        )
        self.assertFalse(any(item["kind"] == "INVOKES_REMOTE" for item in result["unresolved"]))
        self.assertTrue(all(edge["classification"] == "confirmed_static" for edge in remote))

    def test_upstream_trace_and_impact_are_available(self) -> None:
        qualified = "com.wefi.dcis.mis.service.PersonalLoanService.openAccount"
        upstream = self.query.trace(qualified, direction="upstream", max_depth=12)
        self.assertTrue(any(node["repo_id"] == "dfbm" for node in upstream["nodes"]))
        impact = self.query.impact(qualified, max_depth=12)
        self.assertTrue(any(
            "PersonalLoanController.openAccount" in item["label"]
            for item in impact["direct_impacts"]
        ))
        self.assertTrue(impact["transitive_candidates"])

    def test_local_only_golden_remains_unchanged(self) -> None:
        result = self.query.trace("01100121")
        self.assertEqual(result["coverage"]["node_count"], 18)
        self.assertEqual(result["coverage"]["edge_count"], 17)
        self.assertEqual({node["repo_id"] for node in result["nodes"]}, {"dfbm"})


if __name__ == "__main__":
    unittest.main()
