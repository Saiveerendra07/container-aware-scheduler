#!/usr/bin/env python3
"""Prepare, check, or explicitly run a custom sched_ext group experiment."""

import argparse
import csv
import json
import os
import platform
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare_custom_scheduler_experiment as preparation


READY_LINE = "GROUP_SCHEDULER_ATTACHED"
RESULT_FIELDS = [
    "run_id", "timestamp_utc", "group", "cgroup_id", "cgroup_path",
    "kernel_version", "logical_cpu_count", "architecture", "worker_executable",
    "verified_cgroup_id", "identity_verification_status",
    "identity_verification_method", "cgroup_mount_id", "cgroup_filesystem", "group_share",
    "fallback_share", "share_source", "cgroup_cpu_weight", "cgroup_cpu_max",
    "requested_duration_s", "workload_elapsed_s", "cpu_time_s", "exit_status",
    "termination_signal", "completion_status", "measurement_status",
    "measurement_error", "scheduler_attached", "scheduler_shutdown_status",
]


class ExperimentError(RuntimeError):
    pass


def positive_duration(value):
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("duration must be a number") from exc
    if result <= 0 or result != result or result == float("inf"):
        raise argparse.ArgumentTypeError("duration must be finite and positive")
    return result


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("prepare", "check", "run"):
        sub = subparsers.add_parser(mode, help=f"{mode} a custom scheduler experiment")
        sub.add_argument("--group", action="append", required=True, type=preparation.parse_group,
                         metavar="NAME:CGROUP_ID:SHARE:CGROUP_DIRECTORY")
        sub.add_argument("--fallback-share", required=True, type=int)
        sub.add_argument("--duration", required=True, type=positive_duration)
        sub.add_argument("--output-dir", type=Path, default=Path("results/raw"))
        sub.add_argument("--worker", type=Path, default=Path("build/cpu_worker"))
        if mode != "prepare":
            sub.add_argument("--loader", type=Path, default=Path("build/group_scheduler"))
            sub.add_argument("--bpf-object", type=Path, default=Path("build/group_scheduler.bpf.o"))
            sub.add_argument("--supervisor", type=Path, default=Path("build/cpu_supervisor"))
            sub.add_argument("--sched-ext-state", type=Path,
                             default=Path("/sys/kernel/sched_ext/state"))
            sub.add_argument("--btf", type=Path, default=Path("/sys/kernel/btf/vmlinux"))
            sub.add_argument("--attach-timeout", type=positive_duration, default=15.0)
    return parser


def validate_groups(groups, fallback_share):
    if len(groups) < 2 or len(groups) > 8:
        raise ExperimentError("scheduler requires 2 to 8 configured groups")
    if type(fallback_share) is not int or not 1 <= fallback_share <= preparation.MAX_SHARE:
        raise ExperimentError("fallback share must be from 1 to 10000")
    for group in groups:
        if (type(group.get("cgroup_id")) is not int
                or not 0 < group["cgroup_id"] <= preparation.MAX_CGROUP_ID
                or type(group.get("share")) is not int
                or not 0 < group["share"] <= preparation.MAX_SHARE):
            raise ExperimentError("cgroup IDs and shares must be valid positive integers")
    if sum(g["share"] for g in groups) + fallback_share > 9 * preparation.MAX_SHARE:
        raise ExperimentError("combined scheduler shares exceed the loader's supported total")
    names = [g["name"] for g in groups]
    ids = [g["cgroup_id"] for g in groups]
    paths = [str(Path(g["path"]).resolve(strict=True)) for g in groups]
    if len(set(names)) != len(names):
        raise ExperimentError("group names must be unique")
    if len(set(ids)) != len(ids):
        raise ExperimentError("cgroup IDs must be unique")
    if len(set(paths)) != len(paths):
        raise ExperimentError("cgroup paths must be unique")
    return [dict(g, path=path) for g, path in zip(groups, paths)]


def verify_groups(groups, verifier=preparation.verify_group_identity):
    return [verifier(group) for group in groups]


def _check_cgroup_write_permission(path):
    cgroup = Path(path)
    if not cgroup.is_dir() or not (cgroup / "cgroup.procs").is_file():
        raise ExperimentError(f"invalid cgroup-v2 target or missing cgroup.procs: {path}")
    try:
        fd = os.open(cgroup / "cgroup.procs", os.O_WRONLY | os.O_CLOEXEC)
    except OSError as exc:
        raise ExperimentError(f"cgroup.procs is not writable for {path}: {exc}") from exc
    else:
        os.close(fd)


