# Scheduler Dashboard

The dashboard is a read-only Streamlit view of experiment records. It does not
run workloads, attach the custom scheduler, write result files, or change
cgroups.

From the repository root, install its dependencies and launch it:

```sh
python3 -m pip install -r requirements-dashboard.txt
streamlit run dashboard/app.py
```

Open [http://localhost:8501](http://localhost:8501). The default data source is
the repository's `results/` directory. Use the sidebar to select a different
results directory. Paths marked `test`, `tests`, `fixture`, `fixtures`, or
`synthetic` are excluded from project metrics by default; they can be included
for inspection with the sidebar checkbox.

The dashboard reads group-competition and cgroup-competition CSVs, custom
scheduler CSVs with confirmed attachment and configuration metadata, custom
scheduler JSON manifests, and the fixed text formats emitted by the baseline
and CPU-competition runners. It only displays metrics present in, or directly
derivable from, valid measurements. Baseline text records provide iterations
but not measured CPU time; competition throughput uses recorded iterations and
the runner's monotonic total elapsed time. Cgroup CPU time is derived from
`cpu.stat` usage-counter deltas. Group-runner `group_counts` are not treated as
CPU weights.

The custom sched_ext implementation has not been runtime-validated. Ordinary
and cgroup measurements use the active default Linux scheduler; prepared
manifests are listed as prepared, not run. Scheduler status is read from the
local `/sys/kernel/sched_ext/state` interface. An enabled state is reported as
running only when `/sys/kernel/sched_ext/root/ops` identifies this project's
`grp_cpu` scheduler; another active scheduler is reported as not running, and
unreadable identity is reported as unknown.

If the results directory is missing or contains no real experiment data, the
dashboard reports that state and does not synthesize measurements.