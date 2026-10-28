"""Test-only coverage for custom-scheduler result preparation/evaluation."""

import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
EVALUATE = ROOT / "scripts" / "evaluate_fairness.py"
PREPARER = ROOT / "scripts" / "prepare_custom_scheduler_experiment.py"
RUNNER = ROOT / "scripts" / "run_custom_scheduler_experiment.py"


def load_evaluator():
    spec = importlib.util.spec_from_file_location("fairness_evaluator", EVALUATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_preparer():
    spec = importlib.util.spec_from_file_location("custom_experiment_preparer", PREPARER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_runner():
    spec = importlib.util.spec_from_file_location("custom_scheduler_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CustomSchedulerExperimentTests(unittest.TestCase):
    def test_preparation_records_shares_and_does_not_claim_measurements(self):
        preparer = load_preparer()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            groups = []
            for name, share, cgroup_id in (("alpha", 100, 123), ("beta", 300, 456)):
                path = root / name
                path.mkdir()
                (path / "cpu.weight").write_text(str(share), encoding="ascii")
                (path / "cpu.max").write_text("max 100000", encoding="ascii")
                groups.append((name, share, cgroup_id, path))
            parsed_groups = [preparer.parse_group(f"{name}:{cgroup_id}:{share}:{path}")
                             for name, share, cgroup_id, path in groups]
            actual_ids = {group["path"]: group["cgroup_id"] for group in parsed_groups}

            def synthetic_identity(path):
                return {"cgroup_id": actual_ids[path], "mount_id": 42,
                        "filesystem": "cgroup2",
                        "method": "synthetic_test_only name_to_handle_at"}

            with mock.patch.object(preparer, "resolve_cgroup_identity",
                                   side_effect=synthetic_identity):
                csv_path, metadata_path = preparer.prepare(
                    parsed_groups, root / "out", 2.5)
            with csv_path.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([row["group_share"] for row in rows], ["100", "300"])
            self.assertEqual([row["cgroup_cpu_weight"] for row in rows], ["100", "300"])
            self.assertTrue(all(row["cgroup_cpu_max"] == "max 100000" for row in rows))
            self.assertTrue(all(row["measurement_status"] == "not_run_prepared" for row in rows))
            self.assertTrue(all(row["cpu_time_s"] == "NA" for row in rows))
            self.assertTrue(all(row["measurement_error"] == "" for row in rows))
            self.assertTrue(all(row["verified_cgroup_id"] == row["cgroup_id"] for row in rows))
            self.assertTrue(all(row["identity_verification_status"] == "verified" for row in rows))
            self.assertTrue(metadata_path.exists())
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertIn("run_custom_scheduler_experiment.py run",
                          metadata["runtime_blocker"])
            self.assertTrue(all(group["identity_verification_status"] == "verified"
                                for group in metadata["scheduler_groups"]))

            evaluator = load_evaluator()
            runs, warnings = evaluator.load_inputs([str(csv_path)])
            self.assertEqual(warnings, [])
            evaluated = next(iter(runs.values()))
            output = evaluator.evaluate_run(evaluated)
            self.assertEqual(len(output), 2)
            self.assertTrue(all(row["observed_cpu_pct"] == "NA" for row in output))

    def test_name_to_handle_id_is_accepted_only_from_cgroup2_mount(self):
        preparer = load_preparer()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            mountinfo = Path(temp) / "mountinfo"
            mountinfo.write_text(
                "42 1 0:99 / /synthetic rw - cgroup2 cgroup rw\n",
                encoding="utf-8")
            identity = preparer.resolve_cgroup_identity(
                "/synthetic/group", handle_reader=lambda _path: (987654, 42),
                mountinfo_path=mountinfo)
            self.assertEqual(identity["cgroup_id"], 987654)
            self.assertEqual(identity["method"], "name_to_handle_at(FILEID_KERNFS)")
            mountinfo.write_text(
                "42 1 0:99 / /synthetic rw - tmpfs tmpfs rw\n", encoding="utf-8")
            with self.assertRaises(preparer.IdentityVerificationError):
                preparer.resolve_cgroup_identity(
                    "/synthetic/group", handle_reader=lambda _path: (987654, 42),
                    mountinfo_path=mountinfo)

    def test_kernfs_handle_decodes_full_id_instead_of_stat_inode(self):
        preparer = load_preparer()
        expected_id = 0x123456789ABC

        def fake_name_to_handle_at(_dirfd, _name, handle_ptr, mount_id_ptr, _flags):
            handle = handle_ptr._obj
            handle.handle_type = preparer.FILEID_KERNFS
            handle.handle_bytes = preparer.KERNFS_HANDLE_BYTES
            handle.f_handle[:] = expected_id.to_bytes(
                preparer.KERNFS_HANDLE_BYTES, sys.byteorder)
            mount_id_ptr._obj.value = 77
            return 0

        libc = SimpleNamespace(name_to_handle_at=fake_name_to_handle_at)
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            with mock.patch.object(preparer.ctypes, "CDLL", return_value=libc):
                cgroup_id, mount_id = preparer.read_kernfs_handle(temp)
        self.assertEqual(cgroup_id, expected_id)
        self.assertEqual(mount_id, 77)

    def test_unavailable_name_to_handle_at_fails_closed(self):
        preparer = load_preparer()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            with mock.patch.object(preparer.ctypes, "CDLL",
                                   return_value=SimpleNamespace()):
                with self.assertRaisesRegex(preparer.IdentityVerificationError,
                                            "does not expose"):
                    preparer.read_kernfs_handle(temp)

    def test_mismatched_or_unavailable_identity_fails_without_output(self):
        preparer = load_preparer()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            groups = []
            for name, cgroup_id in (("alpha", 10), ("beta", 20)):
                path = root / name
                path.mkdir()
                groups.append(preparer.parse_group(f"{name}:{cgroup_id}:100:{path}"))
            out = root / "out"
            with mock.patch.object(preparer, "resolve_cgroup_identity",
                                   side_effect=lambda path: {
                                       "cgroup_id": 999 if "alpha" in path else 20,
                                       "mount_id": 42, "filesystem": "cgroup2",
                                       "method": "synthetic_test_only"}):
                with self.assertRaisesRegex(preparer.IdentityVerificationError,
                                            "does not match"):
                    preparer.prepare(groups, out, 1)
            with mock.patch.object(preparer, "resolve_cgroup_identity",
                                   side_effect=preparer.IdentityVerificationError(
                                       "interface unavailable")):
                with self.assertRaises(preparer.IdentityVerificationError):
                    preparer.prepare(groups, out, 1)
            self.assertFalse(out.exists())

    def test_duplicate_ids_paths_invalid_shares_and_invalid_paths_are_rejected(self):
        preparer = load_preparer()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            one = root / "one"
            two = root / "two"
            one.mkdir()
            two.mkdir()
            first = preparer.parse_group(f"alpha:10:100:{one}")
            same_id = preparer.parse_group(f"beta:10:200:{two}")
            same_path = preparer.parse_group(f"beta:20:200:{one}")
            with self.assertRaisesRegex(ValueError, "IDs must be unique"):
                preparer.prepare([first, same_id], root / "out", 1)
            with self.assertRaisesRegex(ValueError, "paths must be unique"):
                preparer.prepare([first, same_path], root / "out", 1)
            bad_share = dict(same_id, cgroup_id=11, share=0)
            with self.assertRaisesRegex(ValueError, "shares are capped"):
                preparer.prepare([first, bad_share], root / "out", 1)
            with self.assertRaises(preparer.argparse.ArgumentTypeError):
                preparer.parse_group(f"lost:30:100:{root / 'missing'}")
            for bad in (f"lost:30:0:{one}", f"lost:30:10001:{one}",
                        f"lost:{1 << 64}:100:{one}"):
                with self.assertRaises(preparer.argparse.ArgumentTypeError):
                    preparer.parse_group(bad)

    def test_preparation_enforces_loader_group_limit(self):
        preparer = load_preparer()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            groups = []
            for index in range(9):
                path = root / f"group{index}"
                path.mkdir()
                groups.append({"name": f"g{index}", "cgroup_id": index + 1,
                               "share": 1, "path": str(path)})
            with self.assertRaisesRegex(ValueError, "2 to 8"):
                preparer.prepare(groups, root / "out", 1)

    def test_runner_rejects_non_integer_and_out_of_range_config_values(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            groups = []
            for name, cgroup_id in (("alpha", 101), ("beta", 202)):
                path = root / name
                path.mkdir()
                groups.append({"name": name, "cgroup_id": cgroup_id,
                               "share": 100, "path": str(path)})
            for invalid_groups, fallback in (
                ([dict(groups[0], share=True), groups[1]], 50),
                ([dict(groups[0], cgroup_id=1 << 64), groups[1]], 50),
                (groups, True),
            ):
                with self.assertRaises(runner.ExperimentError):
                    runner.validate_groups(invalid_groups, fallback)

    def test_synthetic_completed_harness_schema_is_accepted_end_to_end(self):
        with tempfile.TemporaryDirectory(prefix="synthetic_test_only_") as temp:
            root = Path(temp)
            input_path = root / "synthetic_test_only_custom.csv"
            with input_path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=[
                    "run_id", "group", "group_share", "cpu_time_s",
                    "measurement_status", "exit_status", "completion_status",
                ])
                writer.writeheader()
                for group, share, cpu in (("alpha", 1, 1), ("beta", 3, 3)):
                    writer.writerow({"run_id": "synthetic_test_only_run", "group": group,
                                     "group_share": share, "cpu_time_s": cpu,
                                     "measurement_status": "complete", "exit_status": 0,
                                     "completion_status": "complete"})
            output_path = root / "synthetic_test_only_analysis.csv"
            subprocess.run([sys.executable, str(EVALUATE), str(input_path),
                            "--output", str(output_path)], check=True,
                           text=True, capture_output=True)
            with output_path.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)
            self.assertEqual({float(row["expected_cpu_pct"]) for row in rows}, {25.0, 75.0})
            self.assertEqual({float(row["allocation_error_pct_points"]) for row in rows}, {0.0})
            self.assertEqual({float(row["jain_fairness_index"]) for row in rows}, {0.8})


if __name__ == "__main__":
    unittest.main()