def _validate_bpf_object(path):
    try:
        with open(path, "rb") as stream:
            header = stream.read(20)
    except OSError as exc:
        raise ExperimentError(f"cannot read scheduler BPF object {path}: {exc}") from exc
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise ExperimentError(f"scheduler BPF object is not a valid ELF file: {path}")
    if header[5] == 1:
        endian = "little"
    elif header[5] == 2:
        endian = "big"
    else:
        raise ExperimentError(f"scheduler BPF object has invalid ELF byte order: {path}")
    if int.from_bytes(header[18:20], endian) != 247:  # EM_BPF
        raise ExperimentError(f"scheduler object is not an EM_BPF ELF object: {path}")


def _has_bpf_capability():
    try:
        with open("/proc/self/status", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("CapEff:"):
                    effective = int(line.split()[1], 16)
                    return bool(effective & (1 << 39) or effective & (1 << 21))
    except (OSError, ValueError, IndexError):
        return False
    return False


def check_prerequisites(args, identity_verifier=preparation.verify_group_identity,
                        capability_check=_has_bpf_capability):
    groups = validate_groups(args.group, args.fallback_share)
    verified = verify_groups(groups, identity_verifier)
    for group in verified:
        _check_cgroup_write_permission(group["path"])
    for path, label in ((args.loader, "scheduler loader"), (args.bpf_object, "BPF object"),
                        (args.worker, "CPU worker")):
        if not path.is_file() or (label != "BPF object" and not os.access(path, os.X_OK)):
            raise ExperimentError(f"{label} missing or not executable/readable: {path}")
        if label == "BPF object" and not os.access(path, os.R_OK):
            raise ExperimentError(f"BPF object is not readable: {path}")
    _validate_bpf_object(args.bpf_object)
    if not args.supervisor.is_file() or not os.access(args.supervisor, os.X_OK):
        raise ExperimentError(f"cgroup-aware supervisor missing or not executable: {args.supervisor}")
    if not args.btf.is_file() or not os.access(args.btf, os.R_OK):
        raise ExperimentError(f"kernel BTF is missing or unreadable: {args.btf}")
    try:
        state = args.sched_ext_state.read_text(encoding="ascii").strip().lower()
    except OSError as exc:
        raise ExperimentError(f"sched_ext state interface unavailable: {args.sched_ext_state}: {exc}") from exc
    if state != "disabled":
        raise ExperimentError(f"sched_ext must be disabled before this run (state={state!r})")
    if not capability_check():
        raise ExperimentError("effective CAP_BPF or CAP_SYS_ADMIN is not present; attach permission cannot be established")
    return verified


def _reserve_outputs(output_dir, mode):
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"custom_scheduler_{mode}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:10]}"
    created = []
    try:
        for suffix in (".csv", ".json", ".scheduler.log"):
            fd, name = tempfile.mkstemp(prefix=run_id + "_", suffix=suffix, dir=output_dir)
            os.close(fd)
            created.append(Path(name))
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return run_id, created[0], created[1], created[2]


def _manifest(run_id, mode, groups, fallback_share, duration, csv_path, log_path,
              worker_path):
    return {
        "run_id": run_id, "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode, "scheduler_attached": False,
        "measurement_status": "not_run_prepared" if mode == "prepare" else "pending",
        "requested_duration_s": duration,
        "scheduler_groups": [{
            "name": g["name"], "cgroup_id": g["cgroup_id"],
            "verified_cgroup_id": g["verified_cgroup_id"],
            "identity_verification_status": g["identity_verification_status"],
            "identity_verification_method": g["identity_verification_method"],
            "cgroup_mount_id": g.get("cgroup_mount_id", "NA"),
            "cgroup_filesystem": g.get("cgroup_filesystem", "NA"),
            "cgroup_path": g["path"], "configured_scheduler_share": g["share"],
            "cgroup_cpu_weight": preparation.read_control(g["path"], "cpu.weight"),
            "cgroup_cpu_max": preparation.read_control(g["path"], "cpu.max"),
        } for g in groups],
        "fallback_scheduler_share": fallback_share,
        "workload": {"worker_executable": str(worker_path),
                     "worker_type": "synthetic CPU-bound worker",
                     "duration_s": duration},
        "share_note": "Configured scheduler shares are distinct from cgroup cpu.weight and cpu.max.",
        "expected_result_csv": str(csv_path), "expected_scheduler_log": str(log_path),
        "runtime_limitations": "Checks are non-privileged and cannot prove that BPF attach will succeed on this kernel.",
        "environment": {"kernel_version": platform.release(),
                        "logical_cpu_count": os.cpu_count() or "NA",
                        "architecture": platform.machine()},
    }


def _write_csv(path, run_id, groups, fallback_share, duration, status, error="",
               attached=False, shutdown="not_started", rows=None, worker_path=None):
    rows = rows or {}
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for group in groups:
            measured = rows.get(group["name"], {})
            writer.writerow({
                "run_id": run_id, "timestamp_utc": timestamp, "group": group["name"],
                "kernel_version": platform.release(),
                "logical_cpu_count": os.cpu_count() or "NA",
                "architecture": platform.machine(),
                "worker_executable": str(Path(worker_path).resolve()) if worker_path else "NA",
                "cgroup_id": group["cgroup_id"], "cgroup_path": group["path"],
                "verified_cgroup_id": group.get("verified_cgroup_id", "NA"),
                "identity_verification_status": group.get("identity_verification_status", "unverified"),
                "identity_verification_method": group.get("identity_verification_method", "NA"),
                "cgroup_mount_id": group.get("cgroup_mount_id", "NA"),
                "cgroup_filesystem": group.get("cgroup_filesystem", "NA"),
                "group_share": group["share"], "fallback_share": fallback_share,
                "share_source": "scheduler --group configuration",
                "cgroup_cpu_weight": preparation.read_control(group["path"], "cpu.weight"),
                "cgroup_cpu_max": preparation.read_control(group["path"], "cpu.max"),
                "requested_duration_s": duration,
                "workload_elapsed_s": measured.get("workload_elapsed_s", "NA"),
                "cpu_time_s": measured.get("cpu_time_s", "NA"),
                "exit_status": measured.get("exit_status", "NA"),
                "termination_signal": measured.get("termination_signal", "NA"),
                "completion_status": measured.get("completion_status", "not_started"),
                "measurement_status": measured.get("measurement_status", status),
                "measurement_error": measured.get("measurement_error", error),
                "scheduler_attached": str(attached).lower(),
                "scheduler_shutdown_status": shutdown,
            })


def _loader_command(args, groups):
    command = [str(args.loader), "--fallback-share", str(args.fallback_share)]
    for group in groups:
        command += ["--group", f'{group["cgroup_id"]}:{group["share"]}']
    return command


def _wait_for_ready(process, timeout, log_stream):
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    pending = bytearray()
    try:
        while time.monotonic() < deadline:
            if run_experiment.interrupted:
                raise ExperimentError(f"experiment interrupted by signal {run_experiment.interrupted}")
            if process.poll() is not None:
                raise ExperimentError(f"scheduler loader exited before attachment readiness (status {process.returncode})")
            events = selector.select(min(0.2, max(0, deadline - time.monotonic())))
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    if process.poll() is not None:
                        raise ExperimentError(
                            "scheduler loader exited before attachment readiness "
                            f"(status {process.returncode})")
                    raise ExperimentError("scheduler loader closed its output before attachment readiness")
                pending.extend(chunk)
                while b"\n" in pending:
                    raw_line, _, remainder = pending.partition(b"\n")
                    pending = bytearray(remainder)
                    line = (raw_line + b"\n").decode("utf-8", errors="replace")
                    log_stream.write(line)
                    log_stream.flush()
                    if raw_line.rstrip(b"\r") == READY_LINE.encode("ascii"):
                        return
        raise ExperimentError("timed out waiting for scheduler attachment readiness")
    finally:
        if pending:
            log_stream.write(pending.decode("utf-8", errors="replace"))
            log_stream.flush()
        selector.close()


def _supervisor_metadata(path):
    try:
        fields = path.read_text(encoding="ascii").strip().split("\t")
    except OSError as exc:
        raise ExperimentError(f"supervisor metadata unavailable: {exc}") from exc
    if len(fields) != 8:
        raise ExperimentError("supervisor wrote malformed metadata")
    pid, elapsed_ns, user_us, system_us, exit_status, worker_signal, exec_errno, supervisor_errno = fields
    errors = []
    if exec_errno != "0": errors.append(f"exec_errno={exec_errno}")
    if supervisor_errno != "0": errors.append(f"supervisor_errno={supervisor_errno}")
    if worker_signal != "0": errors.append(f"termination_signal={worker_signal}")
    if exit_status != "0": errors.append(f"exit_status={exit_status}")
    if "NA" in (elapsed_ns, user_us, system_us, exit_status):
        errors.append("one or more worker measurements are unavailable")
    if errors:
        return {"workload_elapsed_s": "NA", "cpu_time_s": "NA",
                "exit_status": exit_status, "termination_signal": worker_signal,
                "completion_status": "incomplete", "measurement_status": "incomplete",
                "measurement_error": "; ".join(errors)}
    return {"workload_elapsed_s": f"{int(elapsed_ns) / 1e9:.9f}",
            "cpu_time_s": f"{(int(user_us) + int(system_us)) / 1e6:.6f}",
            "exit_status": exit_status, "termination_signal": worker_signal,
            "completion_status": "complete", "measurement_status": "complete",
            "measurement_error": ""}


def _stop_loader(process, timeout=10):
    if process.poll() is not None:
        return "exited" if process.returncode == 0 else f"exit_{process.returncode}"
    try:
        process.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return "exited" if process.returncode == 0 else f"exit_{process.returncode}"
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Keep the runner alive until the loader can report its link teardown.
        # Exiting while it still owns the link could leave sched_ext active.
        try:
            process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
        process.wait()
    return "stopped" if process.returncode == 0 else f"exit_{process.returncode}"


def _signal_supervisor(process):
    try:
        process.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        pass


def run_experiment(args, groups, *, popen=subprocess.Popen):
    """Run after preflight; kept injectable so lifecycle failures are testable."""
    run_id, csv_path, manifest_path, log_path = _reserve_outputs(args.output_dir, "run")
    manifest = _manifest(run_id, "run", groups, args.fallback_share, args.duration,
                         csv_path, log_path, args.worker)
    rows = {}
    attached = False
    shutdown = "not_started"
    loader = None
    error = ""
    supervisors = []
    metadata_files = []
    cancel_files = []
    run_experiment.interrupted = 0
    previous_handlers = {}

    def request_stop(signum, _frame):
        run_experiment.interrupted = signum

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, request_stop)
    try:
        with log_path.open("w", encoding="utf-8") as log:
            loader = popen(_loader_command(args, groups), stdout=subprocess.PIPE,
                           stderr=log, text=True, bufsize=1, start_new_session=True)
            _wait_for_ready(loader, args.attach_timeout, log)
            attached = True
            manifest["scheduler_attached"] = True
            manifest["attachment_status"] = "confirmed_by_loader_after_struct_ops_attach"
            for group in groups:
                if run_experiment.interrupted:
                    raise ExperimentError(f"experiment interrupted by signal {run_experiment.interrupted}")
                if loader.poll() is not None:
                    raise ExperimentError("scheduler loader exited before all workloads were launched")
                metadata_fd, metadata_name = tempfile.mkstemp(prefix=run_id + "_", suffix=".meta",
                                                               dir=args.output_dir)
                os.close(metadata_fd)
                cancel_fd, cancel_name = tempfile.mkstemp(prefix=run_id + "_", suffix=".cancel",
                                                           dir=args.output_dir)
                os.close(cancel_fd)
                Path(cancel_name).write_text("0\n", encoding="ascii")
                metadata_path, cancel_path = Path(metadata_name), Path(cancel_name)
                metadata_files.append(metadata_path); cancel_files.append(cancel_path)
                command = [str(args.supervisor), "--cgroup", group["path"], str(metadata_path),
                           str(args.worker), str(args.duration), str(cancel_path)]
                supervisors.append((group, popen(command, stdout=log, stderr=log,
                                                   start_new_session=True)))
            for index, (group, supervisor) in enumerate(supervisors):
                while supervisor.poll() is None:
                    if loader.poll() is not None:
                        raise ExperimentError("scheduler loader exited while workloads were running")
                    if getattr(run_experiment, "interrupted", False):
                        for _group, child in supervisors:
                            if child.poll() is None:
                                _signal_supervisor(child)
                        raise ExperimentError(f"experiment interrupted by signal {run_experiment.interrupted}")
                    time.sleep(0.05)
                rows[group["name"]] = _supervisor_metadata(metadata_files[index])
                if supervisor.returncode != 0 and rows[group["name"]]["measurement_status"] == "complete":
                    rows[group["name"]].update(measurement_status="incomplete",
                                                completion_status="incomplete",
                                                cpu_time_s="NA", workload_elapsed_s="NA",
                                                measurement_error=f"supervisor exited {supervisor.returncode}")
    except Exception as exc:
        error = str(exc)
        for index, (group, child) in enumerate(supervisors):
            if child.poll() is None:
                _signal_supervisor(child)
        # Wait for each supervisor to finish its own worker cleanup before
        # detaching the scheduler. No ordinary-scheduler fallback is possible.
        for index, (group, child) in enumerate(supervisors):
            if child.poll() is None:
                child.wait()
            if index < len(metadata_files):
                try:
                    rows[group["name"]] = _supervisor_metadata(metadata_files[index])
                except (OSError, ExperimentError, ValueError) as metadata_error:
                    rows[group["name"]] = {
                        "exit_status": "NA", "termination_signal": "NA",
                        "measurement_error": f"{error}; supervisor metadata unavailable: {metadata_error}",
                    }
    finally:
        if loader is not None:
            shutdown = _stop_loader(loader)
            if loader.stdout is not None:
                loader.stdout.close()
            if shutdown != "stopped" and not error:
                error = f"scheduler shutdown did not complete cleanly: {shutdown}"
        for path in metadata_files + cancel_files:
            path.unlink(missing_ok=True)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    if run_experiment.interrupted and not error:
        error = f"experiment interrupted by signal {run_experiment.interrupted}"
    overall = "complete" if attached and not error and all(
        rows.get(group["name"], {}).get("measurement_status") == "complete" for group in groups
    ) else "incomplete"
    if overall != "complete":
        if not error:
            error = "one or more group measurements were incomplete; this run is not valid for allocation comparison"
        # A partial run cannot support a group-to-group allocation comparison.
        # Keep the worker exit codes when known, but exclude every CPU/elapsed
        # measurement from the evaluator.
        for group in groups:
            row = rows.setdefault(group["name"], {})
            detail = row.get("measurement_error", "")
            row.update(workload_elapsed_s="NA", cpu_time_s="NA",
                       completion_status="incomplete", measurement_status="incomplete",
                       measurement_error="; ".join(filter(None, (detail, error))))
    manifest["measurement_status"] = overall
    manifest["scheduler_shutdown_status"] = shutdown
    manifest["error"] = error
    manifest["scheduler_log"] = str(log_path)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    _write_csv(csv_path, run_id, groups, args.fallback_share, args.duration,
               overall, error, attached, shutdown, rows, worker_path=args.worker)
    print(f"Result CSV: {csv_path}")
    print(f"Manifest: {manifest_path}")
    if error or overall != "complete":
        return 1
    return 0


