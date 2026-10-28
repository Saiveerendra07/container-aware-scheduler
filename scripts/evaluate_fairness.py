#!/usr/bin/env python3
"""Compare observed per-group CPU allocation with configured relative shares."""

import argparse
import csv
import json
import math
import os
import re
import sys
from collections import defaultdict


GROUP_COLUMNS = {"run_id", "group", "cpu_time_s", "measurement_status"}
CGROUP_COLUMNS = {
    "run_id", "group", "control_setting", "workload_elapsed_s", "exit_status",
    "measurement_status", "cpu_stat_status", "usage_usec_before", "usage_usec_after",
}
SHARE_FIELDS = ("group_share", "configured_share", "share")
SHARE_MAP_FIELDS = ("group_shares", "configured_group_shares")
MAX_SHARE = 10000
CUSTOM_SCHEDULER_SCHEMA_VERSION = "2"
CUSTOM_CONFIGURATION_FIELDS = {
    "scheduler_attached", "group_share", "fallback_share",
    "custom_scheduler_schema_version", "expected_group_count",
    "expected_group_config_json",
    "cgroup_id", "verified_cgroup_id", "cgroup_path",
    "identity_verification_status", "completion_status", "measurement_status",
    "exit_status", "termination_signal", "workload_elapsed_s", "cpu_time_s",
    "scheduler_shutdown_status",
}
OUTPUT_COLUMNS = (
    "source_file", "run_id", "result_type", "group", "configured_share",
    "share_source", "measurement_count", "excluded_measurement_count",
    "cpu_time_s", "observed_cpu_pct", "expected_cpu_pct",
    "allocation_error_pct_points", "absolute_allocation_error_pct_points",
    "jain_fairness_index", "jain_group_count", "measurement_exclusion_reason",
    "allocation_unavailable_reason",
)


def finite_nonnegative(value):
    if value is None or not str(value).strip() or str(value).strip().upper() == "NA":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def unsigned_counter(value):
    if value is None:
        return None
    raw = str(value).strip()
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 20:
        return None
    result = int(raw)
    return result if result <= 18446744073709551615 else None


def parse_share_map(value):
    """Parse a semicolon/comma/space separated name=positive-integer mapping."""
    if value is None:
        return None
    result = {}
    tokens = re.split(r"[;,\s]+", str(value).strip())
    for token in filter(None, tokens):
        if token.count("=") != 1:
            return None
        name, raw_share = token.split("=", 1)
        if not name or not raw_share.isascii() or not raw_share.isdecimal():
            return None
        share = int(raw_share)
        if share <= 0 or share > MAX_SHARE or name in result:
            return None
        result[name] = share
    return result or None


def jain_index(values):
    if len(values) < 2:
        return None
    total = sum(values)
    squares = sum(value * value for value in values)
    if squares == 0:
        return None
    return total * total / (len(values) * squares)


def identify_schema(fieldnames):
    if fieldnames and CGROUP_COLUMNS.issubset(fieldnames):
        return "cgroup_competition"
    if (fieldnames and GROUP_COLUMNS.issubset(fieldnames)
            and {"scheduler_attached", "group_share", "fallback_share"}.issubset(fieldnames)):
        return "custom_scheduler"
    if fieldnames and GROUP_COLUMNS.issubset(fieldnames):
        return "group_competition"
    return None


