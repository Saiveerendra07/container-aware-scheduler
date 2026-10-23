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
- Bash and standard Linux tools. No sched_ext kernel support or build is needed.

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
reports the chosen controls and successful cleanup. Interrupt with Ctrl-C to
stop both children and trigger cgroup cleanup. Check the exit status; a failed
worker or cleanup produces a nonzero result.

## Relationship to the custom scheduler and limitations

The custom loader in `build/group_scheduler` is a separate, explicit action
described in [scheduler design](scheduler-design.md). This experiment script
never invokes it. A custom sched_ext evaluation requires a compatible kernel,
privileged explicit operator action, and a separate procedure for obtaining
the numeric cgroup IDs and preserving fixed membership during the run.

`cpu.weight` is a relative share among runnable sibling cgroups and is not a
reservation. `cpu.max` imposes a bandwidth cap and can throttle a group; it is
not a relative-share setting. Results depend on parent limits, CPU topology,
other system load, kernel configuration, and worker duration. This initial
runner prints worker output but does not collect CPU-time, throttling, latency,
fairness, or scheduler-overhead metrics and does not create a result CSV. Do
not infer these measures from iteration counts. Record the host, kernel,
delegated parent, control values, and competing load when performing formal
evaluation. The institute-PC evaluation remains future work.