def prepare_experiment(args, groups):
    run_id, csv_path, manifest_path, log_path = _reserve_outputs(args.output_dir, "prepare")
    metadata = _manifest(run_id, "prepare", groups, args.fallback_share, args.duration,
                         csv_path, log_path, args.worker)
    metadata["measurement_status"] = "not_run_prepared"
    metadata["scheduler_attached"] = False
    manifest_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    _write_csv(csv_path, run_id, groups, args.fallback_share, args.duration,
               "not_run_prepared", worker_path=args.worker)
    print(f"Manifest: {manifest_path}")
    print(f"Expected result CSV: {csv_path}")
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        groups = validate_groups(args.group, args.fallback_share)
        groups = verify_groups(groups)
        if args.mode == "prepare":
            return prepare_experiment(args, groups)
        if args.mode == "check":
            # Reuse verified identities to ensure check and run have the same gate.
            checked = check_prerequisites(args, identity_verifier=lambda group: group)
            print(f"Checks passed for {len(checked)} verified groups; no scheduler attached or workload started.")
            return 0
        checked = check_prerequisites(args, identity_verifier=lambda group: group)
        return run_experiment(args, checked)
    except (OSError, ValueError, ExperimentError, preparation.IdentityVerificationError) as exc:
        print(f"custom scheduler experiment: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
