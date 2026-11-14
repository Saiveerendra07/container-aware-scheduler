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

No custom-scheduler runtime success or custom-scheduler performance result has been established.

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