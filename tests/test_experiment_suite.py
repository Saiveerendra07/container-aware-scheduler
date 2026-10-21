import os
import subprocess
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "run_experiment_suite.sh")


class ExperimentSuiteTests(unittest.TestCase):
    def run_suite(self, *args):
        return subprocess.run(
            ["bash", SCRIPT, *args], cwd=ROOT, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def test_dry_run_displays_both_runner_interfaces_and_scheduler_label(self):
        result = self.run_suite(
            "--dry-run", "--baseline-duration", "0.25",
            "--competition-workers", "4", "--competition-duration", "1.5",
            "--repetitions", "2",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("default Linux scheduler", result.stdout)
        self.assertIn("DRY RUN baseline 2:", result.stdout)
        self.assertIn("run_cpu_baseline.sh 0.25", result.stdout)
        self.assertIn("run_cpu_competition.sh 4 1.5", result.stdout)
        self.assertEqual(result.stdout.count("DRY RUN baseline"), 2)
        self.assertEqual(result.stdout.count("DRY RUN competition"), 2)

    def test_invalid_values_fail_before_running(self):
        result = self.run_suite("--dry-run", "--competition-workers", "33")
        self.assertEqual(result.returncode, 2)
        self.assertIn("1 to 32", result.stderr)


if __name__ == "__main__":
    unittest.main()
