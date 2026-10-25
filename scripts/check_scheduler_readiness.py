#!/usr/bin/env python3
"""Read-only preflight for building and preparing the custom sched_ext scheduler."""

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def _command(name, args, *, which, run):
    executable = which(name)
    if not executable:
        return None
    try:
        return run([executable, *args], capture_output=True, text=True, check=False)
    except OSError as exc:
        return subprocess.CompletedProcess([executable, *args], 127, "", str(exc))


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def diagnose(*, sys_root="/sys", proc_root="/proc", scx_root=None,
             env=None, which=shutil.which, run=subprocess.run):
    """Return (diagnostic entries, ready), with command and roots injectable for tests."""
    env = dict(os.environ if env is None else env)
    sys_root, proc_root = Path(sys_root), Path(proc_root)
    entries = []

    def add(name, ok, detail, action=""):
        entries.append({"name": name, "ok": bool(ok), "detail": detail, "action": action})

    uname = _command("uname", ["-sr"], which=which, run=run)
    arch_cmd = _command("uname", ["-m"], which=which, run=run)
    if uname and uname.returncode == 0 and arch_cmd and arch_cmd.returncode == 0:
        release = uname.stdout.strip()
        arch = arch_cmd.stdout.strip()
        target_arch = {"x86_64": "x86", "aarch64": "arm64", "s390x": "s390"}.get(arch, arch)
        add("Kernel and architecture", True,
            f"{release}; architecture {arch}; Makefile BPF target __TARGET_ARCH_{target_arch}.")
    else:
        add("Kernel and architecture", False, "uname could not report kernel and architecture.",
            "Install or restore the coreutils/util-linux uname command, then rerun this diagnostic.")

    btf_path = sys_root / "kernel/btf/vmlinux"
    try:
        with btf_path.open("rb") as stream:
            btf_readable = bool(stream.read(1))
    except OSError:
        btf_readable = False
    if not btf_readable:
        add("Kernel BTF", False, f"{btf_path} is missing or unreadable.",
            "Use a kernel that exposes readable vmlinux BTF at this path; do not generate or substitute BTF here.")
    else:
        add("Kernel BTF", True, f"{btf_path} is present and readable.")

    state_path = sys_root / "kernel/sched_ext/state"
    state = _read(state_path)
    if state is None:
        add("sched_ext state interface", False, f"{state_path} is unavailable or unreadable (interface status unknown).",
            "Use a kernel with the sched_ext state interface and readable sysfs access; do not change kernel settings to satisfy this check.")
    elif state.lower() == "disabled":
        add("sched_ext state interface", True, f"Interface is readable and reports disabled at {state_path}.")
    elif state.lower() == "enabled":
        add("sched_ext state interface", False, f"Interface is readable but scheduler state is enabled at {state_path}.",
            "Do not attach another scheduler; arrange a safe, authorized shutdown and rerun when state reports disabled.")
    else:
        add("sched_ext state interface", False,
            f"Interface is readable but reports unrecognized state {state!r}.",
            "Check this kernel's sched_ext interface and stop before an attach attempt until state is understood.")

    cc_name = env.get("CC", "cc")
    cc = _command(cc_name, ["--version"], which=which, run=run)
    add("C compiler", bool(cc and cc.returncode == 0),
        (cc.stdout.splitlines()[0] if cc and cc.returncode == 0 and cc.stdout else
         f"{cc_name} is missing or failed."),
        "Install/provide a working C compiler or set CC to one; this script does not install it." if not cc or cc.returncode else "")

    clang_name = env.get("CLANG", "clang")
    clang = _command(clang_name, ["-print-targets"], which=which, run=run)
    clang_ok = bool(clang and clang.returncode == 0 and re.search(r"\bbpf\b", clang.stdout, re.I))
    add("Clang BPF backend", clang_ok,
        "Clang reports a BPF target." if clang_ok else
        f"{clang_name} is missing, failed, or does not list a BPF target.",
        "Provide a Clang build with the BPF target enabled or set CLANG; do not substitute a non-BPF compiler." if not clang_ok else "")

    bpftool_name = env.get("BPFTOOL", "bpftool")
    bpftool = _command(bpftool_name, ["version"], which=which, run=run)
    bpftool_ok = bool(bpftool and bpftool.returncode == 0)
    bpftool_detail = (bpftool.stdout.strip().splitlines()[0] if bpftool_ok and bpftool.stdout.strip()
                      else f"{bpftool_name} is missing or failed.")
    add("bpftool", bpftool_ok, bpftool_detail,
        "Provide a working bpftool executable and set BPFTOOL if it is not on PATH." if not bpftool_ok else "")

    pkg_name = env.get("PKG_CONFIG", "pkg-config")
    pkg = which(pkg_name)
    metadata_ok = False
    lib_version = "unavailable"
    cflags = ""
    if pkg:
        version_cmd = _command(pkg_name, ["--modversion", "libbpf"], which=which, run=run)
        cflags_cmd = _command(pkg_name, ["--cflags", "libbpf"], which=which, run=run)
        minimum_cmd = _command(pkg_name, ["--atleast-version=1.5", "libbpf"], which=which, run=run)
        if version_cmd and version_cmd.returncode == 0:
            lib_version = version_cmd.stdout.strip()
        metadata_ok = bool(minimum_cmd and minimum_cmd.returncode == 0
                           and cflags_cmd and cflags_cmd.returncode == 0)
        if cflags_cmd and cflags_cmd.returncode == 0:
            cflags = cflags_cmd.stdout.strip()
    add("libbpf pkg-config metadata", metadata_ok,
        f"libbpf version {lib_version}; Makefile requires pkg-config libbpf >= 1.5." if pkg else
        f"{pkg_name} is missing; cannot query required libbpf metadata.",
        "Provide pkg-config metadata for libbpf >= 1.5 and set PKG_CONFIG if needed; do not install packages as part of this diagnostic." if not metadata_ok else "")

    include_dirs = [Path("/usr/include"), Path("/usr/local/include")]
    try:
        for flag in shlex.split(cflags):
            if flag.startswith("-I") and len(flag) > 2:
                include_dirs.insert(0, Path(flag[2:]))
    except ValueError:
        pass
    libbpf_header = next((directory / "bpf/libbpf.h" for directory in include_dirs
                          if (directory / "bpf/libbpf.h").is_file()), None)
    add("libbpf development header", libbpf_header is not None,
        f"Found {libbpf_header}." if libbpf_header else "bpf/libbpf.h was not found in pkg-config include paths or common system include directories.",
        "Provide the libbpf development headers matching the pkg-config metadata; rerun after arranging the build environment." if libbpf_header is None else "")

    root = Path(scx_root or env.get("SCX_ROOT", "~/scx-c-examples")).expanduser()
    scx_include = root / "scheds/include"
    required_paths = (scx_include / "scx/common.bpf.h", scx_include / "scx/common.h",
                      scx_include / "bpf-compat", scx_include / "lib")
    missing = [str(path) for path in required_paths if not path.exists()]
    add("Reference sched_ext headers", not missing,
        f"All Makefile reference paths exist under {scx_include}." if not missing else
        "Missing: " + ", ".join(missing),
        f"Set SCX_ROOT to an existing sched_ext reference checkout containing scheds/include/scx and its bpf-compat/lib directories; do not clone or update it during this read-only check." if missing else "")

    osrelease = _read(proc_root / "sys/kernel/osrelease") or ""
    proc_version = _read(proc_root / "version") or ""
    wsl_markers = [marker for marker in ("microsoft", "wsl")
                   if marker in (osrelease + " " + proc_version).lower()]
    if env.get("WSL_INTEROP") or env.get("WSL_DISTRO_NAME"):
        wsl_markers.append("WSL environment variable")
    if wsl_markers:
        detail = "Environment appears to be WSL2/WSL based on " + ", ".join(sorted(set(wsl_markers))) + ". This heuristic is not definitive."
    else:
        detail = "No common WSL markers detected; environment appears to be conventional Linux. This heuristic is not definitive."
    add("Host environment heuristic", True, detail + " Host classification does not establish sched_ext support.")

    ready = all(entry["ok"] for entry in entries if entry["name"] != "Host environment heuristic")
    return entries, ready


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scx-root", help="sched_ext reference checkout (default: SCX_ROOT or ~/scx-c-examples)")
    args = parser.parse_args(argv)
    entries, ready = diagnose(scx_root=args.scx_root)
    print("Custom sched_ext runtime-readiness diagnostic (read-only)\n")
    for entry in entries:
        label = "OK" if entry["ok"] else "MISSING/NOT READY"
        print(f"[{label}] {entry['name']}: {entry['detail']}")
        if entry["action"]:
            print(f"  Next action: {entry['action']}")
    print("\nReadiness: " + ("prerequisites detected" if ready else "prerequisites missing or runtime state not ready"))
    print("Passing this diagnostic does not prove kernel, verifier, attach, or scheduler compatibility.")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
