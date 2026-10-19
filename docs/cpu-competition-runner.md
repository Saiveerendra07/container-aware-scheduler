# CPU competition runner

Build the existing worker from the repository root if needed:

```sh
gcc -O2 -Wall -Wextra -std=c11 workloads/cpu_worker.c -o workloads/cpu_worker
```

Run from the repository root, or invoke the script by its path from another
working directory:

```sh
scripts/run_cpu_competition.sh             # 2 workers, 3 seconds each
scripts/run_cpu_competition.sh 4 5         # 4 workers, 5 seconds each
```

The worker count must be an integer from 1 through 32. Duration must be a
positive number of seconds. Omitted arguments use the defaults; explicitly
empty or malformed arguments are rejected before workers start. Extra
arguments are rejected.

Each invocation creates a unique text file under `results/raw/`. It records
the timestamp, kernel version, logical CPU count, requested worker count and
duration, how many workers started, each worker PID, combined worker stdout
and stderr, each worker's exit status, and total elapsed time from a monotonic
clock. The script polls Bash's job table and waits for each worker after it
stops running. This keeps a signal to the runner from interrupting status
collection. On interruption, it sends termination to workers whose statuses
have not yet been collected, waits for them, and records their actual exit
statuses where available. If a worker fails, the runner terminates and reaps
unfinished peers. The runner returns nonzero if a worker fails or it is
interrupted.

Elapsed time is measured from Linux `/proc/uptime`, which provides a monotonic
uptime counter at centisecond resolution. This uses the proc filesystem
already available in Linux/WSL and adds no runtime dependency.

This runner compares competing CPU-bound processes only. It does not measure
CPU usage, request latency, fairness, throttling, or scheduler overhead, and it
does not configure cgroups or schedulers. Iteration counts can vary between
runs as the workers compete for CPU time.

Reproducible checks from the repository root:

```sh
bash -n scripts/run_cpu_competition.sh
scripts/run_cpu_competition.sh
scripts/run_cpu_competition.sh 2 2
scripts/run_cpu_competition.sh 0
scripts/run_cpu_competition.sh 2 -1
scripts/run_cpu_competition.sh two
scripts/run_cpu_competition.sh 33
scripts/run_cpu_competition.sh 2 2 extra
```
