# Custom scheduler experiments

`scripts/run_custom_scheduler_experiment.py` provides explicit `prepare`,
`check`, and `run` modes. `prepare` and `check` never attach the scheduler or
start workloads. `run` is the only mode that may do either, and must be
explicitly requested. All modes validate at least two configured groups,
unique IDs and paths, shares in 1..10000, fallback share, and the exact cgroup
IDs described below. Generated paths are unique and existing raw results are
never overwritten.

First ask the tool to read the ID for each cgroup directory:

```sh
python3 scripts/prepare_custom_scheduler_experiment.py \
  --identify-cgroup /sys/fs/cgroup/test/latency
python3 scripts/prepare_custom_scheduler_experiment.py \
  --identify-cgroup /sys/fs/cgroup/test/batch
```

It prints JSON with the resolved path, kernel ID, cgroup2 mount ID, and
verification method. Copy each returned ID into the experiment command.
Prepare a manifest and `not_run_prepared` CSV without starting anything. The
preparation utility accepts 2 to 8 configured groups, matching the loader, and
validates each share and cgroup ID against the same integer ranges used by the
run path:

```sh
python3 scripts/run_custom_scheduler_experiment.py prepare --duration 10 \
  --fallback-share 100 \
  --group latency:12345:100:/sys/fs/cgroup/test/latency \
  --group batch:12346:300:/sys/fs/cgroup/test/batch
```

Replace the illustrative IDs with the values returned by
`--identify-cgroup`. Preparation rechecks every supplied ID against its path.
The manifest records exact cgroup paths and verified IDs, identity method and
status, configured scheduler shares, fallback share, worker executable and
duration, read-only cgroup `cpu.weight`/`cpu.max`, and expected output paths.
`cpu.weight` and quota (`cpu.max`) are not substituted
for scheduler shares. Prepared CSV rows have `measurement_status=not_run_prepared`
and elapsed time, CPU time, and exit status `NA`.

## Check mode

Before arranging the build, run the read-only prerequisite diagnostic:

```sh
python3 scripts/check_scheduler_readiness.py
```

Pass `--scx-root PATH` when the Makefile's reference sched_ext checkout is not
at `~/scx-c-examples`. The diagnostic does not build, install, alter kernel
state, create cgroups, attach the scheduler, or run workloads. A passing result
only confirms that listed prerequisites were detected; it does not prove
kernel or scheduler compatibility.

Build the loader/BPF object and workload binaries, then run non-attaching checks:

```sh
make
make workloads
python3 scripts/run_custom_scheduler_experiment.py check --duration 10 \
  --fallback-share 100 \
  --group latency:12345:100:/sys/fs/cgroup/test/latency \
  --group batch:12346:300:/sys/fs/cgroup/test/batch
```

Check validates loader/object/worker/supervisor files, readable kernel BTF,
the sched_ext state interface (which must report `disabled`), effective
`CAP_BPF` or `CAP_SYS_ADMIN`, writable target `cgroup.procs` files, cgroup IDs,
and share configuration. It opens `cgroup.procs` for write but does not write
to it. These checks cannot predict BPF verifier, LSM, kernel configuration, or
attach-time failures. In particular, an effective capability bit is only a
necessary preflight indicator, not proof that attachment will be accepted.

## Explicit run mode

Only invoke `run` when prepared delegated cgroups, recovery procedures, and a
compatible sched_ext host are available. The command rechecks prerequisites
immediately before attachment:

```sh
python3 scripts/run_custom_scheduler_experiment.py run --duration 10 \
  --fallback-share 100 \
  --group latency:12345:100:/sys/fs/cgroup/test/latency \
  --group batch:12346:300:/sys/fs/cgroup/test/batch
```

The loader prints `GROUP_SCHEDULER_ATTACHED` only after
`bpf_map__attach_struct_ops()` succeeds. Without that exact readiness line the
runner starts no workload, records an unsuccessful run with unavailable
measurements, and does not fall back to normal Linux scheduling. After
attachment, the runner launches one existing cgroup-aware supervisor per
group. Each supervisor forks a blocked child, moves that child into the
configured cgroup, reads `cgroup.procs` back to verify membership, and only
then releases it to exec the worker. A failed placement does not start the
worker. CPU and elapsed times come from the supervisor's `wait4` and monotonic
clock metadata; completion requires successful exec, exit status zero, no
signal, and valid measurements. Incomplete rows retain `NA` values and errors.

