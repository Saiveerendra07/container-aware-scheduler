import csv
import os
import subprocess
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "generate_experiment_report.sh")


class GenerateExperimentReportTests(unittest.TestCase):
    def run_report(self, *args):
        return subprocess.run(
            ["bash", SCRIPT, *args], cwd=ROOT, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def test_help_and_invalid_options(self):
        help_result = self.run_report("--help")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("No workloads are run", help_result.stdout)
        invalid = self.run_report("--unknown")
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("unknown option", invalid.stderr)

    def test_empty_results_create_summary_but_no_graphs(self):
        columns = [
            "run_id", "timestamp_utc", "kernel_version", "logical_cpu_count",
            "requested_duration_s", "group_counts", "worker_id", "group",
            "group_worker_index", "supervisor_pid", "worker_pid", "user_cpu_s",
            "system_cpu_s", "cpu_time_s", "elapsed_s", "iterations",
            "exit_status", "termination_signal", "measurement_status", "output_log",
        ]
        with tempfile.TemporaryDirectory() as directory:
            raw = os.path.join(directory, "empty.csv")
            processed = os.path.join(directory, "processed")
            graphs = os.path.join(directory, "graphs")
            with open(raw, "w", newline="") as stream:
                csv.writer(stream).writerow(columns)

            result = self.run_report(
                "--processed-dir", processed,
                "--graphs-dir", graphs,
                raw,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("no usable", result.stderr.lower())
            summaries = []
            for parent, _, names in os.walk(processed):
                summaries.extend(os.path.join(parent, name) for name in names
                                 if name == "analyzed_results.csv")
            self.assertEqual(len(summaries), 1)
            self.assertFalse(os.path.exists(graphs))
            self.assertIn(summaries[0], result.stdout)


if __name__ == "__main__":
    unittest.main()
