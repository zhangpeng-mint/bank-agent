from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bankdev_agent.config import load_project_config
from bankdev_agent.knowledge_index import KnowledgeIndexer, _detect_conflicts, _parse_document
from bankdev_agent.knowledge_query import KnowledgeQuery


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG = PROJECT_ROOT / "config/repositories.toml"


class Phase5AKnowledgeIndexRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_project_config(CONFIG)
        cls.temporary = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temporary.name) / "phase5a.sqlite3"
        cls.first = KnowledgeIndexer(cls.config, cls.database).index()
        cls.second = KnowledgeIndexer(cls.config, cls.database).index()
        cls.query = KnowledgeQuery(cls.config, cls.database)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_incremental_rerun_uses_content_cache_without_duplicates(self) -> None:
        first = self.first["stats"]
        second = self.second["stats"]
        self.assertEqual(first["files_parsed"] + first["cache_hits"], first["files_scanned"])
        self.assertEqual(second["files_parsed"], 0)
        self.assertEqual(second["cache_hits"], second["files_scanned"])
        self.assertEqual(second["changes"]["unchanged"], second["files_scanned"])
        with self.query.store.connect() as connection:
            active = connection.execute(
                "SELECT count(*) FROM document_snapshots WHERE active=1"
            ).fetchone()[0]
            documents = connection.execute(
                "SELECT count(*) FROM documents d JOIN document_snapshots s "
                "ON s.snapshot_id=d.snapshot_id WHERE s.active=1"
            ).fetchone()[0]
        self.assertEqual(active, 1)
        self.assertEqual(documents, second["documents"])

    def test_committed_unicode_paths_match_git_baseline(self) -> None:
        knowledge_commit = self.config.repository("bank_knowledge").baseline_commit
        self.assertEqual(self.second["source_revision"], f"git:{knowledge_commit}")
        with self.query.store.connect() as connection:
            metadata = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT d.metadata_json FROM documents d "
                    "JOIN document_snapshots s ON s.snapshot_id=d.snapshot_id "
                    "WHERE s.active=1"
                ).fetchall()
            ]
        self.assertTrue(metadata)
        self.assertTrue(all(item["baseline_match"] for item in metadata))

    def test_interface_id_prefers_primary_current_document(self) -> None:
        result = self.query.search("01100121", limit=5)
        self.assertGreater(result["count"], 0)
        first = result["results"][0]
        self.assertEqual(
            first["path"],
            "02_knowledge/03_接口/01100121_贷款产品查询接口说明_2026-09-29.md",
        )
        self.assertIn("primary_interface_id", first["match_reasons"])
        self.assertTrue(first["evidence"]["uri"].startswith("bankdev://knowledge/"))
        self.assertTrue(first["evidence"]["markdown_link"].startswith("["))

    def test_chinese_fts_search_returns_diverse_documents(self) -> None:
        result = self.query.search("贷款产品查询", limit=5)
        self.assertGreaterEqual(result["count"], 2)
        counts: dict[str, int] = {}
        for item in result["results"]:
            counts[item["doc_id"]] = counts.get(item["doc_id"], 0) + 1
        self.assertTrue(all(count <= 2 for count in counts.values()))
        self.assertTrue(any("fts5_trigram" in item["match_reasons"] for item in result["results"]))

    def test_demo_and_untracked_metadata_are_not_indexed(self) -> None:
        with self.query.store.connect() as connection:
            paths = [row[0] for row in connection.execute("SELECT path FROM documents").fetchall()]
        self.assertFalse(any("/99_演示/" in path for path in paths))
        self.assertFalse(any(".obsidian" in path or path.endswith(".DS_Store") for path in paths))

    def test_parser_keeps_front_matter_line_coordinates(self) -> None:
        payload = _parse_document(
            "---\ndoc_id: TEST-1\nstatus: effective\n---\n# 标题\n\n正文 01100121\n"
        )
        self.assertEqual(payload["metadata"]["doc_id"], "TEST-1")
        self.assertEqual(payload["chunks"][0]["start_line"], 5)
        self.assertEqual(payload["chunks"][0]["end_line"], 7)

    def test_multiple_primary_documents_are_reported_as_conflict(self) -> None:
        documents = [
            {"doc_id": "a", "status": "effective", "primary": ["01100121"]},
            {"doc_id": "b", "status": "current", "primary": ["01100121"]},
        ]
        conflicts = _detect_conflicts("snapshot:test", documents)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0][2], "multiple_active_versions")


if __name__ == "__main__":
    unittest.main()
