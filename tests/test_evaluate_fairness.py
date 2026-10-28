import csv
import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from cgroup_metrics import COLUMNS as CGROUP_COLUMNS


SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "evaluate_fairness.py")
SPEC = importlib.util.spec_from_file_location("evaluate_fairness", SCRIPT)
evaluator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluator)


class FairnessEvaluationTests(unittest.TestCase):
    def custom_rows(self):
        groups = [
            {"name": "alpha", "cgroup_id": 101, "cgroup_path": "/synthetic/alpha", "share": 100},
            {"name": "beta", "cgroup_id": 202, "cgroup_path": "/synthetic/beta", "share": 300},
        ]
        config = json.dumps({"expected_group_count": 2, "fallback_share": 50,
                             "groups": groups}, sort_keys=True, separators=(",", ":"))
        rows = []
        for group in groups:
            rows.append({
                "run_id": "synthetic_test_only_custom", "group": group["name"],
                "cgroup_id": str(group["cgroup_id"]), "cgroup_path": group["cgroup_path"],
                "verified_cgroup_id": str(group["cgroup_id"]),
                "identity_verification_status": "verified",
                "group_share": str(group["share"]), "fallback_share": "50",
                "cpu_time_s": str(group["share"]), "measurement_status": "complete",
                "workload_elapsed_s": "2", "completion_status": "complete",
                "exit_status": "0", "termination_signal": "0", "scheduler_attached": "true",
                "scheduler_shutdown_status": "stopped", "custom_scheduler_schema_version": "2",
                "expected_group_count": "2", "expected_group_config_json": config,
            })
        return rows

    def group_csv(self, directory, name, rows, fields=None):
        path = os.path.join(directory, name)
        fields = fields or list(rows[0])
        with open(path, "w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def evaluate(self, path):
        runs, warnings = evaluator.load_inputs([path])
        self.assertEqual(warnings, [])
        rows = [result for run in runs.values() for result in evaluator.evaluate_run(run)]
        return {row["group"]: row for row in rows}

    def test_synthetic_test_only_equal_shares_match_equal_cpu_time(self):
        rows = [
            {"run_id": "synthetic_test_only_equal", "group": "alpha", "cpu_time_s": "2",
             "measurement_status": "complete", "group_share": "100"},
            {"run_id": "synthetic_test_only_equal", "group": "beta", "cpu_time_s": "2",
             "measurement_status": "complete", "group_share": "100"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            result = self.evaluate(self.group_csv(directory, "synthetic_test_only_equal.csv", rows))
        self.assertEqual(result["alpha"]["observed_cpu_pct"], 50)
        self.assertEqual(result["beta"]["expected_cpu_pct"], 50)
        self.assertEqual(result["alpha"]["allocation_error_pct_points"], 0)
        self.assertEqual(result["alpha"]["jain_fairness_index"], 1)

    def test_synthetic_test_only_unequal_shares_are_not_judged_as_equal(self):
        rows = [
            {"run_id": "synthetic_test_only_weighted", "group": "alpha", "cpu_time_s": "2",
             "measurement_status": "complete", "group_share": "200"},
            {"run_id": "synthetic_test_only_weighted", "group": "beta", "cpu_time_s": "1",
             "measurement_status": "complete", "group_share": "100"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            result = self.evaluate(self.group_csv(directory, "synthetic_test_only_weighted.csv", rows))
        self.assertAlmostEqual(result["alpha"]["observed_cpu_pct"], 200 / 3 * 1)
        self.assertAlmostEqual(result["alpha"]["expected_cpu_pct"], 200 / 3 * 1)
        self.assertAlmostEqual(result["alpha"]["allocation_error_pct_points"], 0)
        self.assertAlmostEqual(result["alpha"]["jain_fairness_index"], 0.9)

    def test_missing_measurement_excludes_group_and_blocks_weighted_conclusion(self):
        rows = [
            {"run_id": "synthetic_test_only_missing", "group": "alpha", "cpu_time_s": "2",
             "measurement_status": "complete", "group_share": "100"},
            {"run_id": "synthetic_test_only_missing", "group": "beta", "cpu_time_s": "NA",
             "measurement_status": "complete", "group_share": "100"},
            {"run_id": "synthetic_test_only_missing", "group": "gamma", "cpu_time_s": "not-a-number",
             "measurement_status": "complete", "group_share": "100"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            result = self.evaluate(self.group_csv(directory, "synthetic_test_only_missing.csv", rows))
        self.assertEqual(result["beta"]["cpu_time_s"], "NA")
        self.assertIn("cpu_time_s is missing", result["beta"]["measurement_exclusion_reason"])
        self.assertEqual(result["gamma"]["cpu_time_s"], "NA")
        self.assertIn("cpu_time_s is missing or invalid",
                      result["gamma"]["measurement_exclusion_reason"])
        self.assertEqual(result["alpha"]["expected_cpu_pct"], "NA")
        self.assertIn("beta:", result["alpha"]["allocation_unavailable_reason"])

    def test_incomplete_measurement_is_excluded_from_jain_and_allocation(self):
        rows = [
            {"run_id": "synthetic_test_only_incomplete", "group": "alpha", "cpu_time_s": "5",
             "measurement_status": "interrupted", "group_share": "1"},
            {"run_id": "synthetic_test_only_incomplete", "group": "beta", "cpu_time_s": "5",
             "measurement_status": "complete", "group_share": "1"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            result = self.evaluate(self.group_csv(directory, "synthetic_test_only_incomplete.csv", rows))
        self.assertEqual(result["alpha"]["cpu_time_s"], "NA")
        self.assertIn("measurement_status is not complete",
                      result["alpha"]["measurement_exclusion_reason"])
        self.assertEqual(result["alpha"]["jain_group_count"], 0)
        self.assertEqual(result["alpha"]["jain_fairness_index"], "NA")
        self.assertEqual(result["beta"]["allocation_error_pct_points"], "NA")
        self.assertEqual(result["beta"]["cpu_time_s"], "NA")
        self.assertEqual(result["beta"]["observed_cpu_pct"], "NA")

    def test_incomplete_group_excludes_surviving_groups_from_run_metrics(self):
        rows = [
            {"run_id": "synthetic_test_only_partial", "group": "alpha", "cpu_time_s": "5",
             "measurement_status": "complete", "group_share": "1"},
            {"run_id": "synthetic_test_only_partial", "group": "beta", "cpu_time_s": "5",
             "measurement_status": "complete", "group_share": "1"},
            {"run_id": "synthetic_test_only_partial", "group": "gamma", "cpu_time_s": "NA",
             "measurement_status": "interrupted", "group_share": "1"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            result = self.evaluate(self.group_csv(
                directory, "synthetic_test_only_partial.csv", rows))
        for group in ("alpha", "beta", "gamma"):
            self.assertEqual(result[group]["cpu_time_s"], "NA")
            self.assertEqual(result[group]["observed_cpu_pct"], "NA")
            self.assertEqual(result[group]["jain_fairness_index"], "NA")
            self.assertEqual(result[group]["allocation_error_pct_points"], "NA")

    def test_custom_run_rejects_missing_expected_group_row(self):
        rows = self.custom_rows()[:1]
        with tempfile.TemporaryDirectory() as directory:
            result = self.evaluate(self.group_csv(directory, "custom_missing.csv", rows))
        self.assertIn("beta", result)
        self.assertEqual(result["alpha"]["observed_cpu_pct"], "NA")
        self.assertIn("expected group row 'beta' is missing",
                      result["alpha"]["allocation_unavailable_reason"])

    def test_custom_run_rejects_duplicate_group_row(self):
        rows = self.custom_rows()
        rows.append(dict(rows[0]))
        with tempfile.TemporaryDirectory() as directory:
            result = self.evaluate(self.group_csv(directory, "custom_duplicate.csv", rows))
        self.assertEqual(result["alpha"]["observed_cpu_pct"], "NA")
        self.assertIn("result row count does not match expected group count",
                      result["alpha"]["allocation_unavailable_reason"])
        self.assertIn("expected group row 'alpha' is duplicated",
                      result["alpha"]["allocation_unavailable_reason"])

    def test_custom_run_rejects_mismatched_share_and_group_metadata(self):
        rows = self.custom_rows()
        rows[1]["group_share"] = "200"
        with tempfile.TemporaryDirectory() as directory:
            result = self.evaluate(self.group_csv(directory, "custom_share_mismatch.csv", rows))
        self.assertEqual(result["alpha"]["expected_cpu_pct"], "NA")
        self.assertIn("configured share mismatch for group 'beta'",
                      result["alpha"]["allocation_unavailable_reason"])

    def test_custom_run_rejects_cgroup_identity_mismatch(self):
        rows = self.custom_rows()
        rows[1]["verified_cgroup_id"] = "999"
        with tempfile.TemporaryDirectory() as directory:
            result = self.evaluate(self.group_csv(directory, "custom_id_mismatch.csv", rows))
        self.assertEqual(result["alpha"]["cpu_time_s"], "NA")
        self.assertIn("cgroup identity mismatch for group 'beta'",
                      result["alpha"]["allocation_unavailable_reason"])

    def test_custom_run_rejects_unattached_or_unclean_shutdown(self):
        for field, value, reason in (
            ("scheduler_attached", "false", "scheduler attachment was not confirmed"),
            ("scheduler_shutdown_status", "exit_9", "scheduler shutdown was not confirmed clean"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                rows = self.custom_rows()
                rows[0][field] = value
                result = self.evaluate(self.group_csv(directory, "custom_lifecycle.csv", rows))
                self.assertEqual(result["alpha"]["observed_cpu_pct"], "NA")
                self.assertIn(reason, result["alpha"]["allocation_unavailable_reason"])

    def test_legacy_custom_run_without_expected_metadata_is_explicitly_excluded(self):
        rows = self.custom_rows()
        for row in rows:
            for field in ("custom_scheduler_schema_version", "expected_group_count",
                          "expected_group_config_json"):
                row.pop(field)
        with tempfile.TemporaryDirectory() as directory:
            result = self.evaluate(self.group_csv(directory, "custom_legacy.csv", rows))
        self.assertEqual(result["alpha"]["cpu_time_s"], "NA")
        self.assertIn("legacy custom-scheduler result lacks expected-group configuration metadata",
                      result["alpha"]["allocation_unavailable_reason"])

    def test_custom_incomplete_run_preserves_reason_but_has_no_metrics(self):
        rows = self.custom_rows()
        rows[1]["measurement_status"] = "incomplete"
        rows[1]["cpu_time_s"] = "NA"
        with tempfile.TemporaryDirectory() as directory:
            result = self.evaluate(self.group_csv(directory, "custom_incomplete.csv", rows))
        self.assertEqual(result["alpha"]["observed_cpu_pct"], "NA")
        self.assertEqual(result["beta"]["cpu_time_s"], "NA")
        self.assertIn("measurement_status is not complete",
                      result["beta"]["measurement_exclusion_reason"])

    def test_actual_cgroup_cpu_weight_schema_uses_counter_snapshots(self):
        rows = []
        for group, before, after in (("A", 100, 2000100), ("B", 0, 1000000)):
            row = {column: "NA" for column in CGROUP_COLUMNS}
            for counter in ("user_usec", "system_usec", "nr_periods",
                            "nr_throttled", "throttled_usec"):
                row[f"{counter}_before"] = "0"
                row[f"{counter}_after"] = "100"
                row[f"{counter}_delta"] = "100"
            row.update({"run_id": "synthetic_test_only_cgroup", "group": group,
                        "control_setting": "cpu.weight A=200 B=100", "workload_elapsed_s": "1",
                        "exit_status": "0", "measurement_status": "complete",
                        "cpu_stat_status": "complete", "usage_usec_before": str(before),
                        "usage_usec_after": str(after), "usage_usec_delta": str(after - before)})
            rows.append(row)
        with tempfile.TemporaryDirectory() as directory:
            result = self.evaluate(self.group_csv(directory, "synthetic_test_only_cgroup.csv", rows,
                                                   list(CGROUP_COLUMNS)))
        self.assertAlmostEqual(result["A"]["cpu_time_s"], 2)
        self.assertAlmostEqual(result["A"]["observed_cpu_pct"], 2 / 3 * 100)
        self.assertAlmostEqual(result["A"]["expected_cpu_pct"], 2 / 3 * 100)
        self.assertAlmostEqual(result["A"]["jain_fairness_index"], 0.9)

    def test_reset_cgroup_counter_is_excluded_even_if_csv_delta_claims_valid(self):
        rows = []
        for group, before, after in (("A", 100, 2000100), ("B", 1000, 0)):
            row = {column: "NA" for column in CGROUP_COLUMNS}
            for counter in ("user_usec", "system_usec", "nr_periods",
                            "nr_throttled", "throttled_usec"):
                row[f"{counter}_before"] = "0"
                row[f"{counter}_after"] = "100"
                row[f"{counter}_delta"] = "100"
            row.update({"run_id": "synthetic_test_only_reset", "group": group,
                        "control_setting": "cpu.weight A=200 B=100", "workload_elapsed_s": "1",
                        "exit_status": "0", "measurement_status": "complete",
                        "cpu_stat_status": "complete", "usage_usec_before": str(before),
                        "usage_usec_after": str(after), "usage_usec_delta": "999999"})
            rows.append(row)
        with tempfile.TemporaryDirectory() as directory:
            result = self.evaluate(self.group_csv(directory, "synthetic_test_only_reset.csv", rows,
                                                   list(CGROUP_COLUMNS)))
        self.assertEqual(result["B"]["cpu_time_s"], "NA")
        self.assertIn("counter reset", result["B"]["measurement_exclusion_reason"])
        self.assertEqual(result["A"]["allocation_error_pct_points"], "NA")

    def test_quota_and_legacy_group_counts_are_not_treated_as_shares(self):
        with tempfile.TemporaryDirectory() as directory:
            quota_rows = [
                {"run_id": "synthetic_test_only_quota", "group": "A",
                 "control_setting": "cpu.max A=50000/100000 B=100000/100000",
                 "workload_elapsed_s": "1", "exit_status": "0", "measurement_status": "complete",
                 "cpu_stat_status": "complete", "usage_usec_before": "0", "usage_usec_after": "100"},
                {"run_id": "synthetic_test_only_quota", "group": "B",
                 "control_setting": "cpu.max A=50000/100000 B=100000/100000",
                 "workload_elapsed_s": "1", "exit_status": "0", "measurement_status": "complete",
                 "cpu_stat_status": "complete", "usage_usec_before": "0", "usage_usec_after": "200"},
            ]
            quota = self.evaluate(self.group_csv(directory, "synthetic_test_only_quota.csv", quota_rows))
            self.assertEqual(quota["A"]["expected_cpu_pct"], "NA")
            self.assertIn("quota, not a relative group share",
                          quota["A"]["allocation_unavailable_reason"])

            group_rows = [
                {"run_id": "synthetic_test_only_counts", "group": "alpha", "cpu_time_s": "1",
                 "measurement_status": "complete", "group_counts": "alpha=2;beta=1"},
                {"run_id": "synthetic_test_only_counts", "group": "beta", "cpu_time_s": "1",
                 "measurement_status": "complete", "group_counts": "alpha=2;beta=1"},
            ]
            legacy = self.evaluate(self.group_csv(directory, "synthetic_test_only_counts.csv", group_rows))
            self.assertEqual(legacy["alpha"]["expected_cpu_pct"], "NA")
            self.assertIn("worker counts, not configured shares",
                          legacy["alpha"]["allocation_unavailable_reason"])

    def test_output_is_new_and_existing_or_input_files_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.group_csv(directory, "synthetic_test_only_output.csv", [
                {"run_id": "synthetic_test_only_output", "group": "alpha", "cpu_time_s": "1",
                 "measurement_status": "complete", "group_share": "1"},
                {"run_id": "synthetic_test_only_output", "group": "beta", "cpu_time_s": "1",
                 "measurement_status": "complete", "group_share": "1"},
            ])
            with open(source, encoding="utf-8") as stream:
                original = stream.read()
            output = os.path.join(directory, "evaluated.csv")
            result = subprocess.run([sys.executable, SCRIPT, source, "--output", output],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(os.path.exists(output))
            with open(source, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), original)

            existing = os.path.join(directory, "existing.csv")
            with open(existing, "w", encoding="utf-8") as stream:
                stream.write("preserve\n")
            result = subprocess.run([sys.executable, SCRIPT, source, "--output", existing],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            with open(existing, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "preserve\n")

            result = subprocess.run([sys.executable, SCRIPT, source, "--output", source],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            with open(source, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), original)


if __name__ == "__main__":
    unittest.main()
