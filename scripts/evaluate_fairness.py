#!/usr/bin/env python3
"""Compare observed per-group CPU allocation with configured relative shares."""

import argparse
import csv
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
    if fieldnames and GROUP_COLUMNS.issubset(fieldnames):
        return "group_competition"
    return None


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
                    })
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
    eligible_names = [name for name in names if name in measured]
    total_cpu = sum(measured[name] for name in eligible_names)
    jain = jain_index([measured[name] for name in eligible_names])
    allocation_blockers = []
    if errors:
        allocation_blockers.extend(f"{name}: {reason}" for name, reason in sorted(errors.items()))
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
        valid = name in measured
        observed = (100.0 * measured[name] / total_cpu
                    if valid and total_cpu > 0 else None)
        expected = (100.0 * shares[name] / shares_total
                    if weighted_available and name in shares else None)
        error = observed - expected if observed is not None and expected is not None else None
        if data["errors"]:
            measurement_reason = "; ".join(data["errors"])
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
