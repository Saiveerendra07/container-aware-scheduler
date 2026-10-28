# Results analysis

Analyze existing group-competition and cgroup-competition CSV files with the
Python 3 standard library; no packages are required. From the repository root:

```sh
python3 scripts/analyze_results.py
python3 scripts/analyze_results.py results/raw/group_competition_*.csv
python3 scripts/analyze_results.py results/raw/cgroup_competition_*.csv
python3 scripts/analyze_results.py results/raw/group_run.csv results/raw/cgroup_run.csv \
  --output results/processed/combined_summary.csv
```

By default, the script reads both `results/raw/group_competition_*.csv` and
`results/raw/cgroup_competition_*.csv`, excluding the documented incomplete
group debugging artifact `group_competition_20261019T083552Z_LUDraY.csv`.
Explicit input paths are processed as given. Raw files are read only. The
optional `--output` writes a derived CSV; if that path already exists, a unique
suffix is added so an earlier summary is preserved. Group and cgroup rows are
tagged with `result_type`; cgroup runs are ordinary default Linux scheduler
experiments and are never labeled as custom sched_ext results.

The analyzer uses the runner's `cpu_time_s` and `elapsed_s` columns. Per-worker
CPU usage is `cpu_time_s / elapsed_s * 100`; missing, invalid, negative, or
zero elapsed measurements produce `NA`. This is the direct worker's average
CPU use over its measured elapsed interval, not total machine utilization.
Values slightly over 100% can occur due to differences in CPU accounting and
elapsed-time measurements.

Jain's fairness index is calculated from per-worker CPU-time values within a
run: `(sum(cpu_time)^2) / (worker_count * sum(cpu_time^2))`. A value of 1
means workers received equal CPU time; values approach 0 as allocation becomes
more unequal. It measures equality among workers, not weighted/share fairness
between groups, latency, or scheduler quality. Derived group-run metrics
require every represented worker row to be complete and successful; when any
row is incomplete, failed, or missing relative to recorded `group_counts`,
CPU usage and Jain fairness for the entire run are unavailable.

This per-worker Jain index measures equality and is not a comparison against
configured group weights. Use [`fairness-evaluation.md`](fairness-evaluation.md)
and `scripts/evaluate_fairness.py` for weighted group allocation when the raw
CSV records explicit shares. Existing group-runner `group_counts` are worker
counts and are not treated as shares.

Malformed files and rows are skipped with warnings. Missing or malformed
measurement fields remain unavailable and do not become invented values.

For cgroup rows, the analyzer recomputes each counter delta from the raw
`<counter>_before` and `<counter>_after` values. A delta is available only
when both are unsigned integer counters and the after value is not lower.
It does not trust a supplied delta when either snapshot is unavailable or
invalid. `usage_usec_delta` is converted to CPU seconds. `cpu_usage_pct` is
`100 * usage_usec_delta / 1,000,000 / workload_elapsed_s`, when the elapsed time
is positive and both snapshots are valid. The cgroup elapsed interval is measured
around the worker invocation and includes small launch/wait overhead. The
accounting is cgroup-wide and can include runner overhead; it is not a direct
worker-only CPU measurement. `nr_throttled_delta` is the number of throttled
periods and `throttled_usec_delta` is converted to `throttled_time_s`.
`throttled_period_pct` is `100 * nr_throttled_delta / nr_periods_delta`, only
when `nr_periods_delta` is positive. Throttling counters are kernel accounting,
not latency measurements. A nonzero `exit_status` or
`measurement_status=incomplete` is retained even when counter snapshots are
valid. Raw snapshots and their valid deltas remain visible for diagnosis, but
derived CPU usage and throttling metrics are unavailable for the failed run.
For successful runs with partial counter availability, each metric remains
individually available only when its required counters are valid.
Missing/`NA`, malformed, negative, or reset-counter snapshots produce `NA`
deltas in the summary.

The derived cgroup columns are `cpu_time_s`, `elapsed_s`, `cpu_usage_pct`,
`nr_periods_delta`, `nr_throttled_delta`, `throttled_time_s`,
`throttled_period_pct`, `exit_status`, `measurement_status`, and
`cpu_stat_status`. Cgroup rows do not receive a Jain index because two cgroup
rows are not worker-level measurements of fairness.

## Repeated-run statistics

Use `scripts/summarize_repeated_runs.py` to summarize runs only when their
scheduling mode, kernel version, logical CPU count, architecture, requested
duration, group layout, workload identity, control settings, and configured
shares are compatible. It excludes runs whose environment metadata needed for
that comparison is missing; older CSVs without kernel/CPU/architecture metadata
therefore remain visible as exclusions instead of being pooled as if comparable.
By default it reads supported
group, cgroup, and custom-scheduler raw CSVs from `results/raw/`:

