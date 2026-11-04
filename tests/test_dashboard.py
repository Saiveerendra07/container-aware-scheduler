import csv
import json
import tempfile
import unittest
from pathlib import Path

from dashboard.app import jain_index, load_experiments, scheduler_status


GROUP_FIELDS = [
    "run_id", "timestamp_utc", "worker_id", "group", "cpu_time_s", "elapsed_s",
    "iterations", "exit_status", "measurement_status",
]


class DashboardDataTests(unittest.TestCase):
    def write_csv(self, path, fields, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_group_run_calculates_group_allocation_and_fairness_without_inventing_weights(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_csv(root / "raw" / "groups.csv", GROUP_FIELDS, [
                {"run_id": "run-1", "timestamp_utc": "2026-10-01T12:00:00Z", "worker_id": "w1",
                 "group": "alpha", "cpu_time_s": "2", "elapsed_s": "4", "iterations": "20",
                 "exit_status": "0", "measurement_status": "complete"},
                {"run_id": "run-1", "timestamp_utc": "2026-10-01T12:00:00Z", "worker_id": "w2",
                 "group": "beta", "cpu_time_s": "1", "elapsed_s": "4", "iterations": "10",
                 "exit_status": "0", "measurement_status": "complete"},
            ])

            data = load_experiments(root)

        self.assertEqual(len(data["experiments"]), 1)
        run = data["experiments"][0]
        self.assertEqual(run["status"], "complete")
        self.assertEqual(run["metrics"]["cpu_time_s"], 3)
        self.assertEqual(run["metrics"]["throughput_per_s"], 7.5)
        self.assertAlmostEqual(run["metrics"]["jain_fairness_index"], 0.9)
        self.assertEqual(run["configured_shares"], {})
        self.assertIsNone(run["groups"][0]["expected_allocation_pct"])

    def test_group_counts_mismatch_makes_fairness_unavailable(self):
        fields = GROUP_FIELDS + ["group_counts"]
        rows = [
            {"run_id": "partial", "worker_id": "w1", "group": "alpha", "cpu_time_s": "1",
             "elapsed_s": "2", "exit_status": "0", "measurement_status": "complete",
             "group_counts": "alpha=2;beta=1"},
            {"run_id": "partial", "worker_id": "w2", "group": "beta", "cpu_time_s": "1",
             "elapsed_s": "2", "exit_status": "0", "measurement_status": "complete",
             "group_counts": "alpha=2;beta=1"},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_csv(root / "partial.csv", fields, rows)
            run = load_experiments(root)["experiments"][0]

        self.assertEqual(run["status"], "incomplete or unavailable")
        self.assertEqual(run["metrics"], {})

    def test_cgroup_csv_derives_cpu_time_only_from_valid_counter_delta(self):
        fields = [
            "run_id", "group", "workload_elapsed_s", "exit_status", "measurement_status",
            "cpu_stat_status", "usage_usec_before", "usage_usec_after", "control_setting",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_csv(root / "cgroup.csv", fields, [
                {"run_id": "cg-1", "group": "alpha", "workload_elapsed_s": "2", "exit_status": "0",
                 "measurement_status": "complete", "cpu_stat_status": "complete",
                 "usage_usec_before": "1000000", "usage_usec_after": "3000000",
                 "control_setting": "cpu.weight alpha=100 beta=200"},
                {"run_id": "cg-1", "group": "beta", "workload_elapsed_s": "2", "exit_status": "0",
                 "measurement_status": "complete", "cpu_stat_status": "complete",
                 "usage_usec_before": "500000", "usage_usec_after": "1500000",
                 "control_setting": "cpu.weight alpha=100 beta=200"},
            ])

            run = load_experiments(root)["experiments"][0]

        self.assertEqual(run["result_type"], "cgroup_competition")
        self.assertEqual(run["metrics"]["cpu_time_s"], 3)
        self.assertAlmostEqual(run["groups"][0]["expected_allocation_pct"], 100 / 3)
        self.assertAlmostEqual(run["groups"][0]["observed_allocation_pct"], 200 / 3)
        self.assertNotIn("jain_fairness_index", run["metrics"])

    def test_runner_text_records_expose_only_recorded_metrics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "raw"
            raw.mkdir()
            (raw / "cpu_baseline_20261001.txt").write_text(
                "Date/time: 2026-10-01T12:00:00+00:00\n"
                "Exit status: 0\n"
                "duration_seconds=2.000\n"
                "iterations=400\n",
                encoding="utf-8",
            )
            (raw / "cpu_competition_20261001.txt").write_text(
                "Timestamp: 2026-10-01T12:00:00+00:00\n"
                "Requested workers: 2\n"
                "Workers started: 2\n"
                "Worker 1 PID: 10\niterations=300\nWorker 1 exit status: 0\n"
                "Worker 2 PID: 11\niterations=500\nWorker 2 exit status: 0\n"
                "Total elapsed time (monotonic, seconds): 2.00\n",
                encoding="utf-8",
            )

            data = load_experiments(root)

        runs = {run["result_type"]: run for run in data["experiments"]}
        self.assertEqual(runs["cpu_baseline"]["metrics"], {"iterations": 400})
        self.assertEqual(runs["cpu_competition"]["metrics"]["iterations"], 800)
        self.assertEqual(runs["cpu_competition"]["metrics"]["elapsed_s"], 2)
        self.assertEqual(runs["cpu_competition"]["metrics"]["throughput_per_s"], 400)
        self.assertNotIn("cpu_time_s", runs["cpu_baseline"]["metrics"])

    def test_custom_run_requires_confirmed_attachment_and_consistent_configuration(self):
        config = {
            "expected_group_count": 2,
            "fallback_share": 100,
            "groups": [
                {"name": "alpha", "cgroup_id": 11, "cgroup_path": "/cg/alpha", "share": 100},
                {"name": "beta", "cgroup_id": 12, "cgroup_path": "/cg/beta", "share": 200},
            ],
        }
        rows = []
        for group in config["groups"]:
            rows.append({
                "run_id": "attached", "timestamp_utc": "2026-10-01T12:00:00Z",
                "group": group["name"], "group_share": group["share"], "fallback_share": 100,
                "scheduler_attached": "true", "measurement_status": "complete",
                "completion_status": "complete", "scheduler_shutdown_status": "stopped",
                "custom_scheduler_schema_version": "2", "expected_group_count": 2,
                "expected_group_config_json": json.dumps(config, sort_keys=True),
                "cgroup_id": group["cgroup_id"], "verified_cgroup_id": group["cgroup_id"],
                "cgroup_path": group["cgroup_path"], "identity_verification_status": "verified",
                "cpu_time_s": 1, "workload_elapsed_s": 2, "exit_status": 0,
                "termination_signal": 0,
            })
        unattached_rows = [dict(row, run_id="unattached", scheduler_attached="false") for row in rows]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_csv(root / "custom_scheduler_run.csv", list(rows[0]), rows + unattached_rows)
            data = load_experiments(root)

        runs = {run["run_id"]: run for run in data["experiments"]}
        self.assertEqual(runs["attached"]["status"], "complete")
        self.assertEqual(runs["attached"]["metrics"]["jain_fairness_index"], 1)
        self.assertEqual(runs["attached"]["configured_shares"], {"alpha": 100.0, "beta": 200.0})
        self.assertEqual(runs["unattached"]["status"], "incomplete or unavailable")
        self.assertEqual(runs["unattached"]["metrics"], {})

    def test_manifests_fixtures_malformed_csv_and_missing_directory_are_nonfatal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "fixtures" / "synthetic.csv"
            self.write_csv(fixture, GROUP_FIELDS, [])
            (root / "run.json").write_text(json.dumps({
                "run_id": "prepared-1", "mode": "prepare", "created_utc": "2026-10-02T00:00:00Z",
                "measurement_status": "not_run_prepared", "scheduler_attached": False,
            }), encoding="utf-8")
            (root / "broken.json").write_text("{not valid json", encoding="utf-8")
            (root / "broken.csv").write_text("run_id,group\nonly-one\n", encoding="utf-8")

            data = load_experiments(root)
            included = load_experiments(root, include_fixtures=True)
            missing = load_experiments(root / "absent")

        self.assertEqual(data["fixture_count"], 1)
        self.assertEqual(len(data["experiments"]), 0)
        self.assertTrue(any(item["status"] == "prepared; not run" for item in data["history"]))
        self.assertTrue(any("unsupported CSV schema" in warning for warning in data["warnings"]))
        self.assertTrue(any("could not read JSON manifest" in warning for warning in data["warnings"]))
        self.assertEqual(len(included["experiments"]), 0)
        self.assertTrue(any(item["source_kind"] == "test fixture" for item in included["history"]))
        self.assertTrue(missing["warnings"])

    def test_jain_index_and_read_only_scheduler_state(self):
        self.assertEqual(jain_index([3, 3]), 1)
        self.assertIsNone(jain_index([0, 0]))
        self.assertIsNone(jain_index([1]))
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            ops = Path(temporary) / "ops"
            state.write_text("enabled\n", encoding="ascii")
            ops.write_text("grp_cpu\n", encoding="ascii")
            self.assertEqual(scheduler_status(state, ops), "Running")
            ops.write_text("other_scheduler\n", encoding="ascii")
            self.assertEqual(scheduler_status(state, ops), "Not running")
            self.assertEqual(scheduler_status(state), "Unknown")
            state.write_text("disabled\n", encoding="ascii")
            self.assertEqual(scheduler_status(state), "Not running")
            self.assertEqual(scheduler_status(state.with_name("missing")), "Unknown")


if __name__ == "__main__":
    unittest.main()