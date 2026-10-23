"""Unprivileged integration tests for supervisor cgroup placement mode."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "workloads" / "cpu_supervisor.c"


class CgroupPlacementSupervisorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build_dir = tempfile.TemporaryDirectory(prefix="synthetic_test_only_")
        cls.binary = Path(cls.build_dir.name) / "cpu_supervisor"
        cls.production_binary = Path(cls.build_dir.name) / "cpu_supervisor_production"
        subprocess.run(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-std=c11",
                        str(SOURCE), "-o", str(cls.production_binary)], check=True)
        subprocess.run(["gcc", "-DCPU_SUPERVISOR_TESTING", "-O2", "-Wall", "-Wextra",
                        "-Werror", "-std=c11",
                        str(SOURCE), "-o", str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build_dir.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic_test_only_")
        self.root = Path(self.temp.name)
        self.metadata = self.root / "worker.meta"
        self.metadata.write_text("", encoding="ascii")
        self.cancel = self.root / "cancel"
        self.cancel.write_text("0\n", encoding="ascii")
        self.worker = self.root / "worker.sh"
        self.worker.write_text(
            "#!/bin/sh\ncat \"$CGROUP_PROCS\" > \"$PLACEMENT_OBSERVED\"\nexit 0\n",
            encoding="ascii")
        self.worker.chmod(0o755)
        self.legacy_worker = self.root / "legacy-worker.sh"
        self.legacy_worker.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        self.legacy_worker.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def run_supervisor(self, cgroup, binary=None):
        env = os.environ.copy()
        env["CGROUP_PROCS"] = str(cgroup / "cgroup.procs")
        env["PLACEMENT_OBSERVED"] = str(self.root / "worker-observed-members")
        return subprocess.run(
            [str(binary or self.binary), "--cgroup", str(cgroup), str(self.metadata),
             str(self.worker), "1", str(self.cancel)],
            env=env, text=True, capture_output=True, timeout=5)

    def test_successful_release_occurs_after_verified_placement(self):
        cgroup = self.root / "delegated-group"
        cgroup.mkdir()
        (cgroup / "cgroup.procs").write_text("", encoding="ascii")
        result = self.run_supervisor(cgroup)
        self.assertEqual(result.returncode, 0, result.stderr)
        placed_pid = (cgroup / "cgroup.procs").read_text(encoding="ascii").strip()
        observed_pid = (self.root / "worker-observed-members").read_text(
            encoding="ascii").strip()
        self.assertTrue(placed_pid.isdecimal())
        self.assertIn(placed_pid, observed_pid.split())
        fields = self.metadata.read_text(encoding="ascii").strip().split("\t")
        self.assertEqual(fields[4], "0")
        self.assertGreaterEqual(int(fields[1]), 0)  # elapsed nanoseconds
        self.assertNotEqual(fields[2], "NA")  # user CPU microseconds

    def test_invalid_cgroup_path_fails_before_workload_can_start(self):
        result = self.run_supervisor(self.root / "missing", self.production_binary)
        self.assertEqual(result.returncode, 125)
        self.assertFalse((self.root / "worker-observed-members").exists())
        self.assertIn("cgroup", result.stderr)

    def test_production_mode_rejects_an_ordinary_directory_as_cgroup(self):
        cgroup = self.root / "ordinary-directory"
        cgroup.mkdir()
        (cgroup / "cgroup.procs").write_text("", encoding="ascii")
        result = self.run_supervisor(cgroup, self.production_binary)
        self.assertEqual(result.returncode, 125)
        self.assertFalse((self.root / "worker-observed-members").exists())
        fields = self.metadata.read_text(encoding="ascii").strip().split("\t")
        self.assertNotEqual(fields[-1], "0")

    def test_legacy_invocation_remains_compatible(self):
        result = subprocess.run(
            [str(self.binary), str(self.metadata), str(self.legacy_worker),
             "1", str(self.cancel)],
            text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        fields = self.metadata.read_text(encoding="ascii").strip().split("\t")
        self.assertEqual(fields[4], "0")

    def test_unwritable_interface_shape_fails_closed_and_cleans_up(self):
        cgroup = self.root / "bad-group"
        cgroup.mkdir()
        (cgroup / "cgroup.procs").mkdir()
        result = self.run_supervisor(cgroup)
        self.assertEqual(result.returncode, 125)
        self.assertFalse((self.root / "worker-observed-members").exists())
        self.assertTrue(self.metadata.exists())
        self.assertNotEqual(self.metadata.read_text(encoding="ascii").split("\t")[-1].strip(),
                            "0")


if __name__ == "__main__":
    unittest.main()
