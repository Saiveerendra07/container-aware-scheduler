import csv
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from cgroup_metrics import COUNTERS, counter_delta, parse_cpu_stat, write_csv


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

    def test_cpu_stat_parser_reads_supported_nonnegative_counters(self):
        parsed = parse_cpu_stat(
            "usage_usec 1200\nuser_usec 1000\nsystem_usec 200\n"
            "nr_periods 8\nnr_throttled 2\nthrottled_usec 400\n"
            "future_kernel_counter 99\n"
        )
        self.assertEqual(set(parsed), set(COUNTERS))
        self.assertEqual(parsed["usage_usec"], 1200)
        self.assertEqual(parsed["nr_throttled"], 2)

    def test_missing_malformed_duplicate_and_reset_counters_are_unavailable(self):
        before = parse_cpu_stat("usage_usec 10\nuser_usec nope\nuser_usec 5\nnr_periods -1\n")
        after = parse_cpu_stat("usage_usec 17\n")
        self.assertEqual(counter_delta(before, after, "usage_usec"), 7)
        self.assertIsNone(counter_delta(before, after, "user_usec"))
        self.assertIsNone(counter_delta(before, after, "system_usec"))
        self.assertIsNone(counter_delta({"usage_usec": 20}, after, "usage_usec"))

    def test_csv_marks_missing_counter_snapshots_na(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = os.path.join(directory, "metadata.tsv")
            output = os.path.join(directory, "result.csv")
            with open(metadata, "w", encoding="utf-8") as stream:
                stream.write("run\ttime\tkernel\t2\tx86_64\t1\tcpu.weight A=100 B=100\tA\t/tmp/a\tNA\tNA\tincomplete\tNA\tNA\n")
            write_csv(metadata, output)
            with open(output, newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                row = next(reader)
                self.assertIn("measurement_status", reader.fieldnames)
            self.assertEqual(row["cpu_stat_status"], "unavailable")
            for counter in COUNTERS:
                self.assertEqual(row[f"{counter}_before"], "NA")
                self.assertEqual(row[f"{counter}_after"], "NA")
                self.assertEqual(row[f"{counter}_delta"], "NA")

    def test_csv_records_counter_snapshots_and_only_valid_deltas(self):
        with tempfile.TemporaryDirectory() as directory:
            before = os.path.join(directory, "before.stat")
            after = os.path.join(directory, "after.stat")
            metadata = os.path.join(directory, "metadata.tsv")
            output = os.path.join(directory, "result.csv")
            with open(before, "w", encoding="ascii") as stream:
                stream.write("usage_usec 10\nuser_usec 7\nsystem_usec 3\nnr_periods 2\n")
                stream.write("nr_throttled 1\nthrottled_usec 4\n")
            with open(after, "w", encoding="ascii") as stream:
                stream.write("usage_usec 20\nuser_usec 14\nsystem_usec 6\nnr_periods 5\n")
                stream.write("nr_throttled 2\nthrottled_usec 9\n")
            with open(metadata, "w", encoding="utf-8") as stream:
                stream.write(
                    f"run\ttime\tkernel\t2\tx86_64\t1\tcpu.max A=50000/100000 B=100000/100000"
                    f"\tA\t/tmp/a\t1.00\t0\tcomplete\t{before}\t{after}\n"
                )
            write_csv(metadata, output)
            with open(output, newline="", encoding="utf-8") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["cpu_stat_status"], "complete")
            self.assertEqual(row["measurement_status"], "complete")
            self.assertEqual(row["usage_usec_before"], "10")
            self.assertEqual(row["usage_usec_after"], "20")
            self.assertEqual(row["usage_usec_delta"], "10")
            self.assertEqual(row["nr_throttled_delta"], "1")


if __name__ == "__main__":
    unittest.main()
