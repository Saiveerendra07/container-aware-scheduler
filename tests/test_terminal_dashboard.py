import tempfile
import unittest
from pathlib import Path

from scripts.terminal_dashboard import weighted_share_compliance, format_table, latest_experiment


class TerminalDashboardTests(unittest.TestCase):
    def test_weighted_compliance_passes_within_tolerance(self):
        groups = [
            {"observed_allocation_pct": 24, "expected_allocation_pct": 25},
            {"observed_allocation_pct": 76, "expected_allocation_pct": 75},
        ]
        worst, status = weighted_share_compliance(groups, 5)
        self.assertAlmostEqual(worst, 1)
        self.assertEqual(status, "PASS")

    def test_weighted_compliance_reports_failed_shares(self):
        groups = [
            {"observed_allocation_pct": 42.72, "expected_allocation_pct": 25},
            {"observed_allocation_pct": 57.28, "expected_allocation_pct": 75},
        ]
        worst, status = weighted_share_compliance(groups, 5)
        self.assertAlmostEqual(worst, 17.72)
        self.assertEqual(status, "FAIL")

    def test_missing_measurements_are_not_tested(self):
        worst, status = weighted_share_compliance([
            {"observed_allocation_pct": None, "expected_allocation_pct": None},
        ])
        self.assertIsNone(worst)
        self.assertEqual(status, "NOT TESTED")

    def test_table_and_latest_experiment_are_deterministic(self):
        table = format_table([("alpha", "25")], ("Group", "Share"))
        self.assertIn("alpha", table)
        self.assertIn("Group", table)
        self.assertIsNone(latest_experiment({"experiments": []}))
        self.assertEqual(
            latest_experiment({"experiments": [{"run_id": "first"}, {"run_id": "latest"}]})["run_id"],
            "latest",
        )


if __name__ == "__main__":
    unittest.main()
