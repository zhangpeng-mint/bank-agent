from __future__ import annotations

import unittest
from pathlib import Path

from bankdev_agent.config import load_project_config
from bankdev_agent.golden import GoldenVerifier


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Interface01100121GoldenTest(unittest.TestCase):
    def test_current_sources_match_evidence_baseline(self) -> None:
        config = load_project_config(PROJECT_ROOT / "config/repositories.toml")
        report = GoldenVerifier(config).verify(
            PROJECT_ROOT / "tests/golden/cases/01100121/case.json"
        )
        failures = [check for check in report.checks if not check.ok]
        snapshot_failures = [snapshot for snapshot in report.snapshots if not snapshot.ok]
        self.assertTrue(report.ok, {"checks": failures, "snapshots": snapshot_failures})


if __name__ == "__main__":
    unittest.main()

