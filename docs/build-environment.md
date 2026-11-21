# Build environment and compatibility notes

This document records the environments actually checked for this repository. It separates workload/test validation from building and running the custom `sched_ext` scheduler.

## 1. Personal Ubuntu WSL2 environment

Last checked kernel:

```text
Linux 6.6.87.2-microsoft-standard-WSL2
```

The WSL2 kernel did not expose `/sys/kernel/sched_ext/state`. Its cgroup-v2 root was not writable by the project user. Therefore:

- CPU workload binaries can be built with `make workloads`.
- Python unit tests and shell syntax checks can run normally.
- The custom scheduler cannot be attached or runtime-tested in this environment because the required sched_ext state interface is absent.
- A successful unit-test run or synthetic test fixture is not evidence of BPF attachment or scheduler performance.

The following commands were verified in WSL2:

```sh
bash -n scripts/*.sh
make workloads
python3 -m unittest discover -s tests -q
bash scripts/run_custom_scheduler_demo.sh --help
```

Most recent recorded result: 106 unit tests passed. The baseline CPU competition runner was also run in WSL2; its output uses the ordinary active Linux scheduler and must not be labelled as a custom sched_ext result.

## 2. Institute Linux build profile (previously checked)

The institute host previously reported:

| Item | Observed value |
| --- | --- |
| Distribution | Ubuntu 24.04.3 |
| Kernel | `7.0.0-38-generic` |
| Architecture | x86_64 |
| CPUs / RAM | 16 CPUs / 15 GiB |
| sched_ext config | `CONFIG_SCHED_CLASS_EXT=y` |
| sched_ext state interface | Present; reported `disabled` |
| Kernel BTF | `/sys/kernel/btf/vmlinux` present |
| Clang | 18.1.3 |
| bpftool | 7.7.0 |
| System libbpf | 1.3.0 (below this project's Makefile minimum) |

A private libbpf 1.7.0 build was used instead of changing the system library. The sched_ext reference headers were taken from a detached worktree at commit:

```text
c2e41fa619c3184aa663f90d3eceaf7fd746e958
```

That header revision was selected because newer sched-ext headers from the then-current main branch used APIs absent from the institute's Linux 7.0 kernel. The BPF source uses `scx_bpf_task_cgroup(p)` with this older header set.

The following build succeeded on that host, with the private dependency paths already prepared:

```sh
export PKG_CONFIG_PATH=/tmp/container-aware-scheduler-deps/libbpf/lib64/pkgconfig
export LD_LIBRARY_PATH=/tmp/container-aware-scheduler-deps/libbpf/lib64:${LD_LIBRARY_PATH:-}

make SCX_ROOT=/tmp/scx-linux70-headers \
  BUILD_DIR=/tmp/cas-build-linux70-oldheaders all
```

The build produced:

- `/tmp/cas-build-linux70-oldheaders/group_scheduler.bpf.o`
- `/tmp/cas-build-linux70-oldheaders/group_scheduler.skel.h`
- `/tmp/cas-build-linux70-oldheaders/group_scheduler`

This was a successful compile, not a successful scheduler attach or runtime experiment. The dependency trees and build outputs were under `/tmp`; they are not part of a fresh clone and may not persist after reboot or cleanup.

## 3. Cgroup and runtime restrictions observed on the institute host

Although sched_ext support and the BPF build were available, the cgroup-v2 root was observed as root-owned and read-only, and the user had no effective `CAP_BPF` or `CAP_SYS_ADMIN` capability. The user's slice also did not have delegation enabled.

The root-run demo wrapper still requires its selected cgroup-v2 parent to permit child cgroup creation. `sudo` does not override a read-only cgroup mount or host policy. A writable, authorized parent and a safe recovery procedure must be arranged by the system administrator before an attach test.

An initial custom-scheduler attachment and workload run was later confirmed on
this Linux 7.0 host, but the run was not a controlled repeated weighted-fairness
study. The measured 100:300 case was 42.7%:57.3%, not evidence of 25%:75%
weighted-share enforcement.

## 4. Requirements for a fresh compatible Linux build

The Makefile checks for:

- a C compiler;
- Clang with the BPF target;
- bpftool;
- pkg-config metadata for libbpf 1.5 or newer, plus matching development headers and libraries;
- userspace link dependencies `libelf`, `zlib`, `zstd`, and pthread;
- sched_ext reference headers in the layout `$SCX_ROOT/scheds/include`;
- readable BTF from the target kernel at `/sys/kernel/btf/vmlinux`.

Use BTF from the kernel that will run the scheduler. Do not substitute BTF from a different kernel. Do not assume an arbitrary sched-ext checkout is compatible: record its commit and verify the actual BPF and userspace builds.

Read-only diagnostics:

```sh
python3 scripts/check_scheduler_readiness.py --build-only \
  --scx-root /absolute/path/to/compatible/scx-c-examples
```

Build after the diagnostics pass:

```sh
make SCX_ROOT=/absolute/path/to/compatible/scx-c-examples \
  VMLINUX_BTF=/sys/kernel/btf/vmlinux all
make workloads
```

The `--build-only` diagnostic checks build prerequisites; it does not establish runtime readiness. Before any runtime test, the target kernel must expose a readable sched_ext state interface reporting `disabled`, and the experiment must pass the project's cgroup identity, permissions, capability, and recovery checks.

## 5. Evidence and claim boundaries

Keep these outcomes distinct in reports and demonstrations:

1. **Workload build:** CPU worker and supervisor compile.
2. **Automated tests:** Python tests pass; many custom-runner cases use synthetic or mocked fixtures.
3. **Custom scheduler build:** BPF object and userspace loader compile against the selected target-kernel headers and BTF.
4. **Custom scheduler runtime:** struct-ops attachment is confirmed, workers are placed in the intended cgroups, measurements complete, and shutdown is confirmed.
5. **Performance evaluation:** repeated controlled custom-scheduler runs are compared with an explicitly documented baseline.

Only outcomes 1–3 have been established in the recorded work. Outcomes 4–5 remain pending.

## 6. Current Linux 7.0.0-42 Investigation

The host currently reports kernel `7.0.0-42-generic`, Clang 18.1.3,
bpftool 7.7.0, BTF at `/sys/kernel/btf/vmlinux`, and sched_ext state
`disabled`. The selected sched_ext headers are `/tmp/scx-linux70-headers` at
`c2e41fa619c3184aa663f90d3eceaf7fd746e958`. Private libbpf 1.7.0's library is
under `/tmp/container-aware-scheduler-deps/libbpflib64`, while its matching
headers are under `/tmp/container-aware-scheduler-deps/libbpf/usr/include`.

The private `/tmp/container-aware-scheduler-deps/libbpflib64/pkgconfig/libbpf.pc`
currently points `includedir` at `/usr/include` and uses relative `libdir=lib64`.
System pkg-config reports libbpf 1.3.0. Selecting the private `.pc` file alone
therefore does not select matching headers and library flags. Use command-line
pkg-config variable overrides and a fresh output directory for a reproducible
build:

```sh
PRIVATE_PREFIX=/tmp/container-aware-scheduler-deps/libbpflib64
PRIVATE_INCLUDE=/tmp/container-aware-scheduler-deps/libbpf/usr/include
SCX_ROOT=/tmp/scx-linux70-headers
BUILD_DIR=$(mktemp -d /tmp/cas-build-final.XXXXXX)
export PKG_CONFIG_PATH="$PRIVATE_PREFIX/pkgconfig"
export LD_LIBRARY_PATH="$PRIVATE_PREFIX${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
PKG_CONFIG="pkg-config --define-variable=libdir=$PRIVATE_PREFIX --define-variable=includedir=$PRIVATE_INCLUDE"

make check-deps CLANG=clang-18 SCX_ROOT="$SCX_ROOT" \
  VMLINUX_BTF=/sys/kernel/btf/vmlinux PKG_CONFIG="$PKG_CONFIG"
make all workloads CLANG=clang-18 SCX_ROOT="$SCX_ROOT" \
  VMLINUX_BTF=/sys/kernel/btf/vmlinux PKG_CONFIG="$PKG_CONFIG" \
  BUILD_DIR="$BUILD_DIR"
```

The loader has no embedded RPATH/RUNPATH. When invoked through `sudo`,
`LD_LIBRARY_PATH` may be filtered; the demo wrapper accepts
`--libbpf-libdir /tmp/container-aware-scheduler-deps/libbpflib64` and verifies
that `ldd` resolves the loader to that directory. A successful build or
library-resolution check does not demonstrate BPF verifier acceptance or
attachment.

The preserved run record
`results/raw/custom_scheduler_run_20261121T074006Z_2666ab5cb7_e7c9nmxv.json`
reports `scheduler_attached=false`, `measurement_status=incomplete`, loader
status `-11`, and an empty scheduler log. That run used temporary
`cas_demo_*` cgroup paths; those paths are gone. The currently named
`/sys/fs/cgroup/cas_latency` and `cas_batch` paths verify as IDs 12112 and
12163, not 12302 and 12353.

An unprivileged GDB attempt with the failed run's IDs and shares did not
reproduce the segmentation fault: libbpf failed its BPF loading probe with
`-EPERM` before the attach breakpoint. This confirms only that the current
process cannot load BPF. The mismatch between system libbpf 1.3 and private
libbpf 1.7, combined with the absent RPATH and sudo environment filtering, is
a concrete runtime-linkage hazard and a plausible explanation for the earlier
crash, but it is not confirmed as the root cause. A privileged debugger run
that reaches the same failing loader point is still required to establish that
cause. No runtime attach or performance result is recorded here.

The current process has no effective `CAP_BPF`/`CAP_SYS_ADMIN`, and cannot
write either existing target `cgroup.procs`. The non-attaching runner `check`
therefore stops with `Permission denied`. Do not interpret the successful
build-only readiness check as authorization or runtime readiness.

## 6. Linux 7.0.0-42 rebuild and crash investigation (2026-11-21)

The current host reports Ubuntu 24.04, kernel `7.0.0-42-generic`, x86_64,
Clang 18.1.3, bpftool 7.7.0, readable BTF, and sched_ext state `disabled`.
The approved reference headers are at `/tmp/scx-linux70-headers` at commit
`c2e41fa619c3184aa663f90d3eceaf7fd746e958`. Private libbpf 1.7.0 is in
`/tmp/container-aware-scheduler-deps/libbpflib64`; its matching development
headers are in `/tmp/container-aware-scheduler-deps/libbpf/usr/include`.

The private `libbpf.pc` in this environment reports version 1.7.0 but contains
`prefix=/usr`, `includedir=/usr/include`, and relative `libdir=lib64`.
Consequently, selecting it with only `PKG_CONFIG_PATH` can compile against
system libbpf 1.3 headers and link with an unintended library path. Use
command-scoped variable overrides instead of editing the dependency files:

```sh
PRIVATE_PREFIX=/tmp/container-aware-scheduler-deps/libbpflib64
PRIVATE_INCLUDE=/tmp/container-aware-scheduler-deps/libbpf/usr/include
SCX_ROOT=/tmp/scx-linux70-headers
BUILD_DIR=$(mktemp -d /tmp/cas-build-final.XXXXXX)
export PKG_CONFIG_PATH="$PRIVATE_PREFIX/pkgconfig"
export LD_LIBRARY_PATH="$PRIVATE_PREFIX${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
PKG_CONFIG="pkg-config --define-variable=libdir=$PRIVATE_PREFIX --define-variable=includedir=$PRIVATE_INCLUDE"

make check-deps CLANG=clang-18 SCX_ROOT="$SCX_ROOT" \
  VMLINUX_BTF=/sys/kernel/btf/vmlinux PKG_CONFIG="$PKG_CONFIG"
make all workloads CLANG=clang-18 SCX_ROOT="$SCX_ROOT" \
  VMLINUX_BTF=/sys/kernel/btf/vmlinux PKG_CONFIG="$PKG_CONFIG" \
  BUILD_DIR="$BUILD_DIR"
```

The `group_scheduler` executable has no embedded RPATH/RUNPATH. When launching
it as root, `sudo` may filter the caller's `LD_LIBRARY_PATH`. The demo wrapper
therefore accepts `--libbpf-libdir` (or `CAS_LIBBPF_LIBDIR`), exports that path
inside the root process, and checks `ldd` resolves the loader to that directory.
Do not treat a successful `ldd` check as evidence of scheduler attachment.

The preserved run record
`results/raw/custom_scheduler_run_20261121T074006Z_2666ab5cb7_e7c9nmxv.json`
reports `scheduler_attached=false`, `measurement_status=incomplete`, loader
status `-11`, and an empty scheduler log. Its group paths were temporary
`cas_demo_*` directories that no longer exist. The currently named
`/sys/fs/cgroup/cas_latency` and `cas_batch` paths instead verify as IDs 12112
and 12163; do not reuse IDs 12302/12353 for those different paths.

An unprivileged GDB attempt with IDs 12302/12353 did **not** reproduce the
segmentation fault: libbpf failed its BPF loading probe with `-EPERM` before the
attach breakpoint. This proves only that the current unprivileged process
cannot load the object; it neither confirms nor rules out the cause of the
prior privileged `-11`. The libbpf header/runtime mismatch and `sudo`
environment filtering are concrete hazards and plausible causes, but the
segfault's root cause remains unconfirmed until a privileged, non-attaching
debug session can reach the same failing loader point. Do not claim a runtime
fix from the successful rebuild alone.

The current shell user has no effective `CAP_BPF`/`CAP_SYS_ADMIN`, and cannot
write either existing `cgroup.procs`. The non-attaching experiment `check`
therefore stops with `Permission denied`. The separate preserved successful
run was obtained under authorized host access; repeated weighted allocation
and performance measurements are not established here.