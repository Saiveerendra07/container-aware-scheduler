# CPU baseline runner

Build the CPU worker once from the repository root:

```sh
gcc -O2 -Wall -Wextra -std=c11 workloads/cpu_worker.c -o workloads/cpu_worker
```

Run a three-second baseline from the repository root or any other working
directory:

```sh
scripts/run_cpu_baseline.sh
scripts/run_cpu_baseline.sh 3
```

The duration is in seconds and must be a positive number. Each run creates a
new timestamped text file under `results/raw/`. The file records the date and
time, kernel version, logical CPU count, duration, command, combined workload
stdout and stderr, and workload exit status. The script prints the result
file's path when it finishes. It does not configure cgroups or schedulers.
