# Repeatable CPU experiment suite

`scripts/run_experiment_suite.sh` runs the existing baseline runner followed by
the existing CPU-competition runner for each requested repetition. It invokes
their actual interfaces: baseline accepts `[duration_seconds]`, while CPU
competition accepts `[worker_count [duration_seconds_per_worker]]`.

These runs use the active **default Linux scheduler**. The suite does not
configure cgroups, attach sched_ext, or test the custom group scheduler. It
does not require sched_ext, and it does not modify either existing runner.

## Commands

From the repository root:

```sh
# Safely preview defaults; no workload runs and no files are written
bash scripts/run_experiment_suite.sh --dry-run

# One baseline run (3 seconds) and competition with 4 workers (5 seconds each)
bash scripts/run_experiment_suite.sh \
  --baseline-duration 3 \
  --competition-workers 4 \
  --competition-duration 5

# Repeat each configuration three times
bash scripts/run_experiment_suite.sh \
  --baseline-duration 3 \
  --competition-workers 4 \
  --competition-duration 5 \
  --repetitions 3
```

The workload binary must already exist at `workloads/cpu_worker`; build it
using the command in [the baseline runner guide](cpu-baseline-runner.md).
Competition worker count is limited to 1 through 32. Durations must be
positive numbers of seconds. Repetitions are limited to 1 through 999.

## Outputs and analysis

Each non-dry suite creates a unique directory under `results/processed/` with
one captured runner log per experiment invocation and a `manifest.tsv`. The
manifest records the suite ID, scheduler label, repetition, command, exit
status, runner log, and raw result path reported by that runner. Existing
suite directories are not overwritten. The runners continue to store their
own uniquely named raw result files under `results/raw/`.

The baseline and CPU-competition runners produce text results. The current
`scripts/analyze_results.py` accepts group-competition CSV files only, so do
not pass these text files to it. Use that analyzer for compatible group-runner
CSV files; this suite intentionally does not combine incompatible formats.

The baseline text file records one worker's output and exit status. The
competition text file records each worker's output and status plus total
elapsed time. These results do not provide per-worker CPU-time measurements,
request latency, or scheduler fairness. `cpu_worker` is a CPU-bound synthetic
workload, and iteration counts vary with machine load. Comparisons should
record the environment and be repeated; they describe these workloads under
the default Linux scheduler only.
