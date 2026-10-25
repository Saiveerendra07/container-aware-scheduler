"""Readiness diagnostic tests use temporary paths and mocked command output."""

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_scheduler_readiness.py"
spec = importlib.util.spec_from_file_location("scheduler_readiness", SCRIPT)
readiness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readiness)


def result(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class SchedulerReadinessTests(unittest.TestCase):
    def fixture(self, root, *, state="disabled", wsl=False):
        sys_root = root / "sys"
        proc_root = root / "proc"
        (sys_root / "kernel/btf").mkdir(parents=True)
        (sys_root / "kernel/btf/vmlinux").write_bytes(b"synthetic-btf")
        (sys_root / "kernel/sched_ext").mkdir()
        if state is not None:
            (sys_root / "kernel/sched_ext/state").write_text(state)
        (proc_root / "sys/kernel").mkdir(parents=True)
        (proc_root / "sys/kernel/osrelease").write_text("6.12.0-test" + ("-microsoft-standard-WSL2" if wsl else ""))
        (proc_root / "version").write_text("Linux version 6.12.0-test")
        scx = root / "scx"
        include = scx / "scheds/include"
        (include / "scx").mkdir(parents=True)
        (include / "scx/common.bpf.h").touch()
        (include / "scx/common.h").touch()
        (include / "bpf-compat").mkdir()
        (include / "lib").mkdir()
        include_dir = root / "includes"
        (include_dir / "bpf").mkdir(parents=True)
        (include_dir / "bpf/libbpf.h").touch()
        return sys_root, proc_root, scx, include_dir

    def commands(self, include_dir, *, missing=(), state_override=None):
        available = {"uname", "cc", "clang", "bpftool", "pkg-config"} - set(missing)

        def which(name):
            return f"/mock/{name}" if name in available else None

        def run(command, **_kwargs):
            name, args = Path(command[0]).name, command[1:]
            if name == "uname" and args == ["-sr"]:
                return result(stdout="Linux 6.12.0-test\n")
            if name == "uname" and args == ["-m"]:
                return result(stdout="x86_64\n")
            if name == "cc":
                return result(stdout="cc synthetic version\n")
            if name == "clang":
                return result(stdout="Registered Targets:\n    bpf - BPF\n")
            if name == "bpftool":
                return result(stdout="bpftool v7.0\n")
            if name == "pkg-config":
                if args == ["--modversion", "libbpf"]:
                    return result(stdout="1.5.0\n")
                if args == ["--cflags", "libbpf"]:
                    return result(stdout=f"-I{include_dir}\n")
                if args == ["--atleast-version=1.5", "libbpf"]:
                    return result()
            return result(2, stderr="unexpected mocked command")

        return which, run

    def test_complete_prerequisites_report_disabled_state_and_wsl_heuristic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sys_root, proc_root, scx, include_dir = self.fixture(root, wsl=True)
            which, run = self.commands(include_dir)
            entries, ready = readiness.diagnose(sys_root=sys_root, proc_root=proc_root,
                                                scx_root=scx, which=which, run=run)
            self.assertTrue(ready)
            state = next(item for item in entries if item["name"] == "sched_ext state interface")
            self.assertTrue(state["ok"])
            self.assertIn("disabled", state["detail"])
            host = next(item for item in entries if item["name"] == "Host environment heuristic")
            self.assertIn("WSL", host["detail"])
            self.assertIn("not definitive", host["detail"])

    def test_missing_prerequisites_are_all_reported_with_next_actions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sys_root, proc_root, scx, include_dir = self.fixture(root, state=None)
            (sys_root / "kernel/btf/vmlinux").unlink()
            (scx / "scheds/include/scx/common.bpf.h").unlink()
            (include_dir / "bpf/libbpf.h").unlink()
            which, run = self.commands(include_dir, missing={"clang", "bpftool", "pkg-config"})
            entries, ready = readiness.diagnose(sys_root=sys_root, proc_root=proc_root,
                                                scx_root=scx, which=which, run=run)
            self.assertFalse(ready)
            failed = [item for item in entries if not item["ok"]]
            self.assertGreaterEqual(len(failed), 7)
            self.assertTrue(all(item["action"] for item in failed))
            state = next(item for item in failed if item["name"] == "sched_ext state interface")
            self.assertIn("unavailable or unreadable", state["detail"])
            self.assertIn("unknown", state["detail"])

    def test_enabled_state_is_distinguished_from_unavailable_interface(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sys_root, proc_root, scx, include_dir = self.fixture(root, state="enabled")
            which, run = self.commands(include_dir)
            entries, ready = readiness.diagnose(sys_root=sys_root, proc_root=proc_root,
                                                scx_root=scx, which=which, run=run)
            state = next(item for item in entries if item["name"] == "sched_ext state interface")
            self.assertFalse(ready)
            self.assertFalse(state["ok"])
            self.assertIn("readable but scheduler state is enabled", state["detail"])


if __name__ == "__main__":
    unittest.main()