On normal completion or handled SIGINT/SIGTERM, the runner first waits for
supervisors to finish their worker cleanup, then sends SIGTERM to the loader.
The loader handles that signal by destroying its BPF link and exits; the runner
waits for that exit before reporting. If loader teardown stalls, the runner
continues waiting rather than intentionally exiting while the loader still
owns the scheduler link. No userspace runner can guarantee recovery from a
kernel task stuck in an uninterruptible state, kernel failure, or external
termination (`SIGKILL`/power loss); arrange an out-of-band recovery path and do
not use this on an untested production host.

The loader checks `bpf_link__destroy()` and returns failure if libbpf reports
a detach error. The runner then marks the result incomplete. A failing detach
status is surfaced with the loader's exit code in the manifest; it is not
reported as a successful shutdown. Loader readiness is read from a fresh pipe
for every invocation, including partial output, and each run reserves new CSV,
manifest, and log paths so stale readiness or prior results cannot be reused.

If the loader exits unexpectedly after attachment, the runner stops the
supervisors and marks every group measurement incomplete so the fairness
evaluator excludes the run. The kernel may already have moved scheduling to
its fallback policy between a scheduler failure and userspace detection; such
a run is explicitly invalid and cannot be interpreted as custom-scheduler
allocation evidence.

The CSV columns `group_share` and `fallback_share` are custom scheduler
configuration. `cgroup_cpu_weight` and `cgroup_cpu_max` are recorded separately
and are never applied by this runner. `scripts/evaluate_fairness.py` accepts
completed rows directly; prepared/incomplete rows are excluded:

```sh
python3 scripts/evaluate_fairness.py results/raw/custom_scheduler_run.csv \
  --output results/analysis/custom_scheduler_fairness.csv
```

Placement requires cgroup v2, a delegated target cgroup, and permission to
write its `cgroup.procs`. The harness never enables controllers or changes
cgroup controls. The old supervisor invocation remains backward-compatible.
Unit tests use controlled fake loader/supervisor processes and synthetic inputs.
They check process cleanup, result invalidation, unique outputs, and attach
timeouts, but do not validate real kernel cgroup migration, sched_ext
attachment, weighted runtime behavior, or teardown.
No runtime success is claimed until a supported sched_ext host has run the
explicit command above and retained the raw output.

## Cgroup identity verification

The BPF scheduler obtains the task's cgroup using
`__COMPAT_scx_bpf_task_cgroup()` and compares `cgrp->kn->id` with the configured
ID in [group_scheduler.bpf.c](../src/group_scheduler.bpf.c). The preparation
tool does not infer this value from `stat().st_ino`. It calls Linux
`name_to_handle_at(2)` on an open directory descriptor. For cgroup2 kernfs,
the kernel's `FILEID_KERNFS` file handle contains the exact 64-bit
`kernfs_node->id`; the tool accepts only that handle type and expected
eight-byte ID. It also matches the syscall's mount ID against
`/proc/self/mountinfo` and requires that mount's filesystem type to be
`cgroup2`.

This uses the kernel's file-handle interface without loading BPF, attaching a
scheduler, changing cgroups, or requiring elevated permissions. Preparation
fails closed if `name_to_handle_at(2)` is unavailable, the kernel/filesystem
does not export a `FILEID_KERNFS` handle, the mount ID cannot be resolved to a
unique `cgroup2` entry, or the supplied ID differs from the handle ID. The
metadata and CSV record the verified ID, status, method, mount ID, and
filesystem. No row is marked verified when the check fails. Kernel versions
or namespace/mount arrangements that do not expose this interface need a
separately verified ID provider; there is no inode-number fallback.

The tests use mocked file-handle responses and synthetic mountinfo in
temporary directories for positive, mismatch, and unavailable-interface
cases. They do not emulate the kernel implementation. The preparation path
was also read-only checked against the current host's real cgroup2 mount root;
this does not validate other hosts, cgroups, kernels, or scheduler runtime.

The tests use synthetic inputs in temporary directories with
`synthetic_test_only` labels; they are not actual scheduler results.
