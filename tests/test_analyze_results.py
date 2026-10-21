import importlib.util
import os
import tempfile
import unittest


SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "analyze_results.py")
SPEC = importlib.util.spec_from_file_location("analyze_results", SCRIPT)
analyzer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analyzer)


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


if __name__ == "__main__":
    unittest.main()
