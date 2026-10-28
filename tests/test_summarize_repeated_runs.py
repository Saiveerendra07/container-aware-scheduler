"""Synthetic test-only coverage for compatible repeated-run summaries."""

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "summarize_repeated_runs.py"


def load_module():
    spec = importlib.util.spec_from_file_location("repeated_run_summary", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIELDS = ["run_id", "timestamp_utc", "kernel_version", "logical_cpu_count", "architecture",
          "requested_duration_s", "workload", "control_setting", "group", "cgroup_path",
          "workload_elapsed_s", "exit_status", "measurement_status", "cpu_stat_status"]
COUNTERS = ("usage_usec", "user_usec", "system_usec", "nr_periods",
            "nr_throttled", "throttled_usec")
for _counter in COUNTERS:
    FIELDS.extend([f"{_counter}_before", f"{_counter}_after", f"{_counter}_delta"])


def write_run(path, run_id, cpu_times, *, duration="1", kernel="test-kernel",
              control="cpu.weight a=100 b=300",
              incomplete=False, missing_cpu=False):
    with path.open("a", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        if path.stat().st_size == 0:
            writer.writeheader()
        for group, cpu_time in zip(("a", "b"), cpu_times):
            cpu_usec = int(cpu_time * 1_000_000)
            values = {"run_id": run_id, "timestamp_utc": "synthetic",
                      "kernel_version": kernel, "logical_cpu_count": 4,
                      "architecture": "x86_64",
                      "requested_duration_s": duration, "workload": "cpu-bound",
                      "control_setting": control,
                      "group": group, "cgroup_path": f"/synthetic/{group}",
                      "workload_elapsed_s": "2", "exit_status": "0",
                      "measurement_status": "incomplete" if incomplete else "complete",
                      "cpu_stat_status": "complete"}
            for counter in COUNTERS:
                before, after = 100, 101
                if counter in ("usage_usec", "user_usec"):
                    after = before + cpu_usec
                elif counter == "system_usec":
                    after = before
                elif counter == "nr_periods":
                    after = before + 10
                elif counter == "nr_throttled":
                    after = before + 2
                elif counter == "throttled_usec":
                    after = before + 50_000
                values[f"{counter}_before"] = before
                values[f"{counter}_after"] = after
                values[f"{counter}_delta"] = after - before
            if missing_cpu and group == "a":
                values["usage_usec_after"] = "NA"
                values["usage_usec_delta"] = "NA"
                values["cpu_stat_status"] = "partial"
            writer.writerow(values)


class RepeatedRunSummaryTests(unittest.TestCase):
    def custom_rows(self, run_id="custom"):
        expected = [
            {"name": "a", "cgroup_id": 11, "cgroup_path": "/synthetic/a", "share": 100},
            {"name": "b", "cgroup_id": 22, "cgroup_path": "/synthetic/b", "share": 300},
        ]
        config = json.dumps({"expected_group_count": 2, "fallback_share": 50,
                             "groups": expected}, sort_keys=True, separators=(",", ":"))
        rows = []
        for group in expected:
            rows.append({
                "run_id": run_id, "group": group["name"],
                "group_share": group["share"], "fallback_share": 50,
                "kernel_version": "test-kernel", "logical_cpu_count": 4,
                "architecture": "x86_64", "worker_executable": "/worker/cpu",
                "cgroup_id": group["cgroup_id"], "cgroup_path": group["cgroup_path"],
                "verified_cgroup_id": group["cgroup_id"],
                "identity_verification_status": "verified",
                "cgroup_cpu_weight": 100, "cgroup_cpu_max": "max 100000",
                "requested_duration_s": 1, "cpu_time_s": 1 if group["name"] == "a" else 3,
                "workload_elapsed_s": 2, "completion_status": "complete",
                "exit_status": 0, "termination_signal": 0,
                "measurement_status": "complete", "scheduler_attached": "true",
                "scheduler_shutdown_status": "stopped",
                "custom_scheduler_schema_version": "2", "expected_group_count": 2,
                "expected_group_config_json": config,
            })
        return rows

    def write_custom_csv(self, path, rows):
        fields = list(rows[0])
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_repeated_runs_calculate_statistics_and_fairness_metrics(self):
        summary = load_module()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            source = Path(temp) / "runs.csv"
            for index, pair in enumerate(((1.0, 3.0), (3.0, 5.0)), start=1):
                write_run(source, f"r{index}", pair)
            rows, exclusions, warnings = summary.summarize([str(source)])
            self.assertEqual(exclusions, [])
            self.assertEqual(warnings, [])
            cpu = next(row for row in rows if row["group"] == "a" and row["metric"] == "cpu_time_s")
            self.assertEqual(cpu["sample_count"], 2)
            self.assertAlmostEqual(cpu["mean"], 2.0)
            self.assertAlmostEqual(cpu["median"], 2.0)
            self.assertAlmostEqual(cpu["standard_deviation"], 2 ** 0.5)
            throttled = next(row for row in rows if row["group"] == "a" and row["metric"] == "throttled_time_s")
            self.assertEqual(throttled["sample_count"], 2)
            weighted = next(row for row in rows if row["group"] == "a" and row["metric"] == "weighted_allocation_error_pct_points")
            self.assertEqual(weighted["sample_count"], 2)
            self.assertEqual(next(row for row in rows if row["metric"] == "jain_fairness_index")["sample_count"], 2)

    def test_incompatible_workload_configurations_are_separate_groups(self):
        summary = load_module()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            source = Path(temp) / "runs.csv"
            write_run(source, "duration_one", (1, 3), duration="1")
            write_run(source, "duration_two", (1, 3), duration="2")
            rows, exclusions, _warnings = summary.summarize([str(source)])
            self.assertFalse(exclusions)
            cpu_rows = [row for row in rows if row["metric"] == "cpu_time_s" and row["group"] == "a"]
            self.assertEqual(len({row["configuration_id"] for row in cpu_rows}), 2)
            self.assertEqual({row["sample_count"] for row in cpu_rows}, {1})
            self.assertTrue(all(row["standard_deviation"] == "NA" for row in cpu_rows))

    def test_different_kernel_environments_are_never_pooled(self):
        summary = load_module()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            source = Path(temp) / "runs.csv"
            write_run(source, "kernel_a", (1, 3), kernel="kernel-a")
            write_run(source, "kernel_b", (1, 3), kernel="kernel-b")
            rows, exclusions, _warnings = summary.summarize([str(source)])
            self.assertFalse(exclusions)
            cpu_rows = [row for row in rows if row["metric"] == "cpu_time_s" and row["group"] == "a"]
            self.assertEqual({row["sample_count"] for row in cpu_rows}, {1})
            self.assertEqual(len({row["configuration_id"] for row in cpu_rows}), 2)

    def test_inconsistent_architecture_within_run_is_excluded(self):
        summary = load_module()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            source = Path(temp) / "runs.csv"
            write_run(source, "mixed_arch", (1, 3))
            contents = source.read_text()
            contents = contents.replace("x86_64", "aarch64", 1)
            source.write_text(contents)
            _rows, exclusions, _warnings = summary.summarize([str(source)])
            self.assertEqual(len(exclusions), 1)
            self.assertIn("inconsistent architecture values within run", exclusions[0]["reasons"])

    def test_inconsistent_workload_within_run_is_excluded(self):
        summary = load_module()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            source = Path(temp) / "runs.csv"
            write_run(source, "mixed_workload", (1, 3))
            with source.open("r", encoding="utf-8") as stream:
                lines = stream.readlines()
            lines[2] = lines[2].replace("cpu-bound", "memory-bound")
            source.write_text("".join(lines))
            _rows, exclusions, _warnings = summary.summarize([str(source)])
            self.assertEqual(len(exclusions), 1)
            self.assertIn("inconsistent workload values within run", exclusions[0]["reasons"])

    def test_negative_and_missing_environment_values_are_not_valid_samples(self):
        summary = load_module()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            source = Path(temp) / "runs.csv"
            write_run(source, "negative", (-1, 3))
            rows, exclusions, _warnings = summary.summarize([str(source)])
            self.assertEqual(len(exclusions), 1)
            self.assertIn("cpu_time_s is missing or invalid", exclusions[0]["reasons"])
            with source.open("a", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDS)
                for group, cpu in (("a", 1), ("b", 3)):
                    row = {field: "NA" for field in FIELDS}
                    row.update({"run_id": "unknown_env", "timestamp_utc": "synthetic",
                                "kernel_version": "NA", "logical_cpu_count": "NA",
                                "architecture": "NA", "requested_duration_s": "1",
                                "control_setting": "cpu.weight a=100 b=300", "group": group,
                                "cgroup_path": f"/synthetic/{group}", "workload_elapsed_s": "2",
                                "exit_status": "0", "measurement_status": "complete",
                                "cpu_stat_status": "complete"})
                    for counter in COUNTERS:
                        row[f"{counter}_before"] = "0"
                        row[f"{counter}_after"] = str(int(cpu * 1_000_000) if counter == "usage_usec" else 1)
                    writer.writerow(row)
            _rows, exclusions, _warnings = summary.summarize([str(source)])
            unknown = next(item for item in exclusions if item["run_id"] == "unknown_env")
            self.assertIn("kernel_version environment metadata is unavailable", unknown["reasons"])

    def test_custom_scheduler_runs_require_clean_attach_shutdown_and_include_weighted_error(self):
        summary = load_module()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            source = Path(temp) / "custom.csv"
            fields = ["run_id", "group", "group_share", "fallback_share",
                      "kernel_version", "logical_cpu_count", "architecture", "worker_executable",
                      "cgroup_id", "cgroup_path", "verified_cgroup_id",
                      "identity_verification_status", "custom_scheduler_schema_version",
                      "expected_group_count", "expected_group_config_json",
                      "cgroup_cpu_weight", "cgroup_cpu_max",
                      "requested_duration_s", "cpu_time_s", "workload_elapsed_s", "completion_status",
                      "exit_status", "termination_signal", "measurement_status",
                      "scheduler_attached", "scheduler_shutdown_status"]
            with source.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                expected_config = json.dumps({
                    "expected_group_count": 2, "fallback_share": 50,
                    "groups": [
                        {"name": "a", "cgroup_id": 11, "cgroup_path": "/synthetic/a", "share": 100},
                        {"name": "b", "cgroup_id": 22, "cgroup_path": "/synthetic/b", "share": 300},
                    ],
                }, sort_keys=True, separators=(",", ":"))
                for run_id, values in (("one", (1, 3)), ("two", (2, 6))):
                    for group, share, cpu in (("a", 100, values[0]), ("b", 300, values[1])):
                        writer.writerow({"run_id": run_id, "group": group,
                                         "group_share": share, "fallback_share": 50,
                                         "kernel_version": "test-kernel", "logical_cpu_count": 4,
                                         "architecture": "x86_64", "worker_executable": "/worker/cpu",
                                         "cgroup_id": 11 if group == "a" else 22,
                                         "cgroup_path": f"/synthetic/{group}",
                                         "verified_cgroup_id": 11 if group == "a" else 22,
                                         "identity_verification_status": "verified",
                                         "custom_scheduler_schema_version": "2",
                                         "expected_group_count": 2,
                                         "expected_group_config_json": expected_config,
                                         "cgroup_cpu_weight": 100, "cgroup_cpu_max": "max 100000",
                                         "requested_duration_s": 1, "cpu_time_s": cpu,
                                         "workload_elapsed_s": 2, "completion_status": "complete",
                                         "exit_status": 0,
                                         "termination_signal": 0,
                                         "measurement_status": "complete",
                                         "scheduler_attached": "true",
                                         "scheduler_shutdown_status": "stopped"})
            rows, exclusions, warnings = summary.summarize([str(source)])
            self.assertEqual(exclusions, [])
            self.assertEqual(warnings, [])
            weighted = next(row for row in rows if row["group"] == "a"
                            and row["metric"] == "weighted_allocation_error_pct_points")
            self.assertEqual(weighted["sample_count"], 2)
            self.assertAlmostEqual(weighted["mean"], 0.0)

            # Workload executable identity and per-group CPU controls are part
            # of configuration compatibility, even when measurements match.
            with source.open("a", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                for run_id, worker, weight in (("other_worker", "/worker/other", 100),
                                               ("other_control", "/worker/cpu", 200)):
                    for group, share, cpu in (("a", 100, 1), ("b", 300, 3)):
                        writer.writerow({"run_id": run_id, "group": group,
                                         "group_share": share, "fallback_share": 50,
                                         "kernel_version": "test-kernel", "logical_cpu_count": 4,
                                         "architecture": "x86_64", "worker_executable": worker,
                                         "cgroup_id": 11 if group == "a" else 22,
                                         "cgroup_path": f"/synthetic/{group}",
                                         "verified_cgroup_id": 11 if group == "a" else 22,
                                         "identity_verification_status": "verified",
                                         "custom_scheduler_schema_version": "2",
                                         "expected_group_count": 2,
                                         "expected_group_config_json": expected_config,
                                         "cgroup_cpu_weight": weight, "cgroup_cpu_max": "max 100000",
                                         "requested_duration_s": 1, "cpu_time_s": cpu,
                                         "workload_elapsed_s": 2, "completion_status": "complete",
                                         "exit_status": 0,
                                         "termination_signal": 0, "measurement_status": "complete",
                                         "scheduler_attached": "true", "scheduler_shutdown_status": "stopped"})
            rows, exclusions, _warnings = summary.summarize([str(source)])
            self.assertFalse(exclusions)
            cpu_rows = [row for row in rows if row["group"] == "a" and row["metric"] == "cpu_time_s"]
            self.assertEqual(len({row["configuration_id"] for row in cpu_rows}), 3)
            self.assertEqual(sorted(row["sample_count"] for row in cpu_rows), [1, 1, 2])

            # A teardown error invalidates otherwise plausible measurements.
            with source.open("a", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                for group, share, cpu in (("a", 100, 1), ("b", 300, 3)):
                    writer.writerow({"run_id": "bad_shutdown", "group": group,
                                     "group_share": share, "fallback_share": 50,
                                     "kernel_version": "test-kernel", "logical_cpu_count": 4,
                                     "architecture": "x86_64", "worker_executable": "/worker/cpu",
                                     "cgroup_id": 11 if group == "a" else 22,
                                     "cgroup_path": f"/synthetic/{group}",
                                     "verified_cgroup_id": 11 if group == "a" else 22,
                                     "identity_verification_status": "verified",
                                     "custom_scheduler_schema_version": "2",
                                     "expected_group_count": 2,
                                     "expected_group_config_json": expected_config,
                                     "cgroup_cpu_weight": 100, "cgroup_cpu_max": "max 100000",
                                     "requested_duration_s": 1, "cpu_time_s": cpu,
                                     "workload_elapsed_s": 2, "completion_status": "complete",
                                     "exit_status": 0,
                                     "termination_signal": 0,
                                     "measurement_status": "complete",
                                     "scheduler_attached": "true",
                                     "scheduler_shutdown_status": "exit_9"})
            rows, exclusions, _warnings = summary.summarize([str(source)])
            self.assertTrue(any("shutdown" in ";".join(item["reasons"])
                                for item in exclusions))

    def test_custom_run_integrity_errors_are_excluded(self):
        summary = load_module()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            scenarios = []
            missing = self.custom_rows("missing")[:1]
            scenarios.append(("missing", missing, "expected group row 'b' is missing"))
            duplicate = self.custom_rows("duplicate")
            duplicate.append(dict(duplicate[0]))
            scenarios.append(("duplicate", duplicate, "expected group row 'a' is duplicated"))
            mismatch = self.custom_rows("mismatch")
            mismatch[1]["group_share"] = 200
            scenarios.append(("mismatch", mismatch, "configured share mismatch for group 'b'"))
            identity_mismatch = self.custom_rows("identity_mismatch")
            identity_mismatch[1]["verified_cgroup_id"] = 999
            scenarios.append(("identity_mismatch", identity_mismatch,
                              "cgroup identity mismatch for group 'b'"))
            inconsistent = self.custom_rows("inconsistent")
            config = json.loads(inconsistent[1]["expected_group_config_json"])
            config["fallback_share"] = 40
            inconsistent[1]["expected_group_config_json"] = json.dumps(
                config, sort_keys=True, separators=(",", ":"))
            scenarios.append(("inconsistent", inconsistent,
                              "expected group configuration differs between result rows"))
            malformed = self.custom_rows("malformed")
            for row in malformed:
                row["expected_group_config_json"] = json.dumps({"groups": "invalid"})
            scenarios.append(("malformed", malformed,
                              "expected groups configuration is missing or invalid"))
            legacy = self.custom_rows("legacy")
            for row in legacy:
                row.pop("expected_group_config_json")
                row.pop("expected_group_count")
                row.pop("custom_scheduler_schema_version")
            scenarios.append(("legacy", legacy,
                              "legacy custom-scheduler result lacks expected-group configuration metadata"))
            incomplete = self.custom_rows("incomplete")
            incomplete[1]["measurement_status"] = "incomplete"
            incomplete[1]["cpu_time_s"] = "NA"
            scenarios.append(("incomplete", incomplete, "measurement_status is incomplete"))

            for name, rows, expected_reason in scenarios:
                source = root / f"{name}.csv"
                self.write_custom_csv(source, rows)
                _summary, exclusions, _warnings = summary.summarize([str(source)])
                self.assertEqual(len(exclusions), 1, name)
                self.assertTrue(any(expected_reason in reason
                                    for reason in exclusions[0]["reasons"]), name)

    def test_incomplete_and_missing_counter_runs_are_excluded_with_reasons(self):
        summary = load_module()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            source = Path(temp) / "runs.csv"
            write_run(source, "valid", (1, 3))
            write_run(source, "incomplete", (1, 3), incomplete=True)
            write_run(source, "missing", (1, 3), missing_cpu=True)
            rows, exclusions, _warnings = summary.summarize([str(source)])
            self.assertEqual(len(exclusions), 2)
            self.assertTrue(any("measurement_status is incomplete" in ";".join(item["reasons"])
                                for item in exclusions))
            self.assertTrue(any("cpu_stat_status" in ";".join(item["reasons"])
                                for item in exclusions))
            cpu = next(row for row in rows if row["metric"] == "cpu_time_s" and row["group"] == "a")
            self.assertEqual(cpu["sample_count"], 1)
            self.assertEqual(cpu["excluded_run_count"], 2)

    def test_all_invalid_inputs_still_report_exclusions_and_unavailable_statistics(self):
        summary = load_module()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            source = Path(temp) / "runs.csv"
            write_run(source, "bad", (1, 3), incomplete=True)
            rows, exclusions, _warnings = summary.summarize([str(source)])
            self.assertEqual(len(exclusions), 1)
            self.assertTrue(any(row["metric"] == "run_exclusion" for row in rows))
            cpu = next(row for row in rows if row["metric"] == "cpu_time_s" and row["group"] == "a")
            self.assertEqual(cpu["sample_count"], 0)
            self.assertEqual(cpu["mean"], "NA")
            self.assertEqual(cpu["standard_deviation"], "NA")

    def test_outputs_are_unique_and_do_not_overwrite_existing_files(self):
        summary = load_module()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            source = Path(temp) / "runs.csv"
            write_run(source, "one", (1, 3))
            rows, exclusions, warnings = summary.summarize([str(source)])
            csv_one, md_one = summary.unique_outputs(Path(temp) / "out")
            summary.write_csv(csv_one, rows)
            summary.write_markdown(md_one, rows, exclusions, warnings)
            csv_two, md_two = summary.unique_outputs(Path(temp) / "out")
            self.assertNotEqual(csv_one, csv_two)
            self.assertNotEqual(md_one, md_two)
            self.assertEqual(csv_one.read_text().splitlines()[0], ",".join(summary.SUMMARY_FIELDS))


if __name__ == "__main__":
    unittest.main()
