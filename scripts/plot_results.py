#!/usr/bin/env python3
"""Plot measurements from CSV summaries written by analyze_results.py."""

import argparse
import csv
import glob
import math
import os
import sys
import uuid


REQUIRED_COLUMNS = {
    "run_id", "worker_id", "group", "cpu_usage_pct",
    "jain_fairness_index", "fairness_workers",
}


class PlotDependencyError(RuntimeError):
    pass


def parse_number(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def read_summaries(paths):
    """Read analyzer CSVs; return usable records and nonfatal warnings."""
    records = []
    warnings = []
    for path in paths:
        try:
            with open(path, newline="", encoding="utf-8-sig") as stream:
                reader = csv.DictReader(stream)
                if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
                    warnings.append(f"{path}: not an analyzer summary CSV; skipped")
                    continue
                for line_no, row in enumerate(reader, start=2):
                    if None in row or any(value is None for value in row.values()):
                        warnings.append(f"{path}:{line_no}: malformed or incomplete row; skipped")
                        continue
                    row["_summary_file"] = os.path.basename(path)
                    row["_line"] = line_no
                    records.append(row)
        except (OSError, UnicodeError, csv.Error) as exc:
            warnings.append(f"{path}: could not read ({exc}); skipped")
    return records, warnings


def plot_data(records):
    """Extract only finite, nonnegative measurements represented in summaries."""
    cpu_rows = []
    fairness_by_run = {}
    warnings = []
    for row in records:
        run_id = row.get("run_id", "").strip()
        worker_id = row.get("worker_id", "").strip()
        group = row.get("group", "").strip() or "unspecified"
        if not run_id or not worker_id:
            warnings.append(f"{row['_summary_file']}:{row['_line']}: missing run or worker ID; skipped")
            continue
        usage = parse_number(row.get("cpu_usage_pct"))
        if usage is not None and usage >= 0:
            cpu_rows.append({"run_id": run_id, "worker_id": worker_id,
                             "group": group, "usage": usage})

        fairness = parse_number(row.get("jain_fairness_index"))
        try:
            worker_count = int(row.get("fairness_workers", ""))
        except ValueError:
            worker_count = 0
        if fairness is not None and 0 <= fairness <= 1 and worker_count >= 2:
            prior = fairness_by_run.get(run_id)
            if prior is None:
                fairness_by_run[run_id] = {"fairness": fairness, "workers": worker_count}
            elif not math.isclose(prior["fairness"], fairness, rel_tol=1e-7, abs_tol=1e-7):
                warnings.append(f"run {run_id}: inconsistent fairness values; keeping first value")
        elif fairness is not None and worker_count < 2:
            warnings.append(f"run {run_id}: fairness omitted (fewer than two valid worker measurements)")
    return cpu_rows, fairness_by_run, warnings


def unique_path(graphs_dir, stem):
    os.makedirs(graphs_dir, exist_ok=True)
    while True:
        candidate = os.path.join(graphs_dir, f"{stem}_{uuid.uuid4().hex[:12]}.png")
        if not os.path.exists(candidate):
            return candidate


def save_figures(cpu_rows, fairness_by_run, graphs_dir):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise PlotDependencyError(
            "Matplotlib is required to render figures. Install it in your Python "
            "environment to enable plotting; no packages were installed."
        ) from exc

    outputs = []
    if cpu_rows:
        ordered = sorted(cpu_rows, key=lambda row: (row["run_id"], row["group"], row["worker_id"]))
        height = max(5.5, len(ordered) * 0.28)
        fig, axis = plt.subplots(figsize=(12, height), constrained_layout=True)
        labels = [f"{row['run_id']}  |  {row['worker_id']} ({row['group']})" for row in ordered]
        groups = sorted({row["group"] for row in ordered})
        palette = plt.get_cmap("tab10")
        colors = [palette(groups.index(row["group"]) % 10) for row in ordered]
        axis.barh(range(len(ordered)), [row["usage"] for row in ordered], color=colors)
        axis.set_yticks(range(len(ordered)), labels)
        axis.invert_yaxis()
        axis.set_xlabel("CPU usage (% of one CPU over measured elapsed time)")
        axis.set_ylabel("Run identifier | worker (group)")
        axis.set_title("Per-worker CPU usage — default Linux scheduler experiments")
        axis.grid(axis="x", alpha=0.25)
        handles = [plt.Rectangle((0, 0), 1, 1, color=palette(i % 10), label=group)
                   for i, group in enumerate(groups)]
        axis.legend(handles=handles, title="Group", loc="best")
        fig.text(0.01, 0.005,
                 "Recorded by existing CPU workload runners. This does not compare against the custom scheduler.",
                 fontsize=8)
        path = unique_path(graphs_dir, "cpu_usage")
        fig.savefig(path, dpi=180, bbox_inches="tight")
        plt.close(fig)
        outputs.append(path)

    if fairness_by_run:
        ordered_runs = sorted(fairness_by_run.items())
        labels = [run_id for run_id, _ in ordered_runs]
        values = [data["fairness"] for _, data in ordered_runs]
        counts = [data["workers"] for _, data in ordered_runs]
        fig_width = max(9, len(labels) * 0.65)
        fig, axis = plt.subplots(figsize=(fig_width, 5.5), constrained_layout=True)
        x_values = list(range(len(labels)))
        axis.bar(x_values, values, color="#4472C4", width=0.7)
        axis.set_xticks(x_values, labels, rotation=45, ha="right")
        axis.set_ylim(0, 1.05)
        axis.set_ylabel("Jain fairness index (dimensionless)")
        axis.set_xlabel("Run identifier")
        axis.set_title("Per-run equality of worker CPU time — default Linux scheduler experiments")
        axis.grid(axis="y", alpha=0.25)
        for x, value, count in zip(x_values, values, counts):
            axis.annotate(f"n={count}", (x, value), xytext=(0, 4),
                          textcoords="offset points", ha="center", fontsize=8)
        fig.text(0.01, 0.005,
                 "Jain index measures equality among workers, not share-weighted fairness; no custom scheduler comparison.",
                 fontsize=8)
        path = unique_path(graphs_dir, "jain_fairness")
        fig.savefig(path, dpi=180, bbox_inches="tight")
        plt.close(fig)
        outputs.append(path)
    return outputs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", help="analyzer summary CSV file(s)")
    parser.add_argument("--graphs-dir", help="figure directory (default: repository graphs/)")
    args = parser.parse_args(argv)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = args.inputs or sorted(glob.glob(os.path.join(repo, "results", "processed", "*.csv")))
    if not paths:
        print("No processed CSV files found. Run scripts/analyze_results.py first.", file=sys.stderr)
        return 0

    records, read_warnings = read_summaries(paths)
    cpu_rows, fairness_by_run, data_warnings = plot_data(records)
    for warning in read_warnings + data_warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    if not cpu_rows and not fairness_by_run:
        print("No usable CPU-usage or fairness measurements found; no figures written.", file=sys.stderr)
        return 0

    graphs_dir = args.graphs_dir or os.path.join(repo, "graphs")
    try:
        outputs = save_figures(cpu_rows, fairness_by_run, graphs_dir)
    except PlotDependencyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if not cpu_rows:
        print("No usable per-worker CPU-usage measurements; CPU-usage graph skipped.")
    if not fairness_by_run:
        print("No runs with at least two valid CPU-time measurements; fairness graph skipped.")
    for output in outputs:
        print(f"Wrote graph: {output}")
    print("Input measurements describe existing default Linux scheduler runs; no custom scheduler comparison was made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
