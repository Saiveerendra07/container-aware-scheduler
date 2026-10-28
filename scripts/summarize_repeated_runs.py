#!/usr/bin/env python3
"""Summarize compatible repeated experiment runs without changing raw results."""

import argparse
import csv
import datetime as dt
import glob
import json
import math
import os
import re
import statistics
import sys
import uuid
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze_results as analyzer
import evaluate_fairness as fairness


SUMMARY_FIELDS = [
    "configuration_id", "scheduling_mode", "result_type", "configuration",
    "group", "metric", "mean", "median", "standard_deviation",
    "sample_count", "excluded_run_count", "exclusion_reasons",
]
METRICS = (
    "cpu_time_s", "cpu_usage_pct", "throttled_time_s", "throttled_period_pct",
    "elapsed_s", "weighted_allocation_error_pct_points", "jain_fairness_index",
)
BASE_CONFIGURATION_FIELDS = (
    "kernel_version", "logical_cpu_count", "architecture", "requested_duration_s",
    "duration_s", "group_counts", "worker_count", "workload", "worker_type",
    "worker_executable", "fallback_share", "control_setting", "scheduler_mode",
)


def _text(value):
    return str(value).strip() if value is not None else ""


def _canonical(value):
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda p: str(p[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return _text(value)


def _finite(value):
    if value in (None, "", "NA"):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _nonnegative(value):
    result = _finite(value)
    return result if result is not None and result >= 0 else None


def _parse_counts(value):
    """Normalize group_counts mappings without guessing when malformed."""
    raw = _text(value)
    if not raw:
        return None
    mapping = {}
    for token in raw.replace(",", ";").split(";"):
        token = token.strip()
        if not token or token.count("=") != 1:
            return {"raw": raw}
        group, count = token.split("=", 1)
        if not group or not count.isascii() or not count.isdecimal() or group in mapping:
            return {"raw": raw}
        mapping[group] = int(count)
    return dict(sorted(mapping.items())) or {"raw": raw}


def _classify(fieldnames):
    names = set(fieldnames or ())
    if {"scheduler_attached", "group_share", "fallback_share"} <= names:
        return "custom_sched_ext", "custom_scheduler"
    if analyzer.CGroup_REQUIRED_COLUMNS <= names:
        return "default_linux_cgroup", "cgroup_competition"
    if analyzer.REQUIRED_COLUMNS <= names:
        return "default_linux_group", "group_competition"
    return None, None


def _read_source(path):
    """Read supported run rows, deriving the existing analyzer's per-row metrics."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            mode, schema = _classify(reader.fieldnames)
            if not mode:
                return [], [f"{path}: unsupported repeated-run CSV schema"]
            source_rows = list(reader)
            if any(None in row or any(v is None for v in row.values()) for row in source_rows):
                return [], [f"{path}: malformed row width; file skipped"]
    except (OSError, UnicodeError, csv.Error) as exc:
        return [], [f"{path}: could not read CSV ({exc})"]

    if schema != "custom_scheduler":
        derived, warnings = analyzer.analyze([path])
        return [(row, mode, schema) for row in derived], warnings
    normalized = []
    for row in source_rows:
        row["result_type"] = schema
        row["elapsed_s"] = row.get("workload_elapsed_s", "NA")
        row["cpu_usage_pct"] = None
        cpu = analyzer.number(row.get("cpu_time_s"))
        elapsed = analyzer.number(row.get("elapsed_s"))
        if cpu is not None and elapsed is not None and elapsed > 0:
            row["cpu_usage_pct"] = 100 * cpu / elapsed
        row["throttled_time_s"] = None
        row["throttled_period_pct"] = None
        normalized.append((row, mode, schema))
    return normalized, []


def _run_exclusion(rows, mode, schema):
    reasons = []
    if not rows:
        return ["run has no measurement rows"]
    environment_fields = ("kernel_version", "logical_cpu_count", "architecture")
    for row in rows:
        for field in environment_fields:
            value = _text(row.get(field))
            if not value or value.upper() == "NA":
                reasons.append(f"{field} environment metadata is unavailable")
        cpu_count = _text(row.get("logical_cpu_count"))
        if not cpu_count.isascii() or not cpu_count.isdecimal() or int(cpu_count) < 1:
            reasons.append("logical_cpu_count is invalid")
        if schema == "custom_scheduler":
            if not _text(row.get("worker_executable")) or _text(row.get("worker_executable")).upper() == "NA":
                reasons.append("worker_executable workload metadata is unavailable")
            for field in ("cgroup_cpu_weight", "cgroup_cpu_max"):
                if not _text(row.get(field)) or _text(row.get(field)).upper() == "NA":
                    reasons.append(f"{field} workload control metadata is unavailable")
    for field in ("kernel_version", "logical_cpu_count", "architecture", "worker_executable"):
        values = {_text(row.get(field)) for row in rows if _text(row.get(field))}
        if len(values) > 1:
            reasons.append(f"inconsistent {field} values within run")
    expected_groups = _parse_counts(rows[0].get("group_counts"))
    if schema == "custom_scheduler" and len({_text(row.get("group")) for row in rows}) < 2:
        reasons.append("custom scheduler run has fewer than two represented groups")
    # Fields that should be identical for every row in a run. Group shares,
    # cgroup paths, and group names are expected to vary by group.
    constant_fields = BASE_CONFIGURATION_FIELDS
    for field in constant_fields:
        values = {_text(row.get(field)) for row in rows if _text(row.get(field))}
        if len(values) > 1:
            reasons.append(f"inconsistent {field} values within run")
    if schema == "custom_scheduler":
        reasons.extend(f"custom configuration: {reason}"
                       for reason in fairness.validate_custom_run_rows(rows))
        shares_by_group = defaultdict(set)
        for row in rows:
            shares_by_group[_text(row.get("group"))].add(_text(row.get("group_share")))
        if any(len(shares) > 1 for shares in shares_by_group.values()):
            reasons.append("inconsistent configured share within a group")
    if expected_groups and "raw" not in expected_groups:
        actual = defaultdict(int)
        for row in rows:
            actual[_text(row.get("group"))] += 1
        # For group-worker runs, each row is a worker; for cgroup/custom runs,
        # there is one measurement row for each named group.
        if schema == "group_competition" and dict(actual) != expected_groups:
            reasons.append("worker group_counts do not match result rows")
    if schema == "cgroup_competition":
        labels = set(re.findall(r"([A-Za-z][A-Za-z0-9_-]*)=", _text(rows[0].get("control_setting"))))
        actual_groups = {_text(row.get("group")) for row in rows}
        if labels and labels != actual_groups:
            reasons.append("configured cgroup labels do not match result rows")

    for row in rows:
        status = _text(row.get("measurement_status")).lower()
        if status != "complete":
            reasons.append(f"measurement_status is {status or 'missing'}")
        if _nonnegative(row.get("cpu_time_s")) is None:
            reasons.append("cpu_time_s is missing or invalid")
        elapsed_value = row.get("workload_elapsed_s") if schema == "custom_scheduler" else row.get("elapsed_s")
        if _nonnegative(elapsed_value) is None or _nonnegative(elapsed_value) <= 0:
            reasons.append("elapsed time is missing or invalid")
        exit_status = _text(row.get("exit_status"))
        if exit_status != "0":
            reasons.append(f"exit_status is {exit_status}")
        if _text(row.get("termination_signal")) not in ("", "0", "NA"):
            reasons.append("worker was terminated by signal")
        if schema == "cgroup_competition" and _text(row.get("cpu_stat_status")).lower() != "complete":
            reasons.append(f"cpu_stat_status is {_text(row.get('cpu_stat_status')) or 'missing'}")
        if schema == "custom_scheduler":
            if _text(row.get("scheduler_attached")).lower() != "true":
                reasons.append("scheduler attachment was not confirmed")
            if _text(row.get("scheduler_shutdown_status")) != "stopped":
                reasons.append("scheduler shutdown was not confirmed clean")
    return sorted(set(reasons))


def _run_configuration(rows, mode, schema):
    first = rows[0]
    config = {key: _canonical(first.get(key, "")) for key in BASE_CONFIGURATION_FIELDS}
    config["scheduling_mode"] = mode
    config["result_type"] = schema
    groups = sorted({_text(row.get("group")) for row in rows if _text(row.get("group"))})
    config["groups"] = groups
    if schema == "group_competition":
        config["group_counts"] = _parse_counts(first.get("group_counts")) or {
            name: sum(_text(row.get("group")) == name for row in rows) for name in groups
        }
    if schema == "custom_scheduler":
        expected = fairness.custom_configuration_from_rows(rows)
        if isinstance(expected, dict) and isinstance(expected.get("groups"), list):
            config["custom_scheduler_schema_version"] = _text(
                first.get("custom_scheduler_schema_version"))
            config["expected_group_count"] = expected.get("expected_group_count")
            config["expected_group_config"] = expected
            groups = sorted(group.get("name", "") for group in expected["groups"]
                            if isinstance(group, dict))
            config["groups"] = groups
        else:
            config["expected_group_config"] = "unavailable_legacy_metadata"
        shares = {}
        configured_names = groups
        for name in configured_names:
            group_row = next((row for row in rows if _text(row.get("group")) == name), None)
            if group_row is None:
                shares[name] = next((
                    group.get("share") for group in (expected or {}).get("groups", [])
                    if isinstance(group, dict) and group.get("name") == name
                ), "NA")
                continue
            raw_share = _text(group_row.get("group_share", ""))
            shares[name] = int(raw_share) if raw_share.isascii() and raw_share.isdecimal() else raw_share
        config["configured_shares"] = shares
        config["cgroup_cpu_controls"] = {
            name: {
                "cpu_weight": _text(next((row.get("cgroup_cpu_weight", "") for row in rows
                                           if _text(row.get("group")) == name), "NA")),
                "cpu_max": _text(next((row.get("cgroup_cpu_max", "") for row in rows
                                       if _text(row.get("group")) == name), "NA")),
            } for name in groups
        }
    if schema == "cgroup_competition":
        config["control_setting"] = _canonical(first.get("control_setting", ""))
    return config


def _metric_values(rows, schema, fair_rows):
    by_group = defaultdict(list)
    for row in rows:
        by_group[_text(row.get("group"))].append(row)
    values = []
    cpu_by_group = {}
    for group, group_rows in sorted(by_group.items()):
        cpu_values = [_nonnegative(row.get("cpu_time_s")) for row in group_rows]
        elapsed_values = [_nonnegative(row.get("elapsed_s")) for row in group_rows]
        if any(value is None for value in cpu_values):
            continue
        cpu = sum(cpu_values)
        elapsed = (sum(v for v in elapsed_values if v is not None) / len(elapsed_values)
                   if elapsed_values and all(v is not None for v in elapsed_values) else None)
        cpu_by_group[group] = cpu
        values.extend([
            (group, "cpu_time_s", cpu),
            (group, "elapsed_s", elapsed),
            (group, "cpu_usage_pct", 100 * cpu / elapsed if elapsed and elapsed > 0 else None),
        ])
        if schema == "cgroup_competition":
            for metric in ("throttled_time_s", "throttled_period_pct"):
                parts = [_nonnegative(row.get(metric)) for row in group_rows]
                metric_value = None
                if parts and all(part is not None for part in parts):
                    if metric == "throttled_time_s":
                        metric_value = sum(parts)
                    else:
                        periods = [analyzer.counter_number(row.get("nr_periods_delta"))
                                   for row in group_rows]
                        throttled = [analyzer.counter_number(row.get("nr_throttled_delta"))
                                     for row in group_rows]
                        if all(v is not None for v in periods + throttled) and sum(periods) > 0:
                            metric_value = 100 * sum(throttled) / sum(periods)
                values.append((group, metric, metric_value))
    if len(cpu_by_group) >= 2:
        fairness_index = analyzer.jain_index(list(cpu_by_group.values()))
        values.append(("ALL_GROUPS", "jain_fairness_index", fairness_index))
    for row in fair_rows:
        group = _text(row.get("group"))
        error = _finite(row.get("allocation_error_pct_points"))
        values.append((group, "weighted_allocation_error_pct_points", error))
    return values


def summarize(paths):
    """Return long-form repeated-run summaries and detailed run exclusions."""
    runs = {}
    warnings = []
    paths = list(dict.fromkeys(os.path.abspath(path) for path in paths))
    for path in paths:
        parsed, file_warnings = _read_source(path)
        warnings.extend(file_warnings)
        for row, mode, schema in parsed:
            key = (os.path.abspath(path), _text(row.get("run_id")), mode, schema)
            runs.setdefault(key, []).append(row)

    fairness_results = {}
    for path in paths:
        evaluated, eval_warnings = fairness.load_inputs([path])
        warnings.extend(eval_warnings)
        for key, run in evaluated.items():
            fair_rows = fairness.evaluate_run(run)
            fairness_results[(os.path.abspath(path), run["run_id"])] = fair_rows

    grouped = defaultdict(lambda: {"configuration": None, "runs": [], "excluded": []})
    exclusions = []
    for (path, run_id, mode, schema), rows in runs.items():
        config = _run_configuration(rows, mode, schema)
        config_text = json.dumps(config, sort_keys=True, separators=(",", ":"))
        config_id = uuid.uuid5(uuid.NAMESPACE_URL, config_text).hex[:12]
        reasons = _run_exclusion(rows, mode, schema)
        record = grouped[(config_id, mode, schema, config_text)]
        record["configuration"] = config_text
        if reasons:
            exclusion = {"configuration_id": config_id, "run_id": run_id,
                         "source_file": path, "reasons": reasons}
            record["excluded"].append(exclusion)
            exclusions.append(exclusion)
        else:
            record["runs"].append((path, run_id, rows,
                                   _metric_values(rows, schema,
                                       fairness_results.get((path, run_id), []))))

    output = []
    for (config_id, mode, schema, config_text), data in sorted(grouped.items()):
        by_metric = defaultdict(list)
        for _path, _run_id, _rows, metrics in data["runs"]:
            for group, metric, value in metrics:
                if value is not None and math.isfinite(value):
                    by_metric[(group, metric)].append(value)
        exclusion_reasons = sorted({reason for item in data["excluded"] for reason in item["reasons"]})
        all_keys = set(by_metric)
        # Emit unavailable metrics too, so missing data is visible rather than
        # silently disappearing from a configuration's summary.
        for metric in METRICS:
            groups = {group for group, name in all_keys if name == metric}
            if metric == "jain_fairness_index" and not groups:
                groups = {"ALL_GROUPS"}
            if metric == "weighted_allocation_error_pct_points":
                groups.update(_configured_group_names(config_text))
            if metric in ("cpu_time_s", "cpu_usage_pct", "elapsed_s", "throttled_time_s", "throttled_period_pct"):
                groups.update(_configured_group_names(config_text))
            for group in sorted(groups):
                values = by_metric.get((group, metric), [])
                output.append({
                    "configuration_id": config_id, "scheduling_mode": mode,
                    "result_type": schema, "configuration": config_text,
                    "group": group, "metric": metric,
                    "mean": statistics.fmean(values) if values else "NA",
                    "median": statistics.median(values) if values else "NA",
                    "standard_deviation": statistics.stdev(values) if len(values) > 1 else "NA",
                    "sample_count": len(values),
                    "excluded_run_count": len(data["excluded"]),
                    "exclusion_reasons": "; ".join(exclusion_reasons),
                })
        for item in data["excluded"]:
            output.append({
                "configuration_id": config_id, "scheduling_mode": mode,
                "result_type": schema, "configuration": config_text,
                "group": "", "metric": "run_exclusion", "mean": "NA",
                "median": "NA", "standard_deviation": "NA", "sample_count": 0,
                "excluded_run_count": 1, "exclusion_reasons":
                    f"{item['run_id']}: " + "; ".join(item["reasons"]),
            })
    return output, exclusions, warnings


def _configured_group_names(config_text):
    try:
        config = json.loads(config_text)
    except (TypeError, json.JSONDecodeError):
        return set()
    return set(config.get("groups", []))


def write_csv(path, rows):
    with open(path, "x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(path, rows, exclusions, warnings):
    with open(path, "x", encoding="utf-8") as stream:
        stream.write("# Repeated experiment run summary\n\n")
        stream.write("This report summarizes compatible completed runs. It does not establish statistical significance.\n\n")
        configs = sorted({row["configuration_id"] for row in rows})
        stream.write(f"Configurations: {len(configs)}; excluded runs: {len(exclusions)}.\n\n")
        stream.write("| Configuration | Mode | Group | Metric | Mean | Median | Sample SD | n |\n")
        stream.write("|---|---|---|---|---:|---:|---:|---:|\n")
        for row in rows:
            if row["metric"] == "run_exclusion":
                continue
            fmt = lambda value: f"{value:.6g}" if isinstance(value, (int, float)) else str(value)
            stream.write(f"| {row['configuration_id']} | {row['scheduling_mode']} | {row['group']} | "
                         f"{row['metric']} | {fmt(row['mean'])} | {fmt(row['median'])} | "
                         f"{fmt(row['standard_deviation'])} | {row['sample_count']} |\n")
        stream.write("\n## Excluded runs\n\n")
        if exclusions:
            for item in exclusions:
                stream.write(f"- `{Path(item['source_file']).name}` run `{item['run_id']}`: "
                             + "; ".join(item["reasons"]) + "\n")
        else:
            stream.write("None.\n")
        if warnings:
            stream.write("\n## Input warnings\n\n")
            for warning in warnings:
                stream.write(f"- {warning}\n")


def unique_outputs(output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    while True:
        token = uuid.uuid4().hex[:10]
        csv_path = output_dir / f"repeated_run_summary_{stamp}_{token}.csv"
        report_path = output_dir / f"repeated_run_summary_{stamp}_{token}.md"
        if not csv_path.exists() and not report_path.exists():
            return csv_path, report_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", help="raw group, cgroup, or custom-scheduler result CSVs")
    parser.add_argument("--output-dir", default="results/processed",
                        help="directory for new CSV and Markdown outputs")
    args = parser.parse_args(argv)
    paths = args.inputs or sorted(
        glob.glob("results/raw/group_competition_*.csv")
        + glob.glob("results/raw/cgroup_competition_*.csv")
        + glob.glob("results/raw/custom_scheduler_run_*.csv"))
    if not paths:
        parser.error("no input CSV files found")
    rows, exclusions, warnings = summarize(paths)
    csv_path, report_path = unique_outputs(args.output_dir)
    write_csv(csv_path, rows)
    write_markdown(report_path, rows, exclusions, warnings)
    for warning in warnings:
        print(f"Warning: {warning}")
    print(f"Summary CSV: {csv_path}")
    print(f"Report: {report_path}")
    print(f"Analysis complete; excluded runs: {len(exclusions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
