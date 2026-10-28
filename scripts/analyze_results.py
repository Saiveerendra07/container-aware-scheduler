#!/usr/bin/env python3
"""Analyze group-competition and cgroup-competition CSVs without changing raw files."""

import argparse
import csv
import glob
import math
import os
import uuid
from collections import defaultdict


DEBUG_ARTIFACT = "group_competition_20261019T083552Z_LUDraY.csv"
REQUIRED_COLUMNS = {
    "run_id", "worker_id", "group", "cpu_time_s", "elapsed_s",
    "measurement_status",
}
CGroup_REQUIRED_COLUMNS = {
    "run_id", "group", "workload_elapsed_s", "exit_status",
    "measurement_status", "cpu_stat_status",
    *(f"{counter}_{phase}" for counter in (
        "usage_usec", "user_usec", "system_usec", "nr_periods",
        "nr_throttled", "throttled_usec",
    ) for phase in ("before", "after")),
    "usage_usec_delta", "nr_periods_delta", "nr_throttled_delta",
    "throttled_usec_delta",
}
CGroup_COUNTERS = (
    "usage_usec", "user_usec", "system_usec", "nr_periods",
    "nr_throttled", "throttled_usec",
)


def number(value):
    """Return a finite nonnegative float, or None for missing/bad data."""
    if value is None or not str(value).strip() or str(value).strip().upper() == "NA":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def counter_number(value):
    """Return an unsigned integer counter, rejecting malformed/rounded values."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 20:
        return None
    parsed = int(raw)
    return parsed if parsed <= 18446744073709551615 else None


def jain_index(values):
    """Jain's index for nonnegative worker CPU-time values."""
    if not values:
        return None
    total = sum(values)
    squares = sum(value * value for value in values)
    if squares == 0:
        return None
    return total * total / (len(values) * squares)


def _successful_run_row(row):
    if str(row.get("measurement_status", "")).strip().lower() != "complete":
        return False
    exit_status = str(row.get("exit_status", "")).strip()
    if row.get("result_type") == "cgroup_competition":
        return exit_status == "0"
    if exit_status not in ("", "NA", "0"):
        return False
    signal_status = str(row.get("termination_signal", "")).strip()
    return signal_status in ("", "NA", "0")


def _group_rows_complete(run_rows):
    raw_counts = str(run_rows[0].get("group_counts", "")).strip()
    if not raw_counts:
        return True
    expected = {}
    for item in raw_counts.split(";"):
        if item.count("=") != 1:
            return False
        group, raw_count = item.split("=", 1)
        if not group or not raw_count.isascii() or not raw_count.isdecimal() or group in expected:
            return False
        expected[group] = int(raw_count)
    actual = defaultdict(int)
    for row in run_rows:
        actual[row.get("group", "")] += 1
    return bool(expected) and dict(actual) == expected


def read_file(path):
    rows = []
    warnings = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames:
                return [], [f"{path}: missing CSV header; skipped file"]
            if CGroup_REQUIRED_COLUMNS.issubset(reader.fieldnames):
                schema = "cgroup_competition"
            elif REQUIRED_COLUMNS.issubset(reader.fieldnames):
                schema = "group_competition"
            else:
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
                row["result_type"] = schema
                if schema == "cgroup_competition":
                    row["worker_id"] = ""
                    row["cpu_time_s"] = "NA"
                    row["elapsed_s"] = row.get("workload_elapsed_s", "NA")
                    row["iterations"] = "NA"
                    row["termination_signal"] = "NA"
                    row["output_log"] = "NA"
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
        run_complete = all(_successful_run_row(row) for row in run_rows)
        group_rows = [row for row in run_rows if row.get("result_type") != "cgroup_competition"]
        if group_rows and not _group_rows_complete(group_rows):
            run_complete = False
        valid_cpu = []
        if run_complete:
            for row in group_rows:
                cpu = number(row.get("cpu_time_s"))
                elapsed = number(row.get("elapsed_s"))
                if cpu is not None and elapsed is not None and elapsed > 0:
                    valid_cpu.append(cpu)
                else:
                    run_complete = False
                    valid_cpu = []
                    break
        fairness = jain_index(valid_cpu)
        for row in run_rows:
            if row.get("result_type") == "cgroup_competition":
                valid_deltas = 0
                for counter in CGroup_COUNTERS:
                    before = counter_number(row.get(f"{counter}_before"))
                    after = counter_number(row.get(f"{counter}_after"))
                    delta = after - before if before is not None and after is not None and after >= before else None
                    row[f"{counter}_delta"] = "NA" if delta is None else delta
                    valid_deltas += delta is not None
                row["cpu_stat_status"] = (
                    "complete" if valid_deltas == len(CGroup_COUNTERS)
                    else "partial" if valid_deltas else "unavailable"
                )
                cpu_usec = counter_number(row["usage_usec_delta"])
                elapsed = number(row.get("workload_elapsed_s"))
                # cpu.stat is in microseconds. Invalid or missing deltas stay unavailable.
                row["cpu_time_s"] = cpu_usec / 1_000_000 if run_complete and cpu_usec is not None else None
                row["elapsed_s"] = elapsed if run_complete else None
                row["cpu_usage_pct"] = (100.0 * cpu_usec / 1_000_000 / elapsed
                                        if run_complete and cpu_usec is not None and elapsed is not None and elapsed > 0
                                        else None)
                periods = counter_number(row["nr_periods_delta"])
                throttled = counter_number(row["nr_throttled_delta"])
                throttled_usec = counter_number(row["throttled_usec_delta"])
                row["nr_periods_delta"] = periods
                row["nr_throttled_delta"] = throttled
                row["throttled_time_s"] = (throttled_usec / 1_000_000
                                            if run_complete and throttled_usec is not None else None)
                row["throttled_period_pct"] = (100.0 * throttled / periods
                                               if run_complete and throttled is not None
                                               and periods is not None and periods > 0
                                               else None)
                row["jain_fairness_index"] = None
                row["fairness_workers"] = 0
                continue
            cpu = number(row.get("cpu_time_s")) if run_complete else None
            elapsed = number(row.get("elapsed_s")) if run_complete else None
            row["cpu_time_s"] = cpu
            row["elapsed_s"] = elapsed
            usage = 100.0 * cpu / elapsed if cpu is not None and elapsed and elapsed > 0 else None
            row["cpu_usage_pct"] = usage
            row["jain_fairness_index"] = fairness
            row["fairness_workers"] = len(valid_cpu)
    return rows, warnings


def show_summary(rows, warnings):
    runs = defaultdict(list)
    for row in rows:
        runs[row.get("run_id", "")].append(row)
    print(f"Analyzed {len(rows)} result rows across {len(runs)} run(s).")
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
            cpu = row.get("cpu_time_s")
            elapsed = row.get("elapsed_s")
            cpu = "NA" if cpu is None else f"{cpu:.6f}" if isinstance(cpu, (int, float)) else cpu
            elapsed = "NA" if elapsed is None else f"{elapsed:.6f}" if isinstance(elapsed, (int, float)) else elapsed
            if row.get("result_type") == "cgroup_competition":
                throttle_s = row.get("throttled_time_s")
                periods = row.get("nr_throttled_delta")
                ttext = "NA" if throttle_s is None else f"{throttle_s:.6f}s"
                ptext = "NA" if periods is None else str(int(periods))
                print(f"  cgroup {row.get('group', '?')}: CPU={cpu}s elapsed={elapsed}s "
                      f"usage={utext} throttled_periods={ptext} throttled_time={ttext} "
                      f"exit={row.get('exit_status', 'NA')} status={row.get('measurement_status', '?')}")
            else:
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
        "logical_cpu_count", "requested_duration_s", "group_counts", "result_type",
        "control_setting", "cgroup_path", "cpu_stat_status",
        "worker_id", "group", "group_worker_index", "cpu_time_s",
        "elapsed_s", "cpu_usage_pct", "jain_fairness_index",
        "fairness_workers", "iterations", "exit_status",
        "termination_signal", "measurement_status", "output_log",
        "usage_usec_delta", "user_usec_delta", "system_usec_delta",
        "nr_periods_delta", "nr_throttled_delta", "throttled_usec_delta",
        "throttled_time_s", "throttled_period_pct",
    ]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if os.path.exists(path):
        stem, extension = os.path.splitext(path)
        extension = extension or ".csv"
        while True:
            candidate = f"{stem}_{uuid.uuid4().hex[:12]}{extension}"
            if not os.path.exists(candidate):
                path = candidate
                break
    with open(path, "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            output = dict(row)
            output["source_file"] = row.get("_source_file", "")
            for field in ("cpu_time_s", "elapsed_s", "cpu_usage_pct", "jain_fairness_index",
                          "throttled_time_s", "throttled_period_pct"):
                value = output.get(field)
                output[field] = ("NA" if output.get("result_type") == "cgroup_competition" else "") if value is None else f"{float(value):.6f}"
            if output.get("result_type") == "cgroup_competition":
                for field in ("usage_usec_delta", "user_usec_delta", "system_usec_delta",
                              "nr_periods_delta", "nr_throttled_delta", "throttled_usec_delta"):
                    if output.get(field) is None:
                        output[field] = "NA"
            writer.writerow(output)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", help="group or cgroup competition CSV file(s); default: results/raw/*competition_*.csv")
    parser.add_argument("-o", "--output", help="write derived result rows to this summary CSV")
    args = parser.parse_args()
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if args.inputs:
        paths = args.inputs
    else:
        patterns = ("group_competition_*.csv", "cgroup_competition_*.csv")
        paths = sorted(path for pattern in patterns
                       for path in glob.glob(os.path.join(repo, "results", "raw", pattern))
                       if os.path.basename(path) != DEBUG_ARTIFACT)
    if not paths:
        parser.error("no input CSV files found")
    rows, warnings = analyze(paths)
    show_summary(rows, warnings)
    if args.output:
        output_path = write_summary(args.output, rows)
        print(f"\nWrote summary CSV: {output_path}")
    elif rows:
        print("\nNo output file written; pass --output to save a processed summary.")


if __name__ == "__main__":
    main()
