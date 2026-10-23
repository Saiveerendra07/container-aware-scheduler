# Container-aware group CPU scheduler: first increment

## Overview

The initial scheduler uses Linux `sched_ext` struct operations with a BPF
scheduling policy and a separate libbpf userspace loader. It recognizes up to
eight configured groups by their exact cgroup IDs and adds one explicit
fallback group. Every group receives a static positive integer share supplied
to the loader.

The BPF side creates a FIFO dispatch queue for each configured group and one
for the fallback group. Each has a shared virtual-time counter. At each
dispatch, the scheduler selects a nonempty group with the lowest current
virtual time and moves one task from that group's queue to the local dispatch
queue. When a group queue becomes active after being empty, its virtual time is
rebased to the minimum virtual time of groups that are queued or running. This
prevents an idle group's stale virtual time from giving it an accumulated
advantage or penalty when it wakes. Running-group counts keep groups active
while their tasks are in a CPU-local queue. Tasks whose exact cgroup ID is not
configured, or whose cgroup cannot be looked up, use the fallback queue;
ordinary tasks handled by this scheduler do not bypass group accounting
through `SCX_DSQ_GLOBAL`.

When a task stops running, the scheduler charges the difference in the
kernel-maintained `p->se.sum_exec_runtime` value since its `running` callback,
scaled by `total group shares / group share`. This is actual accumulated task
CPU runtime rather than an estimate derived from the remaining sched_ext
slice. The local sched_ext examples use this field for runtime accounting
(`scx_flatcg` and `scx_userland`).

This is a first-step weighted scheduler, not a complete CPU controller. Shares
are not CPU reservations and do not account for ancestor cgroup limits, CPU
affinity, or other scheduling classes. Group lookup matches the task's exact
cgroup ID, not an ancestor ID. Group membership must remain unchanged for the
entire scheduler run; migration-aware accounting is not implemented.

## Configuration

Pass `--fallback-share SHARE` and two to eight
`--group CGROUP_ID:SHARE` options to the loader. Cgroup IDs must be unique and
nonzero. All shares, including the fallback share, must be integers from 1
through 10000. Shares are relative: for example, configured shares 200 and
100 target approximately a 2:1 CPU-time ratio while both groups remain
runnable, subject to the fallback group's activity and the limitations below.

The loader rejects malformed values, duplicate IDs, invalid shares, too many
groups, a missing or repeated fallback share, and fewer than two configured
groups before loading BPF. The cgroup IDs must be obtained from the target
system; `/proc/<pid>/cgroup` reports paths, not numeric cgroup IDs. All tasks
not matching a configured ID participate as one fallback group, so unrelated
system tasks may consume part of that group's share.

## Build

The build requires Clang with BPF target support, `bpftool`, GCC or another
compatible C compiler, `pkg-config`, libbpf development files, and readable
kernel BTF at `/sys/kernel/btf/vmlinux`. It also uses the `scx/common.h` and
`scx/common.bpf.h` compatibility headers from a sched_ext source tree. By
default, `SCX_ROOT` points to `~/scx-c-examples`; override it if that source
tree is elsewhere. The libbpf development version must be 1.5 or newer to
match the generated skeleton interface:

```sh
make
make SCX_ROOT=/path/to/scx-c-examples
```

The build generates `build/vmlinux.h`, the BPF object, the libbpf skeleton,
and the loader under `build/`. These are generated build products; do not edit
them. Remove them with `make clean`.

The task-cgroup lookup used by the BPF code is available through the
compatibility header on kernels that provide the sched_ext cgroup kfunc
(Linux 6.12 and later). If that kfunc is unavailable, tasks are treated as
unconfigured and assigned to the fallback group. The scheduler has not been
validated on every kernel, architecture, or cgroup configuration.

## Safety and current limitations

The loader checks configuration before opening the scheduler, reports load or
attach errors, and requests clean shutdown on SIGINT or SIGTERM. The scheduler
is not automatically loaded by the build. Attaching a sched_ext scheduler
requires suitable privileges and a compatible kernel; test only on an
appropriate system with a recovery path.

Before starting the scheduler for a test, create the test cgroups and place
each test workload in its intended cgroup. Do not move tasks into, out of, or
between configured cgroups while the scheduler is attached; stop the scheduler
before changing membership. Keep other workloads out of the configured test
cgroups. Tasks outside those exact IDs use the configured fallback group, so
choose its share deliberately and keep unrelated system load low for
comparative experiments. This procedure treats group membership as immutable
for each run; it does not provide migration-aware accounting.

The policy approximates weighted group fairness; it does not promise a strict
CPU-time ratio. Concurrent CPUs can make dispatch choices before a group's
newly running tasks have been charged, and queue/running state can change while
an idle-group virtual-time rebase is being computed. Tasks within each group
use FIFO order rather than per-task weighted fairness. The fallback group
combines all unmatched tasks rather than providing per-cgroup fairness. There is no
hierarchy-aware share aggregation, dynamic share update, runtime statistics
interface, CPU topology policy, or guarantee of a particular fraction when
groups are idle or constrained elsewhere.
