import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bankdev_agent.config import load_project_config
from bankdev_agent.cli import main
from bankdev_agent.errors import BankDevError
from bankdev_agent.requirement_case import analyze_case, load_case

ROOT = Path(__file__).resolve().parents[2]


class RequirementCaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "var")
        self.path = Path(self.tmp.name) / "case.json"
        self.config = load_project_config(ROOT / "config/repositories.toml")
        self.data = json.loads((ROOT / "tests/cases/unified-loan-orders.json").read_text())
        self.path.write_text(json.dumps(self.data), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_pins_without_changing_scope_or_original_config(self):
        data, frame, scoped = load_case(self.path, self.config)
        self.assertEqual(frame.user_decisions, data["requirement"]["user_decisions"])
        for old, new in zip(self.config.repositories, scoped.repositories):
            self.assertEqual(new.baseline_commit, data["snapshots"][old.id])
            self.assertEqual(new.root, old.root)
            self.assertEqual(new.include, old.include)
            self.assertEqual(new.preexisting_changes, old.preexisting_changes)
        self.assertEqual(self.config.repository("dfbm").baseline_commit,
                         "44f59fabb2235f612fc470b49b18a0d551a9b995")

    def test_rejects_invalid_or_injected_fields(self):
        bad = []
        for key, value in [("case_id", "../escape"), ("snapshots", {"dfbm": "HEAD"}),
                           ("schema_version", True), ("requirement", {"goal": "x", "facts": ["fake"]})]:
            item = copy.deepcopy(self.data)
            item[key] = value
            bad.append(item)
        item = copy.deepcopy(self.data)
        item["requirement"]["terms"] = "not-a-list"
        bad.append(item)
        for item in bad:
            self.path.write_text(json.dumps(item))
            with self.assertRaises(BankDevError):
                load_case(self.path, self.config)
        self.path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(BankDevError):
            load_case(self.path, self.config)

    def test_rejects_baseline_index_and_escape_before_indexing(self):
        for db in [ROOT / "var/bankdev.sqlite3", ROOT / "outside.sqlite3"]:
            with patch("bankdev_agent.requirement_case.CodeIndexer") as indexer:
                with self.assertRaises(BankDevError):
                    analyze_case(self.config, self.path, db, build_index=True)
                indexer.assert_not_called()

    def test_partial_report_and_user_decisions_are_not_promoted(self):
        report = {"status": "partial", "ok": False, "publishable": True,
                  "claims": [], "coverage": {"blockers": []}}
        with patch("bankdev_agent.requirement_case.RequirementAnalyzer.analyze", return_value=report), \
                patch("bankdev_agent.requirement_case.CodeIndexer") as indexer:
            result = analyze_case(self.config, self.path)
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["claims"], [])
            self.assertEqual(len(result["case"]["input_sha256"]), 64)
            indexer.assert_not_called()

    def test_knowledge_revision_mismatch_blocks(self):
        report = {"status": "complete", "ok": True, "publishable": True,
                  "coverage": {"blockers": []}, "knowledge_snapshots": [
                      {"repo_id": "bank_knowledge", "source_revision": "git:" + "0" * 40}]}
        with patch("bankdev_agent.requirement_case.RequirementAnalyzer.analyze", return_value=report):
            result = analyze_case(self.config, self.path)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["publishable"])
        self.assertIn("case_knowledge_revision_mismatch", result["coverage"]["blockers"][0])

    def test_cli_preserves_partial_exit_and_writes_report(self):
        output = Path(self.tmp.name) / "report.json"
        report = {"status": "partial", "ok": False, "publishable": True}
        with patch("bankdev_agent.requirement_case.analyze_case", return_value=report), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                main(["analyze-case", str(self.path), "--output", str(output)])
        self.assertEqual(result.exception.code, 1)
        self.assertEqual(json.loads(output.read_text()), report)
