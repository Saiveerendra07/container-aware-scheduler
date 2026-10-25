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
    try:
        command_words = shlex.split(name)
    except ValueError:
        command_words = []
    if not command_words:
        return None
    executable = which(command_words[0])
    if not executable:
        return None
    command = [executable, *command_words[1:], *args]
    try:
        return run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def _resolve(name, which):
    try:
        command_words = shlex.split(name)
    except ValueError:
        return None
    return which(command_words[0]) if command_words else None


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _shell_command(parts):
    return shlex.join([str(part) for part in parts])


def diagnose(*, sys_root="/sys", proc_root="/proc", scx_root=None,
             env=None, which=shutil.which, run=subprocess.run, build_only=False):
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
    pkg = _resolve(pkg_name, which)
    metadata_ok = False
    lib_version = "unavailable"
    cflags = ""
    libs = ""
    if pkg:
        version_cmd = _command(pkg_name, ["--modversion", "libbpf"], which=which, run=run)
        cflags_cmd = _command(pkg_name, ["--cflags", "libbpf"], which=which, run=run)
        libs_cmd = _command(pkg_name, ["--libs", "libbpf"], which=which, run=run)
        minimum_cmd = _command(pkg_name, ["--atleast-version=1.5", "libbpf"], which=which, run=run)
        if version_cmd and version_cmd.returncode == 0:
            lib_version = version_cmd.stdout.strip()
        metadata_ok = bool(version_cmd and version_cmd.returncode == 0
                           and minimum_cmd and minimum_cmd.returncode == 0
                           and cflags_cmd and cflags_cmd.returncode == 0
                           and libs_cmd and libs_cmd.returncode == 0)
        if cflags_cmd and cflags_cmd.returncode == 0:
            cflags = cflags_cmd.stdout.strip()
        if libs_cmd and libs_cmd.returncode == 0:
            libs = libs_cmd.stdout.strip()
    add("libbpf pkg-config metadata", metadata_ok,
        f"libbpf version {lib_version}; cflags: {cflags or '(empty)'}; libs: {libs or '(empty)'}; Makefile requires pkg-config libbpf >= 1.5." if pkg else
        f"{pkg_name} is missing; cannot query required libbpf metadata.",
        "Provide pkg-config metadata for libbpf >= 1.5 and set PKG_CONFIG if needed; do not install packages as part of this diagnostic." if not metadata_ok else "")

    include_dirs = [Path("/usr/include"), Path("/usr/local/include")]
    try:
        flag_words = shlex.split(cflags)
        index = 0
        while index < len(flag_words):
            flag = flag_words[index]
            if flag in ("-I", "-isystem") and index + 1 < len(flag_words):
                include_dirs.insert(0, Path(flag_words[index + 1]))
                index += 1
            elif flag.startswith("-I") and len(flag) > 2:
                include_dirs.insert(0, Path(flag[2:]))
            elif flag.startswith("-isystem") and len(flag) > len("-isystem"):
                include_dirs.insert(0, Path(flag[len("-isystem"):]))
            index += 1
    except ValueError:
        pass
    libbpf_header = next((directory / "bpf/libbpf.h" for directory in include_dirs
                          if (directory / "bpf/libbpf.h").is_file()), None)
    header_probe_ok = False
    header_probe_error = ""
    if libbpf_header is not None and cc and cc.returncode == 0:
        header_probe = shlex.split(cc_name) + (shlex.split(cflags) if cflags else []) + [
            "-x", "c", "-fsyntax-only", "-"]
        try:
            header_result = run(header_probe, input="#include <bpf/libbpf.h>\n",
                                capture_output=True, text=True, check=False)
        except OSError as exc:
            header_result = subprocess.CompletedProcess(header_probe, 127, "", str(exc))
        header_probe_ok = header_result.returncode == 0
        header_probe_error = header_result.stderr.strip()
    header_ok = libbpf_header is not None and header_probe_ok
    header_detail = (f"Found {libbpf_header}; {cc_name} can compile <bpf/libbpf.h>."
                     if header_ok else
                     ("bpf/libbpf.h was not found in pkg-config include paths or common system include directories."
                      if libbpf_header is None else
                      f"Found {libbpf_header}, but {cc_name} cannot compile it: {header_probe_error or 'compiler unavailable or returned nonzero.'}"))
    add("libbpf development header", header_ok, header_detail,
        "Provide readable libbpf development headers matching pkg-config's include flags, then rerun the preflight." if not header_ok else "")

    root = Path(scx_root or env.get("SCX_ROOT", "~/scx-c-examples")).expanduser()
    scx_include = root / "scheds/include"
    required_paths = ((scx_include / "scx/common.bpf.h", "file"),
                      (scx_include / "scx/common.h", "file"),
                      (scx_include / "bpf-compat", "directory"),
                      (scx_include / "lib", "directory"))
    missing = [str(path) for path, kind in required_paths
               if not (path.is_file() if kind == "file" else path.is_dir())]
    add("Reference sched_ext headers", not missing,
        f"All Makefile reference paths exist under {scx_include}." if not missing else
        "Missing: " + ", ".join(missing),
        f"Set SCX_ROOT to an existing sched_ext reference checkout containing scheds/include/scx and its bpf-compat/lib directories; do not clone or update it during this read-only check." if missing else "")

    build_dir = Path(env.get("BUILD_DIR", "build"))
    btf = Path(env.get("VMLINUX_BTF", "/sys/kernel/btf/vmlinux"))
    cc_words = shlex.split(env.get("CC", "cc"))
    clang_words = shlex.split(env.get("CLANG", "clang"))
    bpftool_words = shlex.split(env.get("BPFTOOL", "bpftool"))
    scx_include_text = str(scx_include)
    arch_value = "unknown"
    if uname and uname.returncode == 0 and arch_cmd and arch_cmd.returncode == 0:
        raw_arch = arch_cmd.stdout.strip()
        arch_value = {"x86_64": "x86", "aarch64": "arm64", "s390x": "s390"}.get(raw_arch, raw_arch)
    bpf_flags = ["-g", "-O2", "-target", "bpf", "-mcpu=v3", f"-D__TARGET_ARCH_{arch_value}",
                 f"-I{build_dir}", "-Iinclude", f"-I{scx_include_text}",
                 f"-I{scx_include_text}/bpf-compat", f"-I{scx_include_text}/lib"]
    if cflags:
        bpf_flags.extend(shlex.split(cflags))
    user_flags = ["-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Iinclude",
                  f"-I{build_dir}", "-isystem", scx_include_text]
    if cflags:
        user_flags.extend(shlex.split(cflags))
    build_steps = [
        "Required source/header inputs: src/group_scheduler.bpf.c, src/group_scheduler.c, include/group_scheduler.h, generated build/vmlinux.h, generated build/group_scheduler.skel.h, <bpf/libbpf.h>",
        "Required reference headers: " + ", ".join(str(path) for path, _kind in required_paths),
        f"Required BTF input: {btf}",
        f"libbpf metadata: pkg-config --atleast-version=1.5 libbpf; pkg-config --cflags --libs libbpf => {cflags or '(unavailable)'} {libs or '(unavailable)'}; userspace also links -lelf -lz -lzstd -lpthread",
        "libbpf header check: " + _shell_command(shlex.split(cc_name) + (shlex.split(cflags) if cflags else []) + ["-x", "c", "-fsyntax-only", "-"]) + " with stdin '#include <bpf/libbpf.h>'",
        "BTF header command: " + _shell_command(bpftool_words + ["btf", "dump", "file", str(btf), "format", "c"]) + f" > {shlex.quote(str(build_dir / 'vmlinux.h'))}",
        "BPF target command: " + _shell_command(clang_words + bpf_flags + ["-c", "src/group_scheduler.bpf.c", "-o", str(build_dir / "group_scheduler.bpf.o")]),
        "Skeleton command: " + _shell_command(bpftool_words + ["gen", "skeleton", str(build_dir / "group_scheduler.bpf.o"), "name", "group_scheduler"]) + f" > {shlex.quote(str(build_dir / 'group_scheduler.skel.h'))}",
        "Userspace link probe: " + _shell_command(cc_words + ["-x", "c", "-o", "/dev/null", "-"]
            + (shlex.split(libs) if libs else ["-lbpf"]) + ["-lelf", "-lz", "-lzstd", "-lpthread"]) + " with trivial C input",
        "Userspace loader command: " + _shell_command(cc_words + user_flags + ["src/group_scheduler.c", "-o", str(build_dir / "group_scheduler")]
            + (shlex.split(libs) if libs else ["-lbpf"]) + ["-lelf", "-lz", "-lzstd", "-lpthread"]),
        "Reproducible Make invocation: " + _shell_command([
            "make", f"CC={env.get('CC', 'cc')}", f"CLANG={env.get('CLANG', 'clang')}",
            f"BPFTOOL={env.get('BPFTOOL', 'bpftool')}",
            f"PKG_CONFIG={env.get('PKG_CONFIG', 'pkg-config')}",
            f"SCX_ROOT={root}", f"VMLINUX_BTF={btf}", f"BUILD_DIR={build_dir}"]),
    ]
    add("Makefile build plan", True, "\n".join(build_steps))

    linker_ok = False
    linker_detail = "C compiler or libbpf linker flags are unavailable; the userspace link check was skipped."
    linker_action = "Provide a working CC and libbpf pkg-config --libs metadata, then rerun build preflight."
    if cc and cc.returncode == 0 and pkg and libs:
        link_probe = cc_words + ["-x", "c", "-o", "/dev/null", "-"] + shlex.split(libs) + [
            "-lelf", "-lz", "-lzstd", "-lpthread"]
        try:
            linked = run(link_probe, input="int main(void) { return 0; }\n",
                         capture_output=True, text=True, check=False)
        except OSError as exc:
            linked = subprocess.CompletedProcess(link_probe, 127, "", str(exc))
        linker_ok = linked.returncode == 0
        linker_detail = ("The userspace target's libbpf and fixed link dependencies resolve."
                         if linker_ok else "Userspace link probe failed: " + (linked.stderr.strip() or "compiler/linker returned nonzero."))
        linker_action = ("" if linker_ok else
                         "Provide the development/linker libraries named in this error and rerun; Makefile requires libbpf plus -lelf -lz -lzstd -lpthread.")
    add("Userspace linker dependencies", linker_ok, linker_detail, linker_action)

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

    informational = {"Host environment heuristic"}
    if build_only:
        # sched_ext may be enabled while compiling; the runner still requires
        # the interface to be readable and disabled before an attach attempt.
        informational.add("sched_ext state interface")
    ready = all(entry["ok"] for entry in entries if entry["name"] not in informational)
    return entries, ready


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scx-root", help="sched_ext reference checkout (default: SCX_ROOT or ~/scx-c-examples)")
    parser.add_argument("--build-only", action="store_true",
                        help="judge BPF/userspace build prerequisites; still report sched_ext state without gating build status")
    args = parser.parse_args(argv)
    entries, ready = diagnose(scx_root=args.scx_root, build_only=args.build_only)
    mode = "build preflight" if args.build_only else "runtime-readiness diagnostic"
    print(f"Custom sched_ext {mode} (read-only)\n")
    for entry in entries:
        label = "OK" if entry["ok"] else "MISSING/NOT READY"
        detail = entry["detail"].replace("\n", "\n  ")
        print(f"[{label}] {entry['name']}: {detail}")
        if entry["action"]:
            print(f"  Next action: {entry['action']}")
    status = ("build prerequisites detected" if args.build_only else "prerequisites detected")
    failure = ("build prerequisites missing" if args.build_only else "prerequisites missing or runtime state not ready")
    print("\nReadiness: " + (status if ready else failure))
    print("Passing this diagnostic does not prove kernel, verifier, attach, or scheduler compatibility.")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
