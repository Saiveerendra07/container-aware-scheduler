# Group competition runner

The runner starts the existing CPU worker as one process per requested worker.
Build the worker from the repository root if needed:

```sh
gcc -O2 -Wall -Wextra -std=c11 workloads/cpu_worker.c -o workloads/cpu_worker
```

The runner builds `workloads/cpu_supervisor.c` with GCC using `-Wall -Wextra
-Werror` for each run. The generated helper is stored temporarily under
`results/raw/` and removed after the run. GCC is therefore a runtime dependency
of this runner, in addition to Bash, awk, and standard Linux `/proc` support.

Run from the repository root or invoke the script by path from another
directory. Give one or more group counts followed by a positive duration in
seconds:

```sh
scripts/run_group_competition.sh latency=2 batch=2 background=1 3
```

Accepted group names are `latency`, `batch`, and `background`. A group may be
specified once; each count must be an integer from 1 through 32. Duration must
be a positive number. Invalid, duplicate, unknown, or extra arguments are
rejected before workers start.

Group names are experiment labels only. The runner does not enforce CPU
shares, priorities, quotas, cgroups, or any scheduler policy. All processes
are scheduled by the active Linux scheduler.

Each invocation creates a unique CSV file and unique per-worker output logs in
`results/raw/`; existing records are not overwritten. The CSV has one row per
requested worker and records run metadata, group and worker identifiers,
supervisor and worker PIDs where available, requested duration, user and system
CPU seconds, their sum as total CPU time, monotonic elapsed seconds, the
worker's printed iteration count when present, exit status, termination signal,
measurement status, and output-log path. Missing measurements or iteration
output are recorded as `NA`; an iteration count is never inferred.

The C supervisor starts the unchanged worker and collects its final user and
system CPU time from `wait4()` resource usage. Elapsed time is measured with
`CLOCK_MONOTONIC`; it includes worker startup and the interval through child
reaping. CPU time is processor time consumed by the worker, not CPU usage
percentage. This experiment does not calculate fairness, request latency,
throttling, or scheduler overhead. The result is specific to the machine,
kernel, competing load, and run conditions.

Each supervisor places the worker in a separate process group whose PGID is
the worker PID. The worker creates that group before `exec`; the supervisor
also attempts `setpgid` in the parent to close the startup race. The runner and
supervisors stay in their original process group. A supervisor signals only
the negative worker PGID, so cleanup cannot target the runner or a peer
worker's group. The runner requests peer cancellation through a unique
per-run control file, then waits for every supervisor; it does not signal
possibly stale supervisor PIDs. The control file, helper executable, and
metadata files are removed after the supervisors have been waited for.

On runner SIGINT/SIGTERM, worker failure, or supervisor termination, the
supervisor sends SIGTERM to the worker process group and escalates to SIGKILL
after at most two seconds if live group members remain. If the worker exits
while descendants remain in its group, they receive the same bounded
termination sequence before the worker is reaped. The supervisor directly
reaps only its worker child; it cannot reap arbitrary descendants that have
been reparented, and a descendant that escapes the process group (for example
with `setsid` or by changing its process group) is outside this cleanup
mechanism. Process-group membership is checked through Linux `/proc`. A
process disappearing between directory enumeration and opening its `stat`
file is treated as a transient race. Other open, read, parse, or directory
scan errors make the scan incomplete: the supervisor records an error, sends
SIGKILL to the group, and reaps its direct worker, but does not claim that
descendant cleanup was verified. Worker CPU usage in `wait4()` covers the
direct worker child, not descendant processes.

A nonzero worker or supervisor status makes the runner return nonzero;
SIGINT and SIGTERM return 130 and 143.
The supervisor reports worker exit status when it exits normally, or its
termination signal when signal-terminated. Supervisor setup errors use status
125; exec failures use 126 or 127. A normal worker exit is recorded as its
exit status; a signal-terminated worker has `NA` for exit status and its signal
number in `termination_signal`. Worker exit status 125 is still classified as
a worker failure: the metadata error fields distinguish it from supervisor
status 125.

The file
`results/raw/group_competition_20261019T083552Z_LUDraY.csv` and its companion
`group_competition_20261019T083552Z_LUDraY_w*.log` files are an incomplete
debugging artifact from development. Do not treat that CSV row or its logs as
experiment data or include them in analysis. The files are retained unchanged.

Reproducible checks from the repository root:

```sh
bash -n scripts/run_group_competition.sh
gcc -O2 -Wall -Wextra -Werror -std=c11 workloads/cpu_supervisor.c -o /tmp/cpu_supervisor
scripts/run_group_competition.sh latency=2 batch=2 2
scripts/run_group_competition.sh latency=0 2  # rejected before launch
scripts/run_group_competition.sh latency=1 latency=2 2  # duplicate rejected
scripts/run_group_competition.sh unknown=1 2  # unknown group rejected
scripts/run_group_competition.sh latency=x 2  # malformed count rejected
scripts/run_group_competition.sh latency=33 2  # excessive count rejected
scripts/run_group_competition.sh latency=1 0  # invalid duration rejected
scripts/run_group_competition.sh latency=1 2 extra  # extra argument rejected
```

Temporary process-control and metadata files are cleaned up after each run;
result CSVs and worker logs remain in `results/raw/`. The runner reports a
unique result path for each invocation and does not overwrite prior results.
INT and TERM remain handled during shell cleanup, so repeated signals request
cancellation without interrupting supervisor polling or result finalization.
The supervisors escalate worker termination after two seconds; a kernel task
that cannot exit, such as one stuck in uninterruptible sleep, can still delay
the runner's wait for that supervisor.
The supervisor's exec-status pipe is private to its fork/exec sequence, so a
read error on that pipe cannot be injected through the runner without adding a
test hook. That error path calls the same bounded worker-group termination and
reaping routine used by signal and cancellation handling; the regression
checks exercise that shared routine, but do not simulate a corrupted pipe.
