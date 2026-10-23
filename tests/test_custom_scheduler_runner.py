"""Synthetic test-only coverage for runner orchestration, never kernel validation."""

import csv
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


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


if __name__ == "__main__":
    unittest.main()
