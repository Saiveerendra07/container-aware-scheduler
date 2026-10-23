import os
import subprocess
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "run_cgroup_competition.sh")


class CgroupCompetitionTests(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run(
            ["bash", SCRIPT, *args], cwd=ROOT, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def test_rejects_incomplete_or_invalid_cpu_controls_before_setup(self):
        cases = [
            ("--parent", "/tmp", "--weight-a", "0", "--weight-b", "100"),
            ("--parent", "/tmp", "--weight-a", "100", "--quota-b", "50000/100000"),
            ("--parent", "/tmp", "--quota-a", "500/999", "--quota-b", "50000/100000"),
            ("--parent", "/tmp", "--weight-a", "100", "--weight-b", "100", "--duration", "0"),
        ]
        for args in cases:
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("Error:", result.stderr)

    def test_dry_run_checks_two_group_setup_without_writing(self):
        with tempfile.TemporaryDirectory() as parent:
            for name, content in (("cgroup.controllers", "cpu cpuset\n"),
                                  ("cgroup.subtree_control", "cpu\n"),
                                  ("cgroup.procs", "")):
                with open(os.path.join(parent, name), "w", encoding="utf-8") as stream:
                    stream.write(content)
            result = self.run_script(
                "--dry-run", "--parent", parent, "--weight-a", "200",
                "--weight-b", "100", "--duration", "0.5",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("cpu.weight A=200 B=100", result.stdout)
            self.assertIn("no writes or scheduler attach", result.stdout)
            self.assertEqual(sorted(os.listdir(parent)),
                             ["cgroup.controllers", "cgroup.procs", "cgroup.subtree_control"])


if __name__ == "__main__":
    unittest.main()
