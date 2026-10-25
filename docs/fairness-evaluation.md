# Weighted group fairness evaluation

`scripts/evaluate_fairness.py` compares measured CPU-time allocation with
configured relative shares. It uses only Python's standard library and reads
existing CSV results; it does not run workloads or attach a scheduler.

## Usage

From the repository root, evaluate existing raw cgroup measurements whose
`control_setting` contains `cpu.weight` values:

```sh
python3 scripts/evaluate_fairness.py results/raw/cgroup_competition_*.csv
python3 scripts/evaluate_fairness.py results/raw/cgroup_competition_*.csv \
  --output results/processed/fairness.csv
```

The first command writes the evaluation CSV to standard output. The second
writes a new file and refuses to overwrite an input or any existing path.
Multiple inputs are accepted; run IDs are evaluated separately within each
source file.

Group-competition rows can also be evaluated when the CSV explicitly records
one of `group_share`, `configured_share`, or `share` per row, or a
`group_shares` / `configured_group_shares` mapping such as
`latency=200;batch=100`. For example:

```sh
python3 scripts/evaluate_fairness.py results/raw/custom_group_run.csv \
  --output results/processed/custom_group_fairness.csv
```

The custom scheduler runner also writes `group_share` on each raw result row,
so completed custom scheduler CSVs can be evaluated directly with this tool.

The repository's existing group runner records `group_counts`, which are
worker counts, not configured shares. That format can still supply observed
group CPU percentages and Jain's equality index, but weighted expected shares
and allocation errors are reported as `NA`. The cgroup result file supports
weighted comparison for `cpu.weight`; `cpu.max` expresses quotas rather than
relative shares and is not converted into an expected allocation. The
analyzer's processed cgroup summary omits before/after snapshots, so pass the
raw cgroup CSV to this tool. The custom sched_ext runner records configured
shares and CPU-time measurements in the schema accepted here. Its `prepare`
output is excluded because it is not a run, and an incomplete `run` is excluded
because its measurement status is not complete. Use `scripts/analyze_results.py`
for the repository's group/cgroup competition schemas; use this evaluator for
share-weighted custom-scheduler allocation analysis.
With only per-row share columns, the evaluator can identify configured groups
that have no measurement only if the CSV also includes a complete
`group_shares` / `configured_group_shares` mapping. Otherwise the configured
group set is inferred from represented rows; use a complete mapping when a
missing whole group must be detected.

## Metrics and exclusions

For each run, valid per-group CPU times are summed. `observed_cpu_pct` is a
group's CPU time divided by the total CPU time of all eligible groups in that
run, multiplied by 100. For cgroup input, CPU time is recomputed from
`usage_usec_after - usage_usec_before`; the supplied delta column is not
trusted. Both snapshots must be valid unsigned integers and the after value
must be at least the before value.

`expected_cpu_pct` is the group's configured positive share divided by the sum
of configured shares for the run, multiplied by 100. Shares must be recorded
explicitly and must not exceed 10000. `allocation_error_pct_points` is signed
observed percentage minus expected percentage: positive means the group used a
larger fraction of measured CPU time than its configured share predicts;
negative means a smaller fraction. The absolute error column reports the
magnitude in percentage points. Allocation metrics are unavailable if a group
measurement or any required share is missing, invalid, or conflicting, so a
partial run is not silently renormalized.

`jain_fairness_index` is `(sum(group_cpu_time)^2) /
(group_count * sum(group_cpu_time^2))`, over eligible measured groups. It
measures equality: 1 means equal CPU time among groups, regardless of their
configured shares. It does not measure whether the configured weighted
allocation was met. Thus a perfect 2:1 allocation has allocation error zero
for shares 2:1 but a Jain index of 0.9. Jain's index is `NA` with fewer than
two eligible groups or when their CPU times are all zero.

Only rows with `measurement_status=complete` are eligible. Cgroup rows must
also have exit status zero, complete `cpu_stat_status`, and valid non-reset
usage snapshots. A group with any invalid or incomplete measurement is
excluded as a whole because its aggregate CPU time would be incomplete. Output
rows report excluded counts and reasons; weighted metrics are unavailable for
the entire run if any group is excluded. Jain's index may still be reported
for the remaining eligible groups, and `jain_group_count` makes that scope
explicit. Malformed input rows/files generate warnings on standard error.

## Test data

Unit tests create synthetic CSVs only inside temporary test directories. Their
run IDs and filenames use `synthetic_test_only`; they are never written to
`results/raw/` or mixed with experiment results. Synthetic tests validate
metric formulas and exclusion behavior, not scheduler performance.
