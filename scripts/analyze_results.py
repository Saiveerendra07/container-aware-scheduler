#!/usr/bin/env python3
"""Analyze group-competition CSV results without changing raw files."""

import argparse
import csv
import glob
import math
import os
from collections import defaultdict


DEBUG_ARTIFACT = "group_competition_20261019T083552Z_LUDraY.csv"
REQUIRED_COLUMNS = {
    "run_id", "worker_id", "group", "cpu_time_s", "elapsed_s",
    "measurement_status",
}


def number(value):
    """Return a finite nonnegative float, or None for missing/bad data."""
    if value is None or not value.strip() or value.strip().upper() == "NA":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def jain_index(values):
    """Jain's index for nonnegative worker CPU-time values."""
    if not values:
        return None
    total = sum(values)
    squares = sum(value * value for value in values)
    if squares == 0:
        return None
    return total * total / (len(values) * squares)


def read_file(path):
    rows = []
    warnings = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
                return [], [f"{path}: missing required columns; skipped file"]
            for line_no, row in enumerate(reader, start=2):
                if None in row:
                    warnings.append(f"{path}:{line_no}: extra CSV fields; skipped row")
                    continue
                if any(value is None for value in row.values()):
                    warnings.append(f"{path}:{line_no}: incomplete CSV row; skipped row")
                    continue
                row["_source_file"] = os.path.basename(path)
                row["_line"] = str(line_no)
                rows.append(row)
    except (OSError, UnicodeError, csv.Error) as exc:
        warnings.append(f"{path}: could not read CSV ({exc}); skipped file")
    return rows, warnings


def analyze(paths):
    rows = []
    warnings = []
    for path in paths:
        parsed, file_warnings = read_file(path)
        rows.extend(parsed)
        warnings.extend(file_warnings)

    by_run = defaultdict(list)
    for row in rows:
        by_run[row.get("run_id", "")].append(row)

    for run_id, run_rows in by_run.items():
        valid_cpu = []
        for row in run_rows:
            cpu = number(row.get("cpu_time_s"))
            if cpu is not None:
                valid_cpu.append(cpu)
        fairness = jain_index(valid_cpu)
        for row in run_rows:
            cpu = number(row.get("cpu_time_s"))
            elapsed = number(row.get("elapsed_s"))
            usage = 100.0 * cpu / elapsed if cpu is not None and elapsed and elapsed > 0 else None
            row["cpu_usage_pct"] = usage
            row["jain_fairness_index"] = fairness
            row["fairness_workers"] = len(valid_cpu)
    return rows, warnings


def show_summary(rows, warnings):
    runs = defaultdict(list)
    for row in rows:
        runs[row.get("run_id", "")].append(row)
    print(f"Analyzed {len(rows)} worker rows across {len(runs)} run(s).")
    print("CPU usage is direct-worker CPU time / elapsed time; small deviations above 100% can reflect")
    print("clock and accounting differences in the recorded intervals.")
    print("Jain index compares per-worker CPU-time equality within each run (1.0 = equal). It does not measure")
    print("share-weighted fairness, latency, group policy, or fairness among descendants.")
    for run_id, run_rows in runs.items():
        fairness = run_rows[0].get("jain_fairness_index")
        ftext = "NA" if fairness is None else f"{fairness:.4f}"
        print(f"\nRun {run_id or '(missing run_id)'}: Jain fairness={ftext} "
              f"({run_rows[0].get('fairness_workers', 0)} worker measurements)")
        for row in run_rows:
            usage = row.get("cpu_usage_pct")
            utext = "NA" if usage is None else f"{usage:.2f}%"
            cpu = row.get("cpu_time_s", "NA") or "NA"
            elapsed = row.get("elapsed_s", "NA") or "NA"
            print(f"  {row.get('worker_id', '?')} group={row.get('group', '?')}: "
                  f"CPU={cpu}s elapsed={elapsed}s usage={utext} "
                  f"status={row.get('measurement_status', '?')}")
    if warnings:
        print(f"\nWarnings ({len(warnings)}):")
        for warning in warnings:
            print(f"  {warning}")


def write_summary(path, rows):
    fields = [
        "source_file", "run_id", "timestamp_utc", "kernel_version",
        "logical_cpu_count", "requested_duration_s", "group_counts",
        "worker_id", "group", "group_worker_index", "cpu_time_s",
        "elapsed_s", "cpu_usage_pct", "jain_fairness_index",
        "fairness_workers", "iterations", "exit_status",
        "termination_signal", "measurement_status", "output_log",
    ]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            output = dict(row)
            output["source_file"] = row.get("_source_file", "")
            for field in ("cpu_usage_pct", "jain_fairness_index"):
                value = output.get(field)
                output[field] = "" if value is None else f"{value:.6f}"
            writer.writerow(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", help="CSV file(s); default: results/raw/group_competition_*.csv")
    parser.add_argument("-o", "--output", help="write derived worker rows to this summary CSV")
    args = parser.parse_args()
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if args.inputs:
        paths = args.inputs
    else:
        pattern = os.path.join(repo, "results", "raw", "group_competition_*.csv")
        paths = sorted(path for path in glob.glob(pattern)
                       if os.path.basename(path) != DEBUG_ARTIFACT)
    if not paths:
        parser.error("no input CSV files found")
    rows, warnings = analyze(paths)
    show_summary(rows, warnings)
    if args.output:
        write_summary(args.output, rows)
        print(f"\nWrote summary CSV: {args.output}")
    elif rows:
        print("\nNo output file written; pass --output to save a processed summary.")


if __name__ == "__main__":
    main()
