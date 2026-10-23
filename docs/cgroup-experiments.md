# Ordinary cgroup CPU experiments

`scripts/run_cgroup_competition.sh` runs two copies of `workloads/cpu_worker`
in separate, temporary cgroup-v2 groups. The kernel's currently active Linux
scheduler handles both workers. The runner sets either `cpu.weight` (relative
weight, 1..10000) or `cpu.max` (quota/period in microseconds). It does not
attach or configure this repository's sched_ext scheduler. These runs are
ordinary cgroup-controller experiments and must not be described as custom
scheduler results.

## Prerequisites and safety

- Linux with a cgroup-v2 mount and the CPU controller available.
- A delegated, writable parent cgroup where `cpu` is already enabled in
  `cgroup.subtree_control`. The script deliberately does not enable controllers;
  the parent administrator or institute setup must delegate this in advance.
- `workloads/cpu_worker` built with the command in
  [the baseline guide](cpu-baseline-runner.md).
- Bash, Python 3 (standard library only), and standard Linux tools. No
  sched_ext kernel support or build is needed.

No `sudo` is used. Do not point `--parent` at a cgroup containing important
workloads. The selected parent must allow creating children and writing
`cpu.weight` or `cpu.max`; availability is checked again after creation. The
script only makes per-run child cgroups and removes them after workers stop.
It does not change global CPU frequency, affinity, online CPU state, or other
system-wide settings. If cleanup cannot remove a cgroup, the script reports
its path so an administrator can investigate.

## Check and run

Check the host default cgroup root (or substitute an institute-provided
delegated parent):

```sh
bash scripts/run_cgroup_competition.sh --check
bash scripts/run_cgroup_competition.sh --check --parent "$HOME/my-delegated-cgroup"
```

The check reports cgroup-v2 availability, CPU controller delegation, and
write access. A successful check ends with `sched_ext: not checked or used`.
To inspect planned settings without making changes:

```sh
bash scripts/run_cgroup_competition.sh --dry-run --parent "$HOME/my-delegated-cgroup" \
  --weight-a 200 --weight-b 100 --duration 5
```

Run the default-scheduler experiment with relative weights:

```sh
bash scripts/run_cgroup_competition.sh --parent "$HOME/my-delegated-cgroup" \
  --weight-a 200 --weight-b 100 --duration 5
```

Or use CPU quotas with a 100 ms period (50 ms and 100 ms per period):

```sh
bash scripts/run_cgroup_competition.sh --parent "$HOME/my-delegated-cgroup" \
  --quota-a 50000/100000 --quota-b 100000/100000 --duration 5
```

Exactly one complete settings pair is required. Quota periods are limited to
1000..1000000 microseconds; use `max/PERIOD` for an unlimited quota. The two
workers start concurrently, print their ordinary worker output, and the script
reports the chosen controls, writes a unique CSV under `results/raw/`, and
removes the temporary groups. Interrupt with Ctrl-C to stop both workers and
trigger cgroup cleanup. Check the exit status; a failed worker or cleanup
produces a nonzero result. Existing result files are never overwritten.

## Measurements and CSV columns

The output has one row for group `A` and one for group `B`. Common columns are
`run_id`, `timestamp_utc`, `kernel_version`, `logical_cpu_count`,
`requested_duration_s`, `control_setting`, `group`, and `cgroup_path`.
`workload_elapsed_s` is measured around the worker invocation using Linux
`/proc/uptime` (centisecond resolution); it includes small launch and wait
overhead. `exit_status` is the worker's exit code where available. A numeric
status with `measurement_status=incomplete` indicates an unsuccessful or
interrupted run; missing timing/status values remain `NA`.

For each of `usage_usec`, `user_usec`, `system_usec`, `nr_periods`,
`nr_throttled`, and `throttled_usec`, the CSV has `<counter>_before`,
`<counter>_after`, and `<counter>_delta` columns. Values are read from each
group's `cpu.stat` immediately before and after the workload. Deltas are
computed only when both values are present, valid nonnegative integers, and
the after value is not lower than the before value. Otherwise the affected
value or delta is `NA`; no missing measurement is inferred.
`cpu_stat_status` is `complete` when all six deltas are available, `partial`
when some are available, and `unavailable` when none are available. The
captured counters are cgroup totals and can include small runner overhead in
addition to the worker. `usage_usec` is CPU runtime, not a percentage;
`nr_throttled` and `throttled_usec` report kernel throttling accounting when
available. They do not measure request latency or scheduler overhead.

As with any interval counters, other processes must not be placed in the
temporary test groups. Parent-level limits and unrelated system load can still
affect the experiment. The runner does not derive CPU usage percentages,
fairness, or policy effectiveness from these counters.

## Relationship to the custom scheduler and limitations

The custom loader in `build/group_scheduler` is a separate, explicit action
described in [scheduler design](scheduler-design.md). This experiment script
never invokes it. A custom sched_ext evaluation requires a compatible kernel,
privileged explicit operator action, and a separate procedure for obtaining
the numeric cgroup IDs and preserving fixed membership during the run.

`cpu.weight` is a relative share among runnable sibling cgroups and is not a
reservation. `cpu.max` imposes a bandwidth cap and can throttle a group; it is
not a relative-share setting. Results depend on parent limits, CPU topology,
other system load, kernel configuration, and worker duration. The CSV records
cgroup CPU-time and throttling counters where available, but it does not
measure latency, fairness, or scheduler overhead. Do not infer these measures
from iteration counts. Record the host, kernel, delegated parent, control
values, and competing load when performing formal evaluation. The
institute-PC evaluation remains future work.
