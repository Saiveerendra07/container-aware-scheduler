#!/usr/bin/env python3
"""Prepare a custom-scheduler experiment record without attaching a scheduler."""

import argparse
import ctypes
import csv
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


MAX_SHARE = 10000
MAX_GROUPS = 8
MAX_CGROUP_ID = (1 << 64) - 1
FILEID_KERNFS = 0xFE
KERNFS_HANDLE_BYTES = 8
CSV_FIELDS = [
    "run_id", "timestamp_utc", "group", "cgroup_id", "cgroup_path",
    "verified_cgroup_id", "identity_verification_status",
    "identity_verification_method",
    "group_share", "share_source", "cgroup_cpu_weight", "cgroup_cpu_max",
    "requested_duration_s", "workload_elapsed_s", "cpu_time_s",
    "exit_status", "completion_status", "measurement_status",
    "measurement_error",
]


def parse_group(value):
    """Parse NAME:CGROUP_ID:SHARE:CGROUP_DIRECTORY."""
    fields = value.split(":", 3)
    if len(fields) != 4:
        raise argparse.ArgumentTypeError(
            "group must be NAME:CGROUP_ID:SHARE:CGROUP_DIRECTORY")
    name, raw_id, raw_share, raw_path = fields
    if not name or any(ch in name for ch in ",\r\n"):
        raise argparse.ArgumentTypeError("group name must be non-empty CSV text")
    if (not raw_id.isascii() or not raw_id.isdecimal()
            or not 0 < int(raw_id) <= MAX_CGROUP_ID):
        raise argparse.ArgumentTypeError("cgroup ID must be an integer in 1..2^64-1")
    if not raw_share.isascii() or not raw_share.isdecimal():
        raise argparse.ArgumentTypeError("share must be a positive integer")
    share = int(raw_share)
    if not 0 < share <= MAX_SHARE:
        raise argparse.ArgumentTypeError(f"share must be from 1 to {MAX_SHARE}")
    path = Path(raw_path)
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise argparse.ArgumentTypeError(f"cgroup path is unavailable: {path}: {exc}") from exc
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"cgroup path is not a directory: {path}")
    return {"name": name, "cgroup_id": int(raw_id), "share": share,
            "path": str(path)}


class IdentityVerificationError(ValueError):
    """Raised when the kernel cannot provide a trustworthy cgroup ID."""


class _FileHandle(ctypes.Structure):
    _fields_ = [("handle_bytes", ctypes.c_uint),
                ("handle_type", ctypes.c_int),
                ("f_handle", ctypes.c_ubyte * KERNFS_HANDLE_BYTES)]


