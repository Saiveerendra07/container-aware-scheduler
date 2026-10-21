import csv
import importlib.util
import os
import tempfile
import unittest


SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "plot_results.py")
SPEC = importlib.util.spec_from_file_location("plot_results", SCRIPT)
plotter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plotter)


class PlotResultsTests(unittest.TestCase):
    def test_rejects_incompatible_analyzer_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "not_summary.csv")
            with open(path, "w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["worker", "cpu"])
                writer.writerow(["w1", "1"])
            rows, warnings = plotter.read_summaries([path])
        self.assertEqual(rows, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("not an analyzer summary", warnings[0])

    def test_empty_analyzer_summary_has_no_plot_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "empty.csv")
            with open(path, "w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=sorted(plotter.REQUIRED_COLUMNS))
                writer.writeheader()
            rows, warnings = plotter.read_summaries([path])
        self.assertEqual(warnings, [])
        cpu, fairness, data_warnings = plotter.plot_data(rows)
        self.assertEqual(cpu, [])
        self.assertEqual(fairness, {})
        self.assertEqual(data_warnings, [])

    def test_uses_measurements_and_omits_single_worker_fairness(self):
        records = [
            {"run_id": "multi", "worker_id": "w1", "group": "latency",
             "cpu_usage_pct": "75", "jain_fairness_index": "0.98", "fairness_workers": "2",
             "_summary_file": "summary.csv", "_line": 2},
            {"run_id": "solo", "worker_id": "w1", "group": "batch",
             "cpu_usage_pct": "NA", "jain_fairness_index": "1", "fairness_workers": "1",
             "_summary_file": "summary.csv", "_line": 3},
        ]
        cpu, fairness, warnings = plotter.plot_data(records)
        self.assertEqual(len(cpu), 1)
        self.assertEqual(cpu[0]["usage"], 75)
        self.assertEqual(fairness["multi"]["fairness"], 0.98)
        self.assertNotIn("solo", fairness)
        self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
