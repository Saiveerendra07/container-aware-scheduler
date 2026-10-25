import csv
import importlib.util
import os
import tempfile
import unittest
import sys


SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "analyze_results.py")
SPEC = importlib.util.spec_from_file_location("analyze_results", SCRIPT)
analyzer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analyzer)
PLOT_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "plot_results.py")
PLOT_SPEC = importlib.util.spec_from_file_location("plot_results", PLOT_SCRIPT)
plotter = importlib.util.module_from_spec(PLOT_SPEC)
PLOT_SPEC.loader.exec_module(plotter)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from cgroup_metrics import COLUMNS as CGROUP_COLUMNS, write_csv as write_cgroup_csv


class AnalyzeResultsTests(unittest.TestCase):
    def test_usage_and_fairness_from_runner_columns(self):
        rows = [
            {"run_id": "r", "worker_id": "w1", "group": "latency",
             "cpu_time_s": "1", "elapsed_s": "2", "measurement_status": "complete"},
            {"run_id": "r", "worker_id": "w2", "group": "batch",
             "cpu_time_s": "3", "elapsed_s": "3", "measurement_status": "complete"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "input.csv")
            import csv
            with open(path, "w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            result, warnings = analyzer.analyze([path])
        self.assertEqual(warnings, [])
        self.assertAlmostEqual(result[0]["cpu_usage_pct"], 50)
        self.assertAlmostEqual(result[1]["cpu_usage_pct"], 100)
        self.assertAlmostEqual(result[0]["jain_fairness_index"], 16 / 20)

    def test_missing_and_malformed_measurements_are_unavailable(self):
        rows = [
            {"run_id": "r", "worker_id": "w1", "group": "x",
             "cpu_time_s": "bad", "elapsed_s": "1", "measurement_status": "incomplete"},
            {"run_id": "r", "worker_id": "w2", "group": "x",
             "cpu_time_s": "2", "elapsed_s": "0", "measurement_status": "incomplete"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "input.csv")
            import csv
            with open(path, "w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            result, _ = analyzer.analyze([path])
        self.assertIsNone(result[0]["cpu_usage_pct"])
        self.assertIsNone(result[1]["cpu_usage_pct"])
        self.assertEqual(result[0]["fairness_workers"], 1)
        self.assertEqual(result[0]["jain_fairness_index"], 1)

    def test_real_cgroup_schema_derives_group_cpu_and_throttling_metrics(self):
        row = {column: "NA" for column in CGROUP_COLUMNS}
        row.update({"run_id": "cg1", "timestamp_utc": "2026-10-23T00:00:00Z",
                    "group": "A", "workload_elapsed_s": "2.00", "exit_status": "0",
                    "measurement_status": "complete", "cpu_stat_status": "complete",
                    "control_setting": "cpu.max A=50000/100000 B=100000/100000"})
        for counter, before, after in (
            ("usage_usec", 100, 1000100), ("user_usec", 90, 900090),
            ("system_usec", 10, 100010), ("nr_periods", 10, 30),
            ("nr_throttled", 2, 7), ("throttled_usec", 100, 250100),
        ):
            row[f"{counter}_before"] = str(before)
            row[f"{counter}_after"] = str(after)
            row[f"{counter}_delta"] = "999999999"  # analyzer must derive from snapshots
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cgroup_competition_test.csv")
            with open(path, "w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=CGROUP_COLUMNS)
                writer.writeheader()
                writer.writerow(row)
            result, warnings = analyzer.analyze([path])
        self.assertEqual(warnings, [])
        measured = result[0]
        self.assertEqual(measured["result_type"], "cgroup_competition")
        self.assertEqual(measured["exit_status"], "0")
        self.assertAlmostEqual(measured["cpu_time_s"], 1)
        self.assertAlmostEqual(measured["cpu_usage_pct"], 50)
        self.assertEqual(measured["nr_throttled_delta"], 5)
        self.assertAlmostEqual(measured["throttled_time_s"], .25)
        self.assertAlmostEqual(measured["throttled_period_pct"], 25)
        self.assertEqual(measured["usage_usec_delta"], 1000000)
        self.assertEqual(measured["cpu_stat_status"], "complete")
        self.assertIsNone(measured["jain_fairness_index"])

    def test_cgroup_na_reset_and_incomplete_rows_remain_unavailable(self):
        row = {column: "NA" for column in CGROUP_COLUMNS}
        row.update({"run_id": "cg2", "group": "B", "workload_elapsed_s": "NA",
                    "exit_status": "143", "measurement_status": "incomplete",
                    "cpu_stat_status": "partial", "usage_usec_before": "10",
                    "usage_usec_after": "NA", "usage_usec_delta": "1000000",
                    "nr_periods_before": "10", "nr_periods_after": "20",
                    "nr_periods_delta": "10", "nr_throttled_before": "15",
                    "nr_throttled_after": "10", "nr_throttled_delta": "bad",
                    "throttled_usec_delta": "-1"})
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cgroup_competition_test.csv")
            with open(path, "w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=CGROUP_COLUMNS)
                writer.writeheader()
                writer.writerow(row)
            result, _ = analyzer.analyze([path])
        measured = result[0]
        self.assertEqual(measured["measurement_status"], "incomplete")
        self.assertEqual(measured["exit_status"], "143")
        self.assertIsNone(measured["cpu_time_s"])
        self.assertIsNone(measured["cpu_usage_pct"])
        self.assertIsNone(measured["nr_throttled_delta"])
        self.assertIsNone(measured["throttled_time_s"])
        self.assertEqual(measured["cpu_stat_status"], "partial")
        self.assertEqual(measured["usage_usec_delta"], "NA")
        with tempfile.TemporaryDirectory() as directory:
            summary_path = os.path.join(directory, "summary.csv")
            analyzer.write_summary(summary_path, result)
            with open(summary_path, newline="", encoding="utf-8") as stream:
                summary = next(csv.DictReader(stream))
        self.assertEqual(summary["measurement_status"], "incomplete")
        self.assertEqual(summary["exit_status"], "143")
        self.assertEqual(summary["usage_usec_delta"], "NA")
        self.assertEqual(summary["cpu_usage_pct"], "NA")

    def test_actual_writer_csv_flows_through_analyzer_summary_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            before = os.path.join(directory, "before.stat")
            after = os.path.join(directory, "after.stat")
            metadata = os.path.join(directory, "metadata.tsv")
            raw = os.path.join(directory, "cgroup_competition_run.csv")
            for path, values in ((before, (100, 90, 10, 4, 1, 200)),
                                 (after, (1000100, 900090, 100010, 24, 6, 250200))):
                with open(path, "w", encoding="ascii") as stream:
                    for counter, value in zip(("usage_usec", "user_usec", "system_usec",
                                               "nr_periods", "nr_throttled", "throttled_usec"), values):
                        stream.write(f"{counter} {value}\n")
            with open(metadata, "w", encoding="utf-8") as stream:
                stream.write(f"cg-run\t2026-10-23T00:00:00Z\tkernel\t2\tx86_64\t1.00\tcpu.weight A=100 B=200\tA\t/tmp/A\t1.00\t0\tcomplete\t{before}\t{after}\n")
            write_cgroup_csv(metadata, raw)
            rows, warnings = analyzer.analyze([raw])
            self.assertEqual(warnings, [])
            row = rows[0]
            self.assertEqual(row["measurement_status"], "complete")
            self.assertAlmostEqual(row["cpu_usage_pct"], 100)
            self.assertEqual(row["nr_throttled_delta"], 5)
            summary_path = os.path.join(directory, "summary.csv")
            analyzer.write_summary(summary_path, rows)
            with open(summary_path, newline="", encoding="utf-8") as stream:
                summary = next(csv.DictReader(stream))
            self.assertEqual(summary["result_type"], "cgroup_competition")
            self.assertEqual(summary["measurement_status"], "complete")
            self.assertEqual(summary["nr_throttled_delta"], "5")
            records, warnings = plotter.read_summaries([summary_path])
            cpu, fairness, cgroup, plot_warnings = plotter.plot_data(records)
            self.assertEqual(warnings, [])
            self.assertEqual(plot_warnings, [])
            self.assertEqual(cpu, [])
            self.assertEqual(fairness, {})
            self.assertEqual(len(cgroup), 1)
            self.assertEqual(cgroup[0]["usage"], 100)
            self.assertEqual(cgroup[0]["throttled_periods"], 5)

    def test_existing_group_schema_remains_compatible_alongside_cgroup_schema(self):
        old = {"run_id": "legacy", "worker_id": "w1", "group": "x",
               "cpu_time_s": "1", "elapsed_s": "2", "measurement_status": "complete"}
        cg = {column: "NA" for column in CGROUP_COLUMNS}
        cg.update({"run_id": "new", "group": "A", "workload_elapsed_s": "1",
                   "measurement_status": "complete", "usage_usec_delta": "500000",
                   "nr_periods_delta": "1", "nr_throttled_delta": "0",
                   "throttled_usec_delta": "0"})
        for counter, delta in (("usage_usec", 500000), ("user_usec", 500000),
                               ("system_usec", 0), ("nr_periods", 1),
                               ("nr_throttled", 0), ("throttled_usec", 0)):
            cg[f"{counter}_before"] = "0"
            cg[f"{counter}_after"] = str(delta)
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for name, data, fields in (("old.csv", [old], list(old)),
                                       ("cgroup.csv", [cg], list(cg))):
                path = os.path.join(directory, name)
                with open(path, "w", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(data)
                paths.append(path)
            rows, warnings = analyzer.analyze(paths)
        self.assertEqual(warnings, [])
        self.assertEqual([row["result_type"] for row in rows],
                         ["group_competition", "cgroup_competition"])
        self.assertAlmostEqual(rows[0]["cpu_usage_pct"], 50)
        self.assertAlmostEqual(rows[1]["cpu_usage_pct"], 50)

    def test_summary_does_not_overwrite_an_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            requested = os.path.join(directory, "summary.csv")
            with open(requested, "w") as stream:
                stream.write("preserve me\n")
            actual = analyzer.write_summary(requested, [])
            self.assertNotEqual(actual, requested)
            with open(requested) as stream:
                self.assertEqual(stream.read(), "preserve me\n")
            self.assertTrue(os.path.exists(actual))


if __name__ == "__main__":
    unittest.main()
