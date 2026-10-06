from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from bankdev_agent.config import load_project_config
from bankdev_agent.errors import ScopeViolation


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config/repositories.toml"


class ProjectConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_project_config(CONFIG_PATH)

    def test_loads_read_only_configuration(self) -> None:
        self.assertEqual("read_only", self.config.access_mode)
        self.assertFalse(self.config.allow_arbitrary_paths)
        self.assertFalse(self.config.follow_symlinks)
        self.assertEqual({"dfbm", "dcis", "fineract", "bank_knowledge"}, {
            repository.id for repository in self.config.repositories
        })

    def test_resolves_allowlisted_evidence(self) -> None:
        repository = self.config.repository("dfbm")
        path = repository.resolve_path(
            "dfbm-fbs/src/main/openapi/dfbm-fbs-api.yaml",
            expected_kind="file",
        )
        self.assertTrue(path.is_file())

    def test_rejects_absolute_and_parent_paths(self) -> None:
        repository = self.config.repository("dfbm")
        for value in ("/etc/passwd", "../mint-dfbm/README.md", "dfbm-fbs/../../README.md"):
            with self.subTest(value=value), self.assertRaises(ScopeViolation):
                repository.resolve_path(value)

    def test_rejects_excluded_git_metadata(self) -> None:
        repository = self.config.repository("dfbm")
        with self.assertRaises(ScopeViolation):
            repository.resolve_path(".git/config", expected_kind="file")

    def test_marks_demo_knowledge_without_granting_authority(self) -> None:
        repository = self.config.repository("bank_knowledge")
        relative = "02_knowledge/99_演示/模拟转账接口.md"
        self.assertTrue(repository.is_included(relative))
        self.assertTrue(repository.is_demo(relative))
        self.assertEqual("never_use_as_real_business_evidence", repository.demo_authority)

    def test_rejects_symlink_component(self) -> None:
        repository = self.config.repository("dfbm")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            (target / "evidence.java").write_text("class Evidence {}", encoding="utf-8")
            (root / "linked").symlink_to(target, target_is_directory=True)
            isolated = replace(
                repository,
                root=root,
                include=("**/*.java",),
                exclude=(),
                preexisting_changes=(),
            )
            with self.assertRaises(ScopeViolation):
                isolated.resolve_path("linked/evidence.java", expected_kind="file")


if __name__ == "__main__":
    unittest.main()