def custom_configuration_from_rows(rows):
    """Return the versioned expected custom group configuration, if present."""
    if not rows or any(not CUSTOM_CONFIGURATION_FIELDS.issubset(row) for row in rows):
        return None
    try:
        config = json.loads(rows[0]["expected_group_config_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(config, dict):
        return None
    groups = config.get("groups")
    if (not isinstance(groups, list)
            or any(not isinstance(group, dict) or not isinstance(group.get("name"), str)
                   for group in groups)):
        return None
    return config


def validate_custom_run_rows(rows):
    """Validate complete group membership, IDs, shares, and run metadata."""
    if not rows:
        return ["custom scheduler run has no result rows"]
    errors = []
    if any(not CUSTOM_CONFIGURATION_FIELDS.issubset(row) for row in rows):
        return ["legacy custom-scheduler result lacks expected-group configuration metadata"]

    configurations = []
    for row in rows:
        if str(row.get("custom_scheduler_schema_version", "")) != CUSTOM_SCHEDULER_SCHEMA_VERSION:
            errors.append("unsupported or missing custom scheduler schema version")
        try:
            config = json.loads(row["expected_group_config_json"])
        except (TypeError, json.JSONDecodeError):
            errors.append("expected group configuration JSON is missing or invalid")
            continue
        if not isinstance(config, dict):
            errors.append("expected group configuration must be a JSON object")
            continue
        configurations.append(config)

    if not configurations:
        return sorted(set(errors))
    expected = configurations[0]
    if any(config != expected for config in configurations[1:]):
        errors.append("expected group configuration differs between result rows")

    expected_count = expected.get("expected_group_count")
    groups = expected.get("groups")
    fallback = expected.get("fallback_share")
    if type(expected_count) is not int or not 2 <= expected_count <= 8:
        errors.append("expected group count is missing or outside 2..8")
        expected_count = None
    if type(fallback) is not int or not 1 <= fallback <= MAX_SHARE:
        errors.append("expected fallback share is missing or invalid")
    if not isinstance(groups, list):
        errors.append("expected groups configuration is missing or invalid")
        groups = []
    if expected_count is not None and len(groups) != expected_count:
        errors.append("expected group count does not match configured group list")
    if len(rows) != len(groups):
        errors.append("result row count does not match expected group count")

    expected_by_name = {}
    expected_ids = set()
    expected_paths = set()
    for group in groups:
        if not isinstance(group, dict):
            errors.append("configured group entry is invalid")
            continue
        name, cgroup_id = group.get("name"), group.get("cgroup_id")
        share, path = group.get("share"), group.get("cgroup_path")
        if (not isinstance(name, str) or not name
                or type(cgroup_id) is not int or not 0 < cgroup_id <= 18446744073709551615
                or type(share) is not int or not 1 <= share <= MAX_SHARE
                or not isinstance(path, str) or not path):
            errors.append("configured group identity or share is missing or invalid")
            continue
        if name in expected_by_name or cgroup_id in expected_ids or path in expected_paths:
            errors.append("expected group names, cgroup IDs, and paths must be unique")
            continue
        expected_by_name[name] = group
        expected_ids.add(cgroup_id)
        expected_paths.add(path)

    if len(expected_by_name) != len(groups):
        errors.append("expected group configuration contains invalid or duplicate entries")
    if (type(fallback) is int and 1 <= fallback <= MAX_SHARE
            and all(type(group.get("share")) is int and 1 <= group["share"] <= MAX_SHARE
                    for group in groups if isinstance(group, dict))
            and sum(group["share"] for group in groups if isinstance(group, dict)) + fallback
            > 9 * MAX_SHARE):
        errors.append("combined configured shares exceed scheduler limit")

    rows_by_name = defaultdict(list)
    for row in rows:
        name = str(row.get("group", ""))
        rows_by_name[name].append(row)
        try:
            row_count = int(row.get("expected_group_count", ""))
        except (TypeError, ValueError):
            row_count = None
        if row_count != expected_count:
            errors.append("expected group count differs between metadata and result rows")
        try:
            row_fallback = int(row.get("fallback_share", ""))
        except (TypeError, ValueError):
            row_fallback = None
        if row_fallback != fallback:
            errors.append("fallback share differs between metadata and result rows")
        configured = expected_by_name.get(name)
        if configured is None:
            errors.append(f"unexpected result group {name!r}")
            continue
        try:
            row_id = int(row.get("cgroup_id", ""))
            verified_id = int(row.get("verified_cgroup_id", ""))
            row_share = int(row.get("group_share", ""))
        except (TypeError, ValueError):
            row_id = verified_id = row_share = None
        if row_id != configured["cgroup_id"] or verified_id != configured["cgroup_id"]:
            errors.append(f"cgroup identity mismatch for group {name!r}")
        if row.get("identity_verification_status") != "verified":
            errors.append(f"cgroup identity is unverified for group {name!r}")
        if row.get("cgroup_path") != configured["cgroup_path"]:
            errors.append(f"cgroup path mismatch for group {name!r}")
        if row_share != configured["share"]:
            errors.append(f"configured share mismatch for group {name!r}")
        if str(row.get("scheduler_attached", "")).strip().lower() != "true":
            errors.append("scheduler attachment was not confirmed")
        if row.get("scheduler_shutdown_status") != "stopped":
            errors.append("scheduler shutdown was not confirmed clean")
        if row.get("measurement_status", "").strip().lower() != "complete":
            errors.append("measurement_status is not complete")
        if row.get("completion_status") != "complete":
            errors.append("completion_status is not complete")
        if str(row.get("exit_status", "")).strip() != "0":
            errors.append("worker exit_status is not zero")
        if str(row.get("termination_signal", "")).strip() != "0":
            errors.append("worker termination_signal is not zero")
        elapsed = finite_nonnegative(row.get("workload_elapsed_s"))
        if elapsed is None or elapsed <= 0:
            errors.append("workload elapsed time is missing or invalid")
        if finite_nonnegative(row.get("cpu_time_s")) is None:
            errors.append("CPU-time measurement is missing or invalid")
    for name in expected_by_name:
        occurrences = len(rows_by_name.get(name, []))
        if occurrences == 0:
            errors.append(f"expected group row {name!r} is missing")
        elif occurrences > 1:
            errors.append(f"expected group row {name!r} is duplicated")
    return sorted(set(errors))


def _row_share(row, group, result_type):
    if result_type == "cgroup_competition":
        setting = str(row.get("control_setting", "")).strip()
        if setting.startswith("cpu.weight"):
            share_map = parse_share_map(setting[len("cpu.weight"):])
            if share_map is None:
                return None, "invalid cpu.weight control_setting"
            if group not in share_map:
                return None, f"group {group!r} has no cpu.weight in control_setting"
            return share_map[group], "control_setting cpu.weight"
        if setting.startswith("cpu.max"):
            return None, "cpu.max is a quota, not a relative group share"
        return None, "configured shares are missing from control_setting"

    for field in SHARE_FIELDS:
        if field in row:
            raw = str(row.get(field, "")).strip()
            if (not raw.isascii() or not raw.isdecimal()
                    or not 0 < int(raw) <= MAX_SHARE):
                return None, f"invalid {field} value"
            return int(raw), field
    for field in SHARE_MAP_FIELDS:
        if field in row:
            share_map = parse_share_map(row.get(field))
            if share_map is None:
                return None, f"invalid {field} mapping"
            if group not in share_map:
                return None, f"group {group!r} missing from {field} mapping"
            return share_map[group], field
    if "group_counts" in row:
        return None, "group_counts records worker counts, not configured shares"
    return None, "configured group shares are not recorded in this CSV schema"


def _measurement(row, result_type):
    if str(row.get("measurement_status", "")).strip().lower() != "complete":
        return None, "measurement_status is not complete"
    if result_type == "cgroup_competition":
        if str(row.get("exit_status", "")).strip() != "0":
            return None, f"worker exit_status is {row.get('exit_status') or 'missing'}"
        if str(row.get("cpu_stat_status", "")).strip().lower() != "complete":
            return None, f"cpu_stat_status is {row.get('cpu_stat_status') or 'missing'}"
        before = unsigned_counter(row.get("usage_usec_before"))
        after = unsigned_counter(row.get("usage_usec_after"))
        if before is None or after is None:
            return None, "usage_usec before/after counter is missing or invalid"
        if after < before:
            return None, "usage_usec counter reset (after is lower than before)"
        return (after - before) / 1_000_000, None

    exit_status = row.get("exit_status")
    if exit_status not in (None, "", "NA") and str(exit_status).strip() != "0":
        return None, f"worker exit_status is {exit_status}"
    termination_signal = row.get("termination_signal")
    if termination_signal not in (None, "", "NA") and str(termination_signal).strip() != "0":
        return None, f"worker termination_signal is {termination_signal}"

    cpu_time = finite_nonnegative(row.get("cpu_time_s"))
    if cpu_time is None:
        return None, "cpu_time_s is missing or invalid"
    return cpu_time, None


def load_inputs(paths):
    runs = {}
    warnings = []
    for path in paths:
        try:
            with open(path, newline="", encoding="utf-8-sig") as stream:
                reader = csv.DictReader(stream)
                schema = identify_schema(reader.fieldnames)
                if not schema:
                    warnings.append(f"{path}: unsupported CSV schema; skipped file")
                    continue
                for line_number, row in enumerate(reader, start=2):
                    if None in row:
                        warnings.append(f"{path}:{line_number}: extra CSV fields; skipped row")
                        continue
                    run_id = str(row.get("run_id") or "(missing run_id)").strip()
                    group = str(row.get("group") or "(missing group)").strip()
                    key = (os.path.abspath(path), run_id, schema)
                    run = runs.setdefault(key, {
                        "source_file": path,
                        "run_id": run_id,
                        "result_type": schema,
                        "groups": defaultdict(lambda: {
                            "cpu_times": [], "errors": [], "shares": set(),
                            "share_sources": set(), "share_errors": set(),
                            "measurement_rows": 0,
                        }),
                        "share_map": {},
                        "share_map_errors": [],
                        "source_rows": [],
                    })
                    run["source_rows"].append(row)
                    group_data = run["groups"][group]
                    group_data["measurement_rows"] += 1
                    cpu_time, error = _measurement(row, schema)
                    if error:
                        group_data["errors"].append(f"line {line_number}: {error}")
                    else:
                        group_data["cpu_times"].append(cpu_time)

                    share, share_source = _row_share(row, group, schema)
                    if share is not None:
                        group_data["shares"].add(share)
                        group_data["share_sources"].add(share_source)
                    else:
                        group_data["share_errors"].add(share_source)

                    if schema == "cgroup_competition":
                        setting = str(row.get("control_setting") or "").strip()
                        if setting.startswith("cpu.weight"):
                            setting_shares = parse_share_map(setting[len("cpu.weight"):])
                            if setting_shares is None:
                                run["share_map_errors"].append(
                                    f"line {line_number}: invalid cpu.weight control_setting")
                            else:
                                for name, value in setting_shares.items():
                                    previous = run["share_map"].get(name)
                                    if previous is not None and previous != value:
                                        run["share_map_errors"].append(
                                            f"conflicting configured share for group {name!r}")
                                    run["share_map"][name] = value
                    else:
                        for field in SHARE_MAP_FIELDS:
                            if field in row and row.get(field):
                                mapping = parse_share_map(row[field])
                                if mapping is None:
                                    run["share_map_errors"].append(
                                        f"line {line_number}: invalid {field} mapping")
                                else:
                                    for name, value in mapping.items():
                                        previous = run["share_map"].get(name)
                                        if previous is not None and previous != value:
                                            run["share_map_errors"].append(
                                                f"conflicting configured share for group {name!r}")
                                        run["share_map"][name] = value
        except (OSError, UnicodeError, csv.Error) as exc:
            warnings.append(f"{path}: could not read CSV ({exc}); skipped file")
    return runs, warnings


def evaluate_run(run):
    groups = run["groups"]
    custom_errors = (validate_custom_run_rows(run.get("source_rows", []))
                     if run["result_type"] == "custom_scheduler" else [])
    custom_config = (custom_configuration_from_rows(run.get("source_rows", []))
                     if run["result_type"] == "custom_scheduler" else None)
    if isinstance(custom_config, dict):
        for configured in custom_config.get("groups", []):
            if isinstance(configured, dict) and isinstance(configured.get("name"), str):
                groups[configured["name"]]
                run["share_map"][configured["name"]] = configured.get("share")
    for name, _share in run["share_map"].items():
        groups[name]  # ensure configured but absent groups are reported

    measured = {}
    errors = {}
    shares = {}
    share_reasons = {}
    for name, data in groups.items():
        if data["errors"]:
            errors[name] = "; ".join(data["errors"])
        elif not data["cpu_times"]:
            errors[name] = "no valid CPU-time measurements"
        else:
            measured[name] = sum(data["cpu_times"])
        if data["share_errors"]:
            share_reasons[name] = "; ".join(sorted(data["share_errors"]))
        elif len(data["shares"]) == 1:
            shares[name] = next(iter(data["shares"]))
            if name in run["share_map"] and shares[name] != run["share_map"][name]:
                share_reasons[name] = "per-row share conflicts with configured share mapping"
                del shares[name]
        elif len(data["shares"]) > 1:
            share_reasons[name] = "conflicting per-row configured shares"
        elif name in run["share_map"]:
            shares[name] = run["share_map"][name]
        else:
            share_reasons[name] = "; ".join(sorted(data["share_sources"])) or "configured shares are missing"

    names = sorted(groups)
    run_complete = not errors and not custom_errors
    eligible_names = [name for name in names if name in measured] if run_complete else []
    total_cpu = sum(measured[name] for name in eligible_names)
    jain = jain_index([measured[name] for name in eligible_names])
    allocation_blockers = []
    if errors:
        allocation_blockers.extend(f"{name}: {reason}" for name, reason in sorted(errors.items()))
    allocation_blockers.extend(custom_errors)
    if run["share_map_errors"]:
        allocation_blockers.extend(sorted(set(run["share_map_errors"])))
    for name in names:
        if name not in shares:
            allocation_blockers.append(f"{name}: {share_reasons.get(name, 'configured share missing')}")
    if len(names) < 2:
        allocation_blockers.append("at least two groups are required for weighted allocation comparison")
    shares_total = sum(shares.values()) if len(shares) == len(names) else 0
    weighted_available = not allocation_blockers and shares_total > 0

    output = []
    for name in names:
        data = groups[name]
        valid = run_complete and name in measured
        observed = (100.0 * measured[name] / total_cpu
                    if valid and total_cpu > 0 else None)
        expected = (100.0 * shares[name] / shares_total
                    if weighted_available and name in shares else None)
        error = observed - expected if observed is not None and expected is not None else None
        if data["errors"]:
            measurement_reason = "; ".join(data["errors"])
        elif custom_errors:
            measurement_reason = "; ".join(custom_errors)
        elif not run_complete:
            measurement_reason = "run excluded because another group has an invalid or incomplete measurement"
        elif not data["measurement_rows"]:
            measurement_reason = "configured group has no measurement row"
        elif not valid:
            measurement_reason = "no valid CPU-time measurements"
        else:
            measurement_reason = ""
        allocation_reason = ""
        if not weighted_available:
            allocation_reason = "; ".join(sorted(set(allocation_blockers)))
        configured_share = shares.get(name)
        sources = data["share_sources"]
        share_source = ", ".join(sorted(sources)) if sources else ""
        if not share_source and name in run["share_map"]:
            share_source = ("control_setting cpu.weight"
                            if run["result_type"] == "cgroup_competition"
                            else "configured share mapping")
        output.append({
            "source_file": run["source_file"],
            "run_id": run["run_id"],
            "result_type": run["result_type"],
            "group": name,
            "configured_share": configured_share if configured_share is not None else "NA",
            "share_source": share_source,
            "measurement_count": len(data["cpu_times"]),
            "excluded_measurement_count": len(data["errors"]),
            "cpu_time_s": measured[name] if valid else "NA",
            "observed_cpu_pct": observed if observed is not None else "NA",
            "expected_cpu_pct": expected if expected is not None else "NA",
            "allocation_error_pct_points": error if error is not None else "NA",
            "absolute_allocation_error_pct_points": abs(error) if error is not None else "NA",
            "jain_fairness_index": jain if jain is not None else "NA",
            "jain_group_count": len(eligible_names),
            "measurement_exclusion_reason": measurement_reason,
            "allocation_unavailable_reason": allocation_reason,
        })
    return output


def write_csv(stream, rows):
    writer = csv.DictWriter(stream, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="group-competition or cgroup-competition CSV file(s)")
    parser.add_argument("-o", "--output", help="write to a new CSV path; existing files are never overwritten")
    args = parser.parse_args(argv)

    output_path = os.path.abspath(args.output) if args.output else None
    input_paths = [os.path.abspath(path) for path in args.inputs]
    if output_path:
        if output_path in input_paths:
            parser.error("output path must not be one of the input files")
        if os.path.exists(output_path):
            parser.error(f"output already exists; refusing to overwrite: {args.output}")

    runs, warnings = load_inputs(args.inputs)
    rows = [row for key in sorted(runs) for row in evaluate_run(runs[key])]
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    if not rows and not warnings:
        print("No compatible group measurements found.", file=sys.stderr)

    if output_path:
        try:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "x", newline="", encoding="utf-8") as stream:
                write_csv(stream, rows)
        except FileExistsError:
            parser.error(f"output already exists; refusing to overwrite: {args.output}")
        except OSError as exc:
            parser.error(f"could not write output {args.output}: {exc}")
    else:
        write_csv(sys.stdout, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
