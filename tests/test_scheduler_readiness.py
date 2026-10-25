"""Readiness diagnostic tests use temporary paths and mocked command output."""

import importlib.util
import os
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

    def commands(self, include_dir, *, missing=(), libbpf_version="1.5.0",
                 clang_bpf=True, link_success=True):
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
                if "-o" in args:
                    return result(0 if link_success else 1, "",
                                  "cannot find -lelf" if not link_success else "")
                return result(stdout="cc synthetic version\n")
            if name == "clang":
                targets = "    bpf - BPF\n" if clang_bpf else "    x86 - 32-bit X86\n"
                return result(stdout="Registered Targets:\n" + targets)
            if name == "bpftool":
                return result(stdout="bpftool v7.0\n")
            if name == "pkg-config":
                if args == ["--modversion", "libbpf"]:
                    return result(stdout=libbpf_version + "\n")
                if args == ["--cflags", "libbpf"]:
                    return result(stdout=f"-I{include_dir}\n")
                if args == ["--libs", "libbpf"]:
                    return result(stdout="-lbpf\n")
                if args == ["--atleast-version=1.5", "libbpf"]:
                    parsed = tuple(int(part) for part in libbpf_version.split(".")[:2])
                    return result(0 if parsed >= (1, 5) else 1)
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
            plan = next(item for item in entries if item["name"] == "Makefile build plan")
            self.assertIn("-target bpf", plan["detail"])
            self.assertIn("bpftool gen skeleton", plan["detail"])
            self.assertIn("src/group_scheduler.c", plan["detail"])
            self.assertIn("-lelf -lz -lzstd -lpthread", plan["detail"])
            self.assertIn(str(scx / "scheds/include/scx/common.bpf.h"), plan["detail"])
            self.assertIn("clang -g -O2 -target bpf -mcpu=v3 -D__TARGET_ARCH_x86", plan["detail"])
            self.assertIn("bpftool gen skeleton build/group_scheduler.bpf.o name group_scheduler", plan["detail"])
            self.assertIn("-isystem", plan["detail"])

    def test_workloads_target_keeps_binaries_as_separate_prerequisites(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
        workload_targets = [line for line in makefile if line.startswith("workloads:")]
        self.assertEqual(workload_targets,
                         ["workloads: $(BUILD_DIR)/cpu_worker $(BUILD_DIR)/cpu_supervisor"])

    def test_workloads_dry_run_does_not_query_scheduler_only_libbpf_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, PKG_CONFIG="missing-pkg-config-for-test")
            result = subprocess.run(
                ["make", "--no-print-directory", "-n", f"BUILD_DIR={temp}/build", "workloads"],
                cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("cpu_worker.c", result.stdout)
            self.assertIn("cpu_supervisor.c", result.stdout)
            self.assertNotIn("missing-pkg-config-for-test", result.stderr)
            self.assertNotIn("Package libbpf was not found", result.stderr)

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

    def test_build_only_preflight_does_not_treat_enabled_scheduler_as_build_blocker(self):
        for state_value in ("enabled", None):
            with self.subTest(state=state_value), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                sys_root, proc_root, scx, include_dir = self.fixture(root, state=state_value)
                which, run = self.commands(include_dir)
                entries, ready = readiness.diagnose(sys_root=sys_root, proc_root=proc_root,
                                                    scx_root=scx, which=which, run=run,
                                                    build_only=True)
                self.assertTrue(ready)
                state = next(item for item in entries if item["name"] == "sched_ext state interface")
                self.assertFalse(state["ok"])

    def test_old_libbpf_metadata_and_clang_without_bpf_backend_are_actionable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sys_root, proc_root, scx, include_dir = self.fixture(root)
            which, run = self.commands(include_dir, libbpf_version="1.4.0", clang_bpf=False)
            entries, ready = readiness.diagnose(sys_root=sys_root, proc_root=proc_root,
                                                scx_root=scx, which=which, run=run)
            self.assertFalse(ready)
            clang = next(item for item in entries if item["name"] == "Clang BPF backend")
            libbpf = next(item for item in entries if item["name"] == "libbpf pkg-config metadata")
            self.assertIn("does not list a BPF target", clang["detail"])
            self.assertTrue(clang["action"])
            self.assertIn("1.4.0", libbpf["detail"])
            self.assertTrue(libbpf["action"])

    def test_userspace_link_failure_reports_missing_library_action(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sys_root, proc_root, scx, include_dir = self.fixture(root)
            which, run = self.commands(include_dir, link_success=False)
            entries, ready = readiness.diagnose(sys_root=sys_root, proc_root=proc_root,
                                                scx_root=scx, which=which, run=run)
            self.assertFalse(ready)
            link = next(item for item in entries if item["name"] == "Userspace linker dependencies")
            self.assertIn("cannot find -lelf", link["detail"])
            self.assertIn("-lelf -lz -lzstd -lpthread", link["action"])


if __name__ == "__main__":
    unittest.main()