```sh
python3 scripts/summarize_repeated_runs.py
```

Pass selected files and a destination directory to keep the results together:

```sh
python3 scripts/summarize_repeated_runs.py \
  results/raw/group_competition_*.csv \
  results/raw/cgroup_competition_*.csv \
  results/raw/custom_scheduler_run_*.csv \
  --output-dir results/processed/repeated
```

Each invocation writes a unique `repeated_run_summary_*.csv` and matching
Markdown report. Existing raw inputs and prior outputs are not overwritten.
Runs with inconsistent row configuration, non-complete status, invalid CPU or
elapsed measurement, nonzero exit, incomplete cgroup counters, or an unconfirmed
custom-scheduler attach/shutdown are excluded and listed with reasons. Runs
from different compatible configurations are summarized separately rather
than pooled.

The CSV uses one row per configuration, group, and metric. CPU time is summed
across workers in a group for each run; elapsed time is the mean worker elapsed
time for that group; CPU usage is group CPU time divided by that elapsed time.
Cgroup throttled seconds are summed per group and throttled-period percentage
is recomputed from summed counter deltas. Weighted allocation error comes from
the fairness evaluator and is available only when every required group's CPU
measurement and configured share are valid. Jain's index is calculated from
per-run group CPU totals when at least two groups have measurements; it
measures equality, not whether weighted shares were met.

For each available per-run metric the report gives arithmetic mean, median,
sample standard deviation (`n-1` denominator), and sample count. A one-run
sample has a valid mean and median but `NA` standard deviation. Missing metrics
remain `NA`; no zero is substituted. These descriptive summaries do not test
statistical significance. Repeated runs alone do not ensure independent or
randomized trials, balanced order, controlled thermal/load conditions, or a
sufficient sample size.

## Plotting analyzed results

`scripts/plot_results.py` reads only CSV summaries produced by the analyzer.
No analyzed summaries are present by default until the analyzer is run. For
example:

```sh
python3 scripts/analyze_results.py \
  -o results/processed/group_summary.csv
python3 scripts/plot_results.py
```

Pass one or more summaries explicitly, or set a different figure directory:

```sh
python3 scripts/plot_results.py results/processed/group_summary.csv
python3 scripts/plot_results.py results/processed/group_summary.csv \
  --graphs-dir graphs
```

The script can draw per-worker CPU-usage and Jain-index charts for group-runner
summaries, plus separate cgroup CPU-usage and cgroup throttling charts when at
least one valid measurement for that chart exists. The throttling chart shows throttled-period
counts and throttled seconds where each is available. It skips missing or
malformed values, and omits the fairness graph when a run has fewer than two
measured workers. Cgroup graphs explicitly identify the default Linux
scheduler and do not compare against sched_ext. Figures use unique names under
`graphs/`; existing figures are not overwritten. It requires Matplotlib for rendering. If Matplotlib is unavailable,
the script reports the dependency and leaves the project and input files
unchanged.

The existing baseline and competition runs use the default Linux scheduler.
These plots do not claim a comparison with the custom scheduler. CPU usage is
per worker; Jain fairness represents equality among workers, not
share-weighted fairness or latency.

## Generate a report in one command

Use `scripts/generate_experiment_report.sh` to run the analyzer and then pass
its uniquely named summary CSV to the plotter. No workloads are launched:

```sh
# Analyze both default results/raw competition CSV sets and plot them
bash scripts/generate_experiment_report.sh

# Analyze selected group and cgroup raw CSVs together
bash scripts/generate_experiment_report.sh results/raw/group_run.csv \
  results/raw/cgroup_run.csv

# Choose a different figures directory
bash scripts/generate_experiment_report.sh \
  --graphs-dir graphs results/raw/run_a.csv
```

Each invocation creates a new directory under `results/processed/` containing
`analyzed_results.csv`; prior summaries, raw results, and figures are kept.
Add `--repeated-summary` to also save the repeated-run CSV and Markdown report
in that invocation's directory:

```sh
bash scripts/generate_experiment_report.sh --repeated-summary \
  results/raw/cgroup_run_a.csv results/raw/cgroup_run_b.csv
```

The script passes that exact summary file to `plot_results.py`. If there are no
usable measurements, the plotter says no figures were written. If measurements
are available but Matplotlib is missing, the plotter reports that dependency
and the wrapper exits with its error status while preserving the summary.
The wrapper does not install dependencies. The analyzer's default input glob
includes both supported formats and excludes the documented incomplete group
debugging artifact.
