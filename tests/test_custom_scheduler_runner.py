"""Synthetic test-only coverage for runner orchestration, never kernel validation."""

import csv
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts" / "run_custom_scheduler_experiment.py"
EVALUATOR_PATH = ROOT / "scripts" / "evaluate_fairness.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("custom_scheduler_runner", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def executable(path, source):
    path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    path.chmod(0o755)
    return path


class CustomSchedulerRunnerTests(unittest.TestCase):
    def read_rows(self, path):
        with path.open(newline="", encoding="utf-8") as stream:
            return list(csv.DictReader(stream))

    def setup_groups(self, root, runner):
        groups = []
        for name, cgroup_id, share in (("alpha", 101, 100), ("beta", 202, 300)):
            path = root / name
            path.mkdir()
            (path / "cgroup.procs").write_text("", encoding="ascii")
            (path / "cpu.weight").write_text("100", encoding="ascii")
            groups.append({"name": name, "cgroup_id": cgroup_id, "share": share,
                           "path": str(path)})
        return [dict(group, verified_cgroup_id=group["cgroup_id"],
                     identity_verification_status="verified",
                     identity_verification_method="synthetic_test_only") for group in groups]

    def test_group_validation_rejects_duplicate_identity_and_invalid_shares(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            groups = self.setup_groups(Path(temp), runner)
            with self.assertRaisesRegex(runner.ExperimentError, "IDs must be unique"):
                runner.validate_groups([groups[0], dict(groups[1], cgroup_id=101)], 100)
            with self.assertRaisesRegex(runner.ExperimentError, "fallback share"):
                runner.validate_groups(groups, 0)

    def test_prepare_writes_unique_manifest_and_na_rows_without_launching(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            groups = self.setup_groups(root, runner)
            args = SimpleNamespace(output_dir=root / "out", fallback_share=50, duration=1.5,
                                   worker=Path("build/cpu_worker"))
            runner.prepare_experiment(args, groups)
            runner.prepare_experiment(args, groups)
            manifests = list((root / "out").glob("*.json"))
            csvs = list((root / "out").glob("*.csv"))
            self.assertEqual(len(manifests), 2)
            self.assertEqual(len(csvs), 2)
            metadata = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertFalse(metadata["scheduler_attached"])
            self.assertEqual([g["configured_scheduler_share"] for g in metadata["scheduler_groups"]],
                             [100, 300])
            with csvs[0].open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["cpu_time_s"] == "NA" for row in rows))
            self.assertTrue(all(row["measurement_status"] == "not_run_prepared" for row in rows))

    def test_check_validates_only_and_does_not_start_any_process(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            groups = self.setup_groups(root, runner)
            loader = executable(root / "loader", "raise SystemExit(0)\n")
            worker = executable(root / "worker", "raise SystemExit(0)\n")
            supervisor = executable(root / "supervisor", "raise SystemExit(0)\n")
            obj = root / "scheduler.o"
            obj.write_bytes(b"\x7fELF\x02\x01" + b"\x00" * 12 + (247).to_bytes(2, "little"))
            btf = root / "btf"; btf.write_bytes(b"synthetic_test_only")
            state = root / "state"; state.write_text("disabled\n", encoding="ascii")
            args = SimpleNamespace(group=groups, fallback_share=50, loader=loader,
                                   bpf_object=obj, worker=worker, supervisor=supervisor,
                                   btf=btf, sched_ext_state=state)
            checked = runner.check_prerequisites(args,
                identity_verifier=lambda group: group, capability_check=lambda: True)
            self.assertEqual(len(checked), 2)

    def test_failed_loader_startup_records_incomplete_na_results(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            groups = self.setup_groups(root, runner)
            loader = executable(root / "loader", "import sys\nsys.exit(9)\n")
            args = SimpleNamespace(output_dir=root / "out", fallback_share=50, duration=0.1,
                                   loader=loader, worker=root / "worker", attach_timeout=1)
            self.assertEqual(runner.run_experiment(args, groups), 1)
            csv_path = next((root / "out").glob("*.csv"))
            with csv_path.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertTrue(all(row["measurement_status"] == "incomplete" for row in rows))
            self.assertTrue(all(row["cpu_time_s"] == "NA" for row in rows))
            self.assertTrue(all(row["scheduler_attached"] == "false" for row in rows))
            manifest = json.loads(next((root / "out").glob("*.json")).read_text())
            self.assertIn("exited before attachment readiness", manifest["error"])

    def test_partial_readiness_output_cannot_bypass_attach_timeout(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            groups = self.setup_groups(root, runner)
            loader = executable(root / "loader", """
import signal, sys, time
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
sys.stdout.write('GROUP_SCHEDULER_')
sys.stdout.flush()
while True: time.sleep(.05)
""")
            args = SimpleNamespace(output_dir=root / "out", fallback_share=50, duration=0.1,
                                   loader=loader, worker=root / "worker", attach_timeout=0.2)
            started = time.monotonic()
            self.assertEqual(runner.run_experiment(args, groups), 1)
            self.assertLess(time.monotonic() - started, 2.0)
            csv_path = next((root / "out").glob("*.csv"))
            with csv_path.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertTrue(all(row["scheduler_attached"] == "false" for row in rows))
            self.assertTrue(all(row["cpu_time_s"] == "NA" for row in rows))
            manifest = json.loads(next((root / "out").glob("*.json")).read_text())
            self.assertIn("timed out", manifest["error"])

    def test_launch_failure_after_attach_stops_and_reaps_started_supervisors(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            groups = self.setup_groups(root, runner)
            loader = executable(root / "loader", """
import signal, time
signal.signal(signal.SIGTERM, lambda *_: exit(0))
print('GROUP_SCHEDULER_ATTACHED', flush=True)
while True: time.sleep(.02)
""")
            supervisor = executable(root / "supervisor", """
import signal, time
signal.signal(signal.SIGTERM, lambda *_: exit(0))
while True: time.sleep(.02)
""")
            launched = []
            supervisor_launches = []
            real_popen = subprocess.Popen

            def fail_second_supervisor(command, **kwargs):
                if command[0] == str(supervisor):
                    if supervisor_launches:
                        raise OSError("synthetic launch failure")
                    supervisor_launches.append(True)
                child = real_popen(command, **kwargs)
                launched.append(child)
                return child

            args = SimpleNamespace(output_dir=root / "out", fallback_share=50, duration=0.1,
                                   loader=loader, worker=root / "worker", supervisor=supervisor,
                                   attach_timeout=2)
            self.assertEqual(runner.run_experiment(args, groups, popen=fail_second_supervisor), 1)
            self.assertEqual(len(launched), 2)  # loader plus first supervisor
            self.assertTrue(all(child.poll() is not None for child in launched))
            rows = self.read_rows(next((root / "out").glob("*.csv")))
            self.assertTrue(all(row["measurement_status"] == "incomplete" for row in rows))
            self.assertTrue(all(row["cpu_time_s"] == "NA" for row in rows))
            self.assertTrue(all(row["scheduler_attached"] == "true" for row in rows))

    def test_sigterm_during_run_records_incomplete_and_reaps_processes(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            groups = self.setup_groups(root, runner)
            loader = executable(root / "loader", """
import signal, time
signal.signal(signal.SIGTERM, lambda *_: exit(0))
print('GROUP_SCHEDULER_ATTACHED', flush=True)
while True: time.sleep(.02)
""")
            supervisor = executable(root / "supervisor", """
import pathlib, signal, sys, time
metadata = pathlib.Path(sys.argv[3])
def stop(*_):
    metadata.write_text('123\\tNA\\tNA\\tNA\\tNA\\t15\\t0\\t0\\n')
    sys.exit(143)
signal.signal(signal.SIGTERM, stop)
while True: time.sleep(.02)
""")
            launched = []
            real_popen = subprocess.Popen
            def capture(command, **kwargs):
                child = real_popen(command, **kwargs)
                launched.append(child)
                return child
            real_sleep = time.sleep
            sent = [False]
            def interrupt_once(seconds):
                real_sleep(min(seconds, .02))
                if not sent[0]:
                    sent[0] = True
                    os.kill(os.getpid(), signal.SIGTERM)
            args = SimpleNamespace(output_dir=root / "out", fallback_share=50, duration=5,
                                   loader=loader, worker=root / "worker", supervisor=supervisor,
                                   attach_timeout=2)
            with mock.patch.object(runner.time, "sleep", side_effect=interrupt_once):
                self.assertEqual(runner.run_experiment(args, groups, popen=capture), 1)
            self.assertTrue(all(child.poll() is not None for child in launched))
            rows = self.read_rows(next((root / "out").glob("*.csv")))
            self.assertTrue(all(row["measurement_status"] == "incomplete" for row in rows))
            self.assertTrue(all(row["cpu_time_s"] == "NA" for row in rows))
            manifest = json.loads(next((root / "out").glob("*.json")).read_text())
            self.assertIn("signal", manifest["error"])

    def test_sigint_during_run_records_incomplete_and_reaps_processes(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            groups = self.setup_groups(root, runner)
            loader = executable(root / "loader", """
import signal, time
signal.signal(signal.SIGTERM, lambda *_: exit(0))
print('GROUP_SCHEDULER_ATTACHED', flush=True)
while True: time.sleep(.02)
""")
            supervisor = executable(root / "supervisor", """
import pathlib, signal, sys, time
metadata = pathlib.Path(sys.argv[3])
def stop(*_):
    metadata.write_text('123\\tNA\\tNA\\tNA\\tNA\\t2\\t0\\t0\\n')
    sys.exit(130)
signal.signal(signal.SIGTERM, stop)
while True: time.sleep(.02)
""")
            launched = []
            real_popen = subprocess.Popen
            def capture(command, **kwargs):
                child = real_popen(command, **kwargs)
                launched.append(child)
                return child
            real_sleep = time.sleep
            sent = [False]
            def interrupt_once(seconds):
                real_sleep(min(seconds, .02))
                if not sent[0]:
                    sent[0] = True
                    os.kill(os.getpid(), signal.SIGINT)
            args = SimpleNamespace(output_dir=root / "out", fallback_share=50, duration=5,
                                   loader=loader, worker=root / "worker", supervisor=supervisor,
                                   attach_timeout=2)
            with mock.patch.object(runner.time, "sleep", side_effect=interrupt_once):
                self.assertEqual(runner.run_experiment(args, groups, popen=capture), 1)
            self.assertTrue(all(child.poll() is not None for child in launched))
            rows = self.read_rows(next((root / "out").glob("*.csv")))
            self.assertTrue(all(row["measurement_status"] == "incomplete" for row in rows))
            self.assertTrue(all(row["cpu_time_s"] == "NA" for row in rows))
            manifest = json.loads(next((root / "out").glob("*.json")).read_text())
            self.assertIn("signal", manifest["error"])

    def test_loader_nonzero_shutdown_is_reported_and_invalidates_measurements(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            groups = self.setup_groups(root, runner)
            loader = executable(root / "loader", """
import signal, time
signal.signal(signal.SIGTERM, lambda *_: exit(7))
print('GROUP_SCHEDULER_ATTACHED', flush=True)
while True: time.sleep(.02)
""")
            supervisor = executable(root / "supervisor", """
import pathlib, sys
pathlib.Path(sys.argv[3]).write_text('123\\t1000000000\\t1000000\\t500000\\t0\\t0\\t0\\t0\\n')
""")
            args = SimpleNamespace(output_dir=root / "out", fallback_share=50, duration=0.1,
                                   loader=loader, worker=root / "worker", supervisor=supervisor,
                                   attach_timeout=2)
            self.assertEqual(runner.run_experiment(args, groups), 1)
            rows = self.read_rows(next((root / "out").glob("*.csv")))
            self.assertTrue(all(row["measurement_status"] == "incomplete" for row in rows))
            self.assertTrue(all(row["cpu_time_s"] == "NA" for row in rows))
            manifest = json.loads(next((root / "out").glob("*.json")).read_text())
            self.assertEqual(manifest["scheduler_shutdown_status"], "exit_7")
            self.assertIn("exit_7", manifest["error"])

    def test_mocked_lifecycle_emits_evaluator_compatible_rows(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            groups = self.setup_groups(root, runner)
            loader = executable(root / "loader", """
import signal, time
signal.signal(signal.SIGTERM, lambda *_: exit(0))
print('GROUP_SCHEDULER_ATTACHED', flush=True)
while True: time.sleep(.05)
""")
            supervisor = executable(root / "supervisor", """
import pathlib, sys
metadata = pathlib.Path(sys.argv[3])
metadata.write_text('123\\t1000000000\\t1000000\\t500000\\t0\\t0\\t0\\t0\\n')
""")
            worker = executable(root / "worker", "pass\n")
            args = SimpleNamespace(output_dir=root / "out", fallback_share=50, duration=1.0,
                                   loader=loader, worker=worker, supervisor=supervisor,
                                   attach_timeout=2)
            self.assertEqual(runner.run_experiment(args, groups), 0)
            csv_path = next((root / "out").glob("*.csv"))
            spec = importlib.util.spec_from_file_location("test_fairness_evaluator", EVALUATOR_PATH)
            evaluator = importlib.util.module_from_spec(spec); spec.loader.exec_module(evaluator)
            runs, warnings = evaluator.load_inputs([str(csv_path)])
            self.assertEqual(warnings, [])
            evaluated = evaluator.evaluate_run(next(iter(runs.values())))
            self.assertEqual(len(evaluated), 2)
            self.assertTrue(all(row["observed_cpu_pct"] != "NA" for row in evaluated))
            rows = self.read_rows(csv_path)
            first_run_id = rows[0]["run_id"]

            # A later loader gets a fresh pipe and fresh output reservations;
            # the prior readiness line and result cannot make it look complete.
            args.loader = executable(root / "second_loader", "raise SystemExit(9)\n")
            self.assertEqual(runner.run_experiment(args, groups), 1)
            csv_paths = list((root / "out").glob("*.csv"))
            manifest_paths = list((root / "out").glob("*.json"))
            self.assertEqual(len(csv_paths), 2)
            self.assertEqual(len(manifest_paths), 2)
            all_rows = [self.read_rows(path) for path in csv_paths]
            self.assertEqual(len({result[0]["run_id"] for result in all_rows}), 2)
            first_rows = next(result for result in all_rows if result[0]["run_id"] == first_run_id)
            second_rows = next(result for result in all_rows if result[0]["run_id"] != first_run_id)
            self.assertTrue(all(row["measurement_status"] == "complete" for row in first_rows))
            self.assertTrue(all(row["measurement_status"] == "incomplete" for row in second_rows))
            self.assertTrue(all(row["cpu_time_s"] == "NA" for row in second_rows))


if __name__ == "__main__":
    unittest.main()
