"""Root-free tests for the demo's temporary-cgroup cleanup policy."""

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "scripts" / "run_custom_scheduler_demo.sh"


def cleanup_function():
    source = DEMO.read_text(encoding="utf-8")
    start = source.index("cleanup() {")
    end = source.index("\n}\ntrap cleanup EXIT", start) + len("\n}")
    return source[start:end]


def run_cleanup(temp, created, candidate_a, candidate_b):
    capture = Path(temp) / "capture"
    capture.write_text("", encoding="ascii")
    mock_bin = Path(temp) / "mock-bin"
    mock_bin.mkdir()
    mock_rmdir = mock_bin / "rmdir"
    mock_rmdir.write_text(
        "#!/bin/sh\n"
        "for path do rm -f -- \"$path/cgroup.procs\"; done\n"
        "exec /bin/rmdir \"$@\"\n",
        encoding="ascii")
    mock_rmdir.chmod(0o755)
    command = "\n".join((
        cleanup_function(),
        f"group_a={shlex.quote(str(candidate_a))}",
        f"group_b={shlex.quote(str(candidate_b))}",
        "created=(" + " ".join(shlex.quote(str(path)) for path in created) + ")",
        f"capture={shlex.quote(str(capture))}",
        "cleanup",
    ))
    env = os.environ.copy()
    env.pop("SUDO_UID", None)
    env.pop("SUDO_GID", None)
    env["PATH"] = str(mock_bin) + os.pathsep + env["PATH"]
    return subprocess.run(["bash", "-c", command], env=env,
                          capture_output=True, text=True, check=False)


class CustomSchedulerDemoCleanupTests(unittest.TestCase):
    def test_help_exposes_private_libbpf_and_worker_timeout_controls(self):
        result = subprocess.run(["bash", str(DEMO), "--help"], capture_output=True,
                                text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--libbpf-libdir DIR", result.stdout)
        self.assertIn("--worker-timeout SEC", result.stdout)

    def make_group(self, path, *, members="", blocker=False):
        path.mkdir()
        (path / "cgroup.procs").write_text(members, encoding="ascii")
        if blocker:
            (path / "unrelated-entry").write_text("keep", encoding="ascii")
        return path

    def test_cleanup_removes_only_groups_recorded_as_created(self):
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            created = self.make_group(root / "created")
            preexisting = self.make_group(root / "preexisting")

            result = run_cleanup(root, [created], created, preexisting)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(created.exists())
            self.assertTrue(preexisting.exists())

    def test_nonempty_created_group_is_left_and_cleanup_fails(self):
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            populated = self.make_group(root / "populated", members="98765\n")

            result = run_cleanup(root, [populated], populated, root / "unused")

            self.assertEqual(result.returncode, 1)
            self.assertTrue(populated.exists())
            self.assertIn("leaving non-empty cgroup untouched", result.stderr)

    def test_rmdir_failure_is_reported_without_forcing_removal(self):
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            blocked = self.make_group(root / "blocked", blocker=True)

            result = run_cleanup(root, [blocked], blocked, root / "unused")

            self.assertEqual(result.returncode, 1)
            self.assertTrue(blocked.exists())
            self.assertIn("could not remove temporary cgroup", result.stderr)


if __name__ == "__main__":
    unittest.main()