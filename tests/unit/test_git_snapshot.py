from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from bankdev_agent.config import load_project_config
from bankdev_agent.git_snapshot import GitSnapshotInspector


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class GitSnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_project_config(PROJECT_ROOT / "config/repositories.toml")
        cls.inspector = GitSnapshotInspector()

    def test_all_repositories_match_current_snapshot(self) -> None:
        reports = [self.inspector.inspect(repository) for repository in self.config.repositories]
        failures = {report.repo_id: report.to_dict() for report in reports if not report.ok}
        self.assertFalse(failures, failures)

    def test_dfbm_preexisting_changes_are_preserved(self) -> None:
        report = self.inspector.inspect(self.config.repository("dfbm"))
        self.assertFalse(report.clean)
        self.assertEqual((".sdkmanrc", "README.md"), report.expected_changes)
        self.assertFalse(report.unexpected_changes)
        self.assertFalse(report.missing_expected_changes)

    def test_porcelain_leading_space_preserves_first_path_character(self) -> None:
        repository = self.config.repository("bank_knowledge")
        output = " M 02_knowledge/example.md\n?? .DS_Store"
        with patch.object(self.inspector, "_git", return_value=output):
            changes = self.inspector._changes(repository)
        self.assertEqual(" M", changes[0].status)
        self.assertEqual("02_knowledge/example.md", changes[0].path)


if __name__ == "__main__":
    unittest.main()
