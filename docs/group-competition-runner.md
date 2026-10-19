# Group competition runner

Build the existing CPU worker from the repository root if needed:

```sh
gcc -O2 -Wall -Wextra -std=c11 workloads/cpu_worker.c -o workloads/cpu_worker
```

Run the script from the repository root or invoke it by path from another
working directory. Provide one or more `group=count` definitions, followed by
the duration in seconds for every worker:

```sh
scripts/run_group_competition.sh latency=2 batch=2 background=1 3
```

Accepted group names are `latency`, `batch`, and `background`. Each name may
appear once, and each count must be an integer from 1 through 32. A run may
include one, two, or all three groups. Duration must be a positive number of
seconds. Missing arguments, duplicate or unknown group names, malformed
definitions, invalid counts or duration, and extra arguments are rejected
before workers are launched.

Group names are labels used to organize and report worker processes. They do
not set scheduling priority, CPU shares, quotas, cgroups, or any other CPU
control. All workers run through the normal Linux scheduler.

Each run creates a unique result file under `results/raw/`. It records the
timestamp, kernel version, logical CPU count, requested duration and group
counts, each worker's group, PID, combined stdout and stderr, exit status, and
total elapsed time from Linux `/proc/uptime` at centisecond resolution. A
failed worker causes unfinished peers to be terminated and reaped, and the
runner returns nonzero. SIGINT and SIGTERM also trigger cleanup and a nonzero
exit. The result records worker statuses where available.

This experiment compares competing CPU-bound processes. It does not measure
request latency, fairness, throttling, CPU usage, or scheduler overhead.
Iteration counts may vary between runs, and these groups do not imply any
scheduling policy.

Reproducible checks from the repository root:

```sh
bash -n scripts/run_group_competition.sh
scripts/run_group_competition.sh latency=2 batch=2 2
scripts/run_group_competition.sh latency=0 2
scripts/run_group_competition.sh latency=1 latency=2 2
scripts/run_group_competition.sh unknown=1 2
scripts/run_group_competition.sh latency=x 2
scripts/run_group_competition.sh latency=33 2
scripts/run_group_competition.sh latency=1 0
scripts/run_group_competition.sh latency=1 2 extra
```