def read_kernfs_handle(path):
    """Return the kernfs node ID via name_to_handle_at(2), not st_ino."""
    if not hasattr(os, "O_PATH"):
        raise IdentityVerificationError("O_PATH directory descriptors are unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        name_to_handle_at = libc.name_to_handle_at
    except AttributeError as exc:
        raise IdentityVerificationError(
            "libc does not expose name_to_handle_at(2)") from exc
    name_to_handle_at.argtypes = [ctypes.c_int, ctypes.c_char_p,
                                  ctypes.POINTER(_FileHandle),
                                  ctypes.POINTER(ctypes.c_int), ctypes.c_int]
    name_to_handle_at.restype = ctypes.c_int
    path_fd = os.open(path, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        handle = _FileHandle()
        handle.handle_bytes = KERNFS_HANDLE_BYTES
        mount_id = ctypes.c_int()
        if name_to_handle_at(path_fd, b".", ctypes.byref(handle),
                             ctypes.byref(mount_id), 0) != 0:
            error_number = ctypes.get_errno()
            raise IdentityVerificationError(
                f"name_to_handle_at(2) failed for {path}: "
                f"{os.strerror(error_number)}")
        if handle.handle_type != FILEID_KERNFS:
            raise IdentityVerificationError(
                f"unexpected handle type {handle.handle_type}; expected FILEID_KERNFS")
        if handle.handle_bytes != KERNFS_HANDLE_BYTES:
            raise IdentityVerificationError(
                f"unexpected kernfs handle size {handle.handle_bytes}")
        cgroup_id = int.from_bytes(bytes(handle.f_handle), byteorder=sys.byteorder)
        if cgroup_id <= 0:
            raise IdentityVerificationError("kernel returned an invalid cgroup ID")
        return cgroup_id, mount_id.value
    finally:
        os.close(path_fd)


def mount_filesystem_type(mount_id, mountinfo_path="/proc/self/mountinfo"):
    matches = []
    try:
        with open(mountinfo_path, encoding="utf-8") as stream:
            for line in stream:
                fields = line.rstrip("\n").split(" - ", 1)
                if len(fields) != 2:
                    continue
                left, right = fields[0].split(), fields[1].split()
                if left and right and left[0].isdecimal() \
                        and int(left[0]) == mount_id:
                    matches.append(right[0])
    except OSError as exc:
        raise IdentityVerificationError(
            f"cannot read mount table {mountinfo_path}: {exc}") from exc
    if len(matches) != 1:
        raise IdentityVerificationError(
            f"mount ID {mount_id} is missing or ambiguous in mount table")
    return matches[0]


def resolve_cgroup_identity(path, handle_reader=read_kernfs_handle,
                            mountinfo_path="/proc/self/mountinfo"):
    """Resolve cgrp->kn->id, the value compared by group_scheduler.bpf.c."""
    try:
        cgroup_id, mount_id = handle_reader(path)
    except (OSError, IdentityVerificationError) as exc:
        raise IdentityVerificationError(str(exc)) from exc
    filesystem = mount_filesystem_type(mount_id, mountinfo_path)
    if filesystem != "cgroup2":
        raise IdentityVerificationError(
            f"path is on {filesystem!r}, not a cgroup2 mount")
    return {"cgroup_id": cgroup_id, "mount_id": mount_id,
            "filesystem": filesystem,
            "method": "name_to_handle_at(FILEID_KERNFS)"}


def verify_group_identity(group):
    identity = resolve_cgroup_identity(group["path"])
    actual_id = identity["cgroup_id"]
    if actual_id != group["cgroup_id"]:
        raise IdentityVerificationError(
            f"group {group['name']!r}: supplied cgroup ID {group['cgroup_id']} "
            f"does not match kernel ID {actual_id}")
    return {**group, "verified_cgroup_id": actual_id,
            "identity_verification_status": "verified",
            "identity_verification_method": identity["method"],
            "cgroup_mount_id": identity["mount_id"],
            "cgroup_filesystem": identity["filesystem"]}


def read_control(path, filename):
    try:
        return (Path(path) / filename).read_text(encoding="ascii").strip()
    except OSError:
        return "NA"


def create_unique_paths(output_dir, run_id):
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    try:
        for suffix in (".csv", ".json"):
            fd, path = tempfile.mkstemp(prefix=run_id + "_", suffix=suffix,
                                        dir=output_dir)
            os.close(fd)
            paths.append(Path(path))
        return paths
    except Exception:
        for path in paths:
            path.unlink(missing_ok=True)
        raise


def prepare(groups, output_dir, duration):
    names = [group["name"] for group in groups]
    ids = [group["cgroup_id"] for group in groups]
    paths = [str(Path(group["path"]).resolve(strict=True)) for group in groups]
    if not 2 <= len(groups) <= MAX_GROUPS:
        raise ValueError(f"scheduler requires 2 to {MAX_GROUPS} configured groups")
    if len(set(names)) != len(names):
        raise ValueError("group names must be unique")
    if len(set(ids)) != len(ids):
        raise ValueError("cgroup IDs must be unique")
    if len(set(paths)) != len(paths):
        raise ValueError("cgroup paths must be unique")
    for group in groups:
        if (type(group["cgroup_id"]) is not int
                or not 0 < group["cgroup_id"] <= MAX_CGROUP_ID
                or type(group["share"]) is not int
                or not 0 < group["share"] <= MAX_SHARE):
            raise ValueError("cgroup IDs and shares must be positive integers; "
                             f"shares are capped at {MAX_SHARE}")
    verified_groups = [verify_group_identity(group) for group in groups]

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run_id = ("custom_scheduler_prepare_"
              + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
              + "_" + uuid.uuid4().hex[:8])
    csv_path, metadata_path = create_unique_paths(Path(output_dir), run_id)
    share_map = ";".join(f'{g["name"]}={g["share"]}' for g in verified_groups)
    try:
        with csv_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for group in verified_groups:
                writer.writerow({
                    "run_id": run_id,
                    "timestamp_utc": timestamp,
                    "group": group["name"],
                    "cgroup_id": group["cgroup_id"],
                    "cgroup_path": group["path"],
                    "verified_cgroup_id": group["verified_cgroup_id"],
                    "identity_verification_status": group["identity_verification_status"],
                    "identity_verification_method": group["identity_verification_method"],
                    "group_share": group["share"],
                    "share_source": "scheduler --group configuration",
                    "cgroup_cpu_weight": read_control(group["path"], "cpu.weight"),
                    "cgroup_cpu_max": read_control(group["path"], "cpu.max"),
                    "requested_duration_s": duration,
                    "workload_elapsed_s": "NA",
                    "cpu_time_s": "NA",
                    "exit_status": "NA",
                    "completion_status": "not_started",
                    "measurement_status": "not_run_prepared",
                    "measurement_error": "",
                })
        metadata = {
            "run_id": run_id,
            "timestamp_utc": timestamp,
            "mode": "prepare_only",
            "scheduler_attached": False,
            "measurement_status": "not_run_prepared",
            "requested_duration_s": duration,
            "scheduler_groups": [{
                "name": g["name"], "cgroup_id": g["cgroup_id"],
                "configured_share": g["share"], "cgroup_path": g["path"],
                "verified_cgroup_id": g["verified_cgroup_id"],
                "identity_verification_status": g["identity_verification_status"],
                "identity_verification_method": g["identity_verification_method"],
                "cgroup_mount_id": g["cgroup_mount_id"],
                "cgroup_filesystem": g["cgroup_filesystem"],
            } for g in verified_groups],
            "scheduler_share_map": share_map,
            "controls_note": "cpu.weight and cpu.max are read-only cgroup controls; they are not scheduler shares.",
            "runtime_blocker": "This command only prepares records; use run_custom_scheduler_experiment.py run for explicit runtime execution.",
        }
        with metadata_path.open("w", encoding="utf-8") as stream:
            json.dump(metadata, stream, indent=2)
            stream.write("\n")
    except Exception:
        csv_path.unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)
        raise
    return csv_path, metadata_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true",
                      help="write a non-privileged preparation record; never attaches")
    mode.add_argument("--identify-cgroup", metavar="DIRECTORY",
                      help="print the kernel-verified ID for one cgroup-v2 directory")
    parser.add_argument("--group", action="append", type=parse_group,
                        metavar="NAME:CGROUP_ID:SHARE:CGROUP_DIRECTORY",
                        help="repeat for each explicitly configured scheduler group")
    parser.add_argument("--duration", type=float,
                        help="requested workload duration in seconds")
    parser.add_argument("--output-dir", default="results/raw",
                        help="directory for new CSV and JSON files (default: results/raw)")
    args = parser.parse_args(argv)
    if args.identify_cgroup:
        try:
            path = Path(args.identify_cgroup).resolve(strict=True)
            if not path.is_dir():
                raise IdentityVerificationError(f"not a directory: {path}")
            identity = resolve_cgroup_identity(str(path))
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps({"cgroup_path": str(path), **identity}, sort_keys=True))
        return 0
    if not args.group or len(args.group) < 2:
        parser.error("--prepare requires at least two --group values")
    if args.duration is None or args.duration <= 0:
        parser.error("--prepare requires a positive --duration")
    try:
        csv_path, metadata_path = prepare(args.group, args.output_dir, args.duration)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Prepared CSV (measurements not run): {csv_path}")
    print(f"Metadata: {metadata_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
