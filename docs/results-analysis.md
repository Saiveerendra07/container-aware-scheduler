# Results analysis

Analyze recorded group-competition CSV files with the Python 3 standard
library; no packages are required. From the repository root:

```sh
python3 scripts/analyze_results.py
python3 scripts/analyze_results.py results/raw/group_competition_*.csv
python3 scripts/analyze_results.py --output results/processed/group_summary.csv
python3 scripts/analyze_results.py results/raw/some_run.csv -o results/processed/some_run_summary.csv
```

By default, the script reads `results/raw/group_competition_*.csv` and excludes
the documented incomplete debugging artifact
`group_competition_20261019T083552Z_LUDraY.csv`. Explicit input paths are
processed as given. Raw files are only read; an optional output is a separate
CSV in `results/processed/`.

The analyzer uses the runner's `cpu_time_s` and `elapsed_s` columns. Per-worker
CPU usage is `cpu_time_s / elapsed_s * 100`; missing, invalid, negative, or
zero elapsed measurements produce `NA`. This is the direct worker's average
CPU use over its measured elapsed interval, not total machine utilization.
Values slightly over 100% can occur due to differences in CPU accounting and
elapsed-time measurements.

Jain's fairness index is calculated from available per-worker CPU-time values
within a run: `(sum(cpu_time)^2) / (worker_count * sum(cpu_time^2))`. A value
of 1 means workers received equal CPU time; values approach 0 as allocation
becomes more unequal. It measures equality among workers, not weighted/share
fairness between groups, latency, or scheduler quality. Incomplete rows with
usable CPU time can contribute; the summary reports how many measurements
were included. If there are no valid CPU-time values or all are zero, the
index is unavailable.

Malformed files and rows are skipped with warnings. Missing or malformed
measurement fields remain unavailable and do not become invented values.

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

The script can draw a per-worker CPU-usage chart and a per-run Jain index chart
when those measurements are available. It skips missing or malformed values,
and omits the fairness graph when a run has fewer than two measured workers.
Figures are saved under `graphs/` with unique names; existing figures are not
overwritten. It requires Matplotlib for rendering. If Matplotlib is unavailable,
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
# Analyze the default results/raw group-competition CSV set and plot it
bash scripts/generate_experiment_report.sh

# Analyze selected raw CSVs instead
bash scripts/generate_experiment_report.sh results/raw/run_a.csv results/raw/run_b.csv

# Choose a different figures directory
bash scripts/generate_experiment_report.sh \
  --graphs-dir graphs results/raw/run_a.csv
```

Each invocation creates a new directory under `results/processed/` containing
`analyzed_results.csv`; prior summaries, raw results, and figures are kept.
The script passes that exact summary file to `plot_results.py`. If there are no
usable measurements, the plotter says no figures were written. If measurements
are available but Matplotlib is missing, the plotter reports that dependency
and the wrapper exits with its error status while preserving the summary.
The wrapper does not install dependencies. The analyzer and plotter retain
their existing behavior, including the analyzer's documented exclusion of the
incomplete debugging artifact when using its default input glob.
