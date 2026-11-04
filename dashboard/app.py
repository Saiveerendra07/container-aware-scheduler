#!/usr/bin/env python3
"""Read-only dashboard for existing Container-Aware Scheduler experiment data."""

import csv
import datetime as dt
import json
import math
import os
import re
from collections import defaultdict
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS_DIR = REPO_ROOT / "results"
SCHED_EXT_STATE = Path("/sys/kernel/sched_ext/state")
SCHED_EXT_OPS = Path("/sys/kernel/sched_ext/root/ops")
PROJECT_SCHEDULER_OPS = "grp_cpu"

GROUP_COLUMNS = {"run_id", "worker_id", "group", "cpu_time_s", "elapsed_s", "measurement_status"}
CGROUP_COLUMNS = {
    "run_id", "group", "workload_elapsed_s", "exit_status", "measurement_status",
    "cpu_stat_status", "usage_usec_before", "usage_usec_after",
}
CUSTOM_COLUMNS = {
    "run_id", "group", "cpu_time_s", "workload_elapsed_s", "group_share",
    "fallback_share", "scheduler_attached", "measurement_status",
    "completion_status", "scheduler_shutdown_status", "expected_group_config_json",
}
FIXTURE_MARKERS = {"test", "tests", "fixture", "fixtures", "synthetic", "test-fixtures"}
RESULT_METRICS = (
    ("iterations", "Iterations"),
    ("cpu_time_s", "CPU time (s)"),
    ("elapsed_s", "Elapsed time (s)"),
    ("throughput_per_s", "Throughput (iterations/s)"),
    ("cpu_usage_pct", "CPU usage (% of one CPU)"),
)


def _number(value):
    if value is None or not str(value).strip() or str(value).strip().upper() == "NA":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def jain_index(values):
    """Return Jain's equality index for at least two valid, nonnegative values."""
    clean = [_number(value) for value in values]
    if len(clean) < 2 or any(value is None for value in clean):
        return None
    total = sum(clean)
    squares = sum(value * value for value in clean)
    if squares == 0:
        return None
    return total * total / (len(clean) * squares)


def scheduler_status(state_path=SCHED_EXT_STATE, ops_path=SCHED_EXT_OPS):
    """Identify this scheduler from read-only kernel state, never from enabled alone."""
    try:
        state = Path(state_path).read_text(encoding="ascii").strip().lower()
    except (OSError, UnicodeError):
        return "Unknown"
    if state == "disabled":
        return "Not running"
    if state == "enabled":
        try:
            ops_name = Path(ops_path).read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return "Unknown"
        if not ops_name:
            return "Unknown"
        return "Running" if ops_name == PROJECT_SCHEDULER_OPS else "Not running"
    return "Unknown"


def _fixture_path(path, root):
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    for part in parts:
        tokens = set(filter(None, re.split(r"[^a-z0-9]+", part.lower())))
        if tokens & FIXTURE_MARKERS:
            return True
    return False


def _csv_schema(fields):
    names = set(fields or ())
    if CUSTOM_COLUMNS <= names:
        return "custom_scheduler"
    if CGROUP_COLUMNS <= names or (
        "cgroup_competition" in names and {"cpu_time_s", "elapsed_s", "group"} <= names
    ):
        return "cgroup_competition"
    if GROUP_COLUMNS <= names:
        return "group_competition"
    return None


def _read_csv(path):
    warnings = []
    try:
        with path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            schema = _csv_schema(reader.fieldnames)
            if schema is None:
                return None, [], [f"{path}: unsupported CSV schema; file listed but not analyzed"]
            rows = []
            for line_number, row in enumerate(reader, start=2):
                if None in row or any(value is None for value in row.values()):
                    warnings.append(f"{path}:{line_number}: malformed row skipped")
                    continue
                if not any(str(value).strip() for value in row.values()):
                    continue
                rows.append(row)
            return schema, rows, warnings
    except (OSError, UnicodeError, csv.Error) as exc:
        return None, [], [f"{path}: could not read CSV ({exc})"]


def _share_map(rows, schema):
    shares = {}
    if schema == "custom_scheduler":
        for row in rows:
            share = _number(row.get("group_share"))
            if share is not None and share > 0:
                shares[row.get("group", "")] = share
    elif schema == "cgroup_competition":
        for row in rows:
            setting = str(row.get("control_setting", "")).strip()
            if not setting.startswith("cpu.weight"):
                continue
            tokens = re.split(r"[;,\s]+", setting[len("cpu.weight"):].strip())
            parsed = {}
            for token in filter(None, tokens):
                if token.count("=") != 1:
                    parsed = {}
                    break
                name, raw_value = token.split("=", 1)
                value = _number(raw_value)
                if not name or value is None or value <= 0 or name in parsed:
                    parsed = {}
                    break
                parsed[name] = value
            shares.update(parsed)
    return shares


def _custom_configuration_valid(rows):
    if not rows:
        return False
    try:
        config = json.loads(rows[0].get("expected_group_config_json", ""))
    except (TypeError, json.JSONDecodeError):
        return False
    groups = config.get("groups") if isinstance(config, dict) else None
    if (not isinstance(groups, list) or not 2 <= len(groups) <= 8
            or type(config.get("expected_group_count")) is not int
            or config["expected_group_count"] != len(groups)
            or type(config.get("fallback_share")) is not int
            or not 1 <= config["fallback_share"] <= 10000):
        return False
    configured = {}
    for group in groups:
        if not isinstance(group, dict):
            return False
        name, share = group.get("name"), group.get("share")
        if (not isinstance(name, str) or not name
                or type(group.get("cgroup_id")) is not int or group["cgroup_id"] <= 0
                or type(share) is not int or not 1 <= share <= 10000
                or not isinstance(group.get("cgroup_path"), str) or not group["cgroup_path"]):
            return False
        configured[name] = group
    row_names = [row.get("group", "") for row in rows]
    if (len(configured) != len(groups) or len(rows) != len(groups)
            or len(set(row_names)) != len(row_names) or set(row_names) != set(configured)):
        return False
    for row in rows:
        try:
            row_config = json.loads(row.get("expected_group_config_json", ""))
        except (TypeError, json.JSONDecodeError):
            return False
        if row_config != config:
            return False
        group = configured[row.get("group", "")]
        if (str(row.get("custom_scheduler_schema_version", "")) != "2"
                or str(row.get("scheduler_attached", "")).lower() != "true"
                or row.get("completion_status") != "complete"
                or row.get("scheduler_shutdown_status") != "stopped"
                or row.get("identity_verification_status") != "verified"
                or str(row.get("expected_group_count", "")) != str(len(groups))
                or str(row.get("fallback_share", "")) != str(config.get("fallback_share", ""))
                or str(row.get("cgroup_id", "")) != str(group.get("cgroup_id", ""))
                or str(row.get("verified_cgroup_id", "")) != str(group.get("cgroup_id", ""))
                or row.get("cgroup_path") != group.get("cgroup_path")
                or _number(row.get("group_share")) != group.get("share")):
            return False
    return True


def _group_rows_complete(rows):
    raw_counts = str(rows[0].get("group_counts", "")).strip() if rows else ""
    if not raw_counts:
        return True
    expected = {}
    for token in raw_counts.split(";"):
        if token.count("=") != 1:
            return False
        name, raw_count = token.split("=", 1)
        if (not name or not raw_count.isascii() or not raw_count.isdecimal()
                or name in expected):
            return False
        expected[name] = int(raw_count)
    actual = defaultdict(int)
    for row in rows:
        actual[str(row.get("group", ""))] += 1
    return bool(expected) and actual == expected


def _row_measurement(row, schema):
    if str(row.get("measurement_status", "")).strip().lower() != "complete":
        return None
    if schema == "custom_scheduler":
        if (str(row.get("scheduler_attached", "")).lower() != "true"
                or row.get("completion_status") != "complete"
                or row.get("scheduler_shutdown_status") != "stopped"
                or str(row.get("exit_status", "")) != "0"
                or str(row.get("termination_signal", "")) != "0"):
            return None
    elif schema == "cgroup_competition":
        if (str(row.get("exit_status", "")) != "0"
                or str(row.get("cpu_stat_status", "")).lower() != "complete"):
            return None
    elif str(row.get("exit_status", "0")) not in ("", "NA", "0"):
        return None

    cpu_time = None
    if schema == "cgroup_competition":
        before = _number(row.get("usage_usec_before"))
        after = _number(row.get("usage_usec_after"))
        if before is not None and after is not None and after >= before:
            cpu_time = (after - before) / 1_000_000
    if cpu_time is None:
        cpu_time = _number(row.get("cpu_time_s"))
    elapsed = _number(row.get("workload_elapsed_s"))
    if elapsed is None:
        elapsed = _number(row.get("elapsed_s"))
    if cpu_time is None or elapsed is None or elapsed <= 0:
        return None
    return {
        "cpu_time_s": cpu_time,
        "elapsed_s": elapsed,
        "iterations": _number(row.get("iterations")),
    }


def _timestamp(rows, manifest, path):
    values = []
    for row in rows:
        value = row.get("timestamp_utc") or row.get("created_utc")
        parsed = _parse_timestamp(value)
        if parsed:
            values.append(parsed)
    if manifest:
        parsed = _parse_timestamp(manifest.get("created_utc") or manifest.get("timestamp_utc"))
        if parsed:
            values.append(parsed)
    if values:
        return max(values).isoformat(timespec="seconds")
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return ""


def _parse_timestamp(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _experiment(run_id, schema, rows, path, root):
    valid_rows = [_row_measurement(row, schema) for row in rows]
    complete = bool(rows) and all(measurement is not None for measurement in valid_rows)
    if schema == "group_competition" and not _group_rows_complete(rows):
        complete = False
    if schema == "custom_scheduler":
        complete = complete and _custom_configuration_valid(rows)
    groups = sorted({str(row.get("group", "")).strip() for row in rows if str(row.get("group", "")).strip()})
    shares = _share_map(rows, schema)
    group_values = []
    total_cpu = 0.0
    elapsed_values = []
    iteration_values = []
    if complete:
        by_group = defaultdict(float)
        for row, measurement in zip(rows, valid_rows):
            name = str(row.get("group", "")).strip()
            by_group[name] += measurement["cpu_time_s"]
            total_cpu += measurement["cpu_time_s"]
            elapsed_values.append(measurement["elapsed_s"])
            if measurement["iterations"] is not None:
                iteration_values.append(measurement["iterations"])
        measured_total = sum(by_group.values())
        share_total = sum(shares.values()) if shares and set(shares) == set(groups) else 0
        for name in groups:
            cpu = by_group[name]
            observed = 100 * cpu / measured_total if measured_total > 0 else None
            expected = 100 * shares[name] / share_total if share_total > 0 else None
            group_values.append({
                "group": name,
                "configured_share": shares.get(name),
                "expected_allocation_pct": expected,
                "cpu_time_s": cpu,
                "observed_allocation_pct": observed,
                "allocation_deviation_pct_points": observed - expected if observed is not None and expected is not None else None,
            })

    elapsed = max(elapsed_values) if complete and elapsed_values else None
    iterations = sum(iteration_values) if complete and len(iteration_values) == len(rows) else None
    metrics = {}
    if complete:
        metrics["cpu_time_s"] = total_cpu
        metrics["elapsed_s"] = elapsed
        if iterations is not None and elapsed and elapsed > 0:
            metrics["throughput_per_s"] = iterations / elapsed
        if elapsed and elapsed > 0:
            metrics["cpu_usage_pct"] = 100 * total_cpu / elapsed
        if schema != "cgroup_competition":
            fairness = jain_index([item["cpu_time_s"] for item in group_values])
            if fairness is not None:
                metrics["jain_fairness_index"] = fairness

    return {
        "run_id": run_id,
        "result_type": schema,
        "experiment_type": {
            "group_competition": "Default Linux group",
            "cgroup_competition": "Cgroup controls (default Linux)",
            "custom_scheduler": "Custom sched_ext",
        }.get(schema, schema),
        "status": "complete" if complete else "incomplete or unavailable",
        "timestamp": _timestamp(rows, None, path),
        "groups": group_values,
        "group_names": groups,
        "configured_shares": shares,
        "metrics": metrics,
        "source_file": str(path),
        "relative_path": str(path.relative_to(root)) if path.is_relative_to(root) else str(path),
        "source_kind": "test fixture" if _fixture_path(path, root) else "experiment data",
        "row_count": len(rows),
        "raw_rows": rows,
    }


def _read_text_run(path, root):
    """Parse only the fixed text formats emitted by the baseline/competition runners."""
    if path.name.startswith("cpu_baseline_"):
        schema = "cpu_baseline"
        experiment_type = "Baseline (default Linux)"
    elif path.name.startswith("cpu_competition_"):
        schema = "cpu_competition"
        experiment_type = "CPU competition (default Linux)"
    else:
        return None, None

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"{path}: could not read text result ({exc})"

    def captured(pattern):
        match = re.search(pattern, text, re.MULTILINE)
        return match.group(1).strip() if match else None

    run_id = path.stem
    iterations = [int(value) for value in re.findall(r"^iterations=([0-9]+)\s*$", text, re.MULTILINE)]
    statuses = [int(value) for value in re.findall(r"^Worker\s+[0-9]+\s+exit status:\s*([0-9]+)\s*$", text, re.MULTILINE)]
    metrics = {}
    timestamp = captured(r"^(?:Date/time|Timestamp):\s*(.+)$") or ""
    total_iterations = sum(iterations) if iterations else None
    if schema == "cpu_baseline":
        status_value = captured(r"^Exit status:\s*([0-9]+)\s*$")
        complete = status_value == "0" and len(iterations) == 1
        if complete:
            metrics["iterations"] = total_iterations
        status = "complete" if complete else "incomplete or unavailable"
    else:
        requested = captured(r"^Requested workers:\s*([0-9]+)\s*$")
        started = captured(r"^Workers started:\s*([0-9]+)\s*$")
        elapsed = _number(captured(r"^Total elapsed time \(monotonic, seconds\):\s*([0-9]+(?:\.[0-9]+)?)\s*$"))
        expected = int(started) if started and started.isascii() and started.isdecimal() else None
        complete = (requested is not None and started == requested and expected is not None and expected > 0
                    and len(iterations) == expected and len(statuses) == expected
                    and all(code == 0 for code in statuses) and elapsed is not None and elapsed > 0)
        if complete:
            metrics["iterations"] = total_iterations
            metrics["elapsed_s"] = elapsed
            metrics["throughput_per_s"] = total_iterations / elapsed
        status = "complete" if complete else "incomplete or unavailable"

    parsed_timestamp = _parse_timestamp(timestamp)
    normalized_timestamp = parsed_timestamp.isoformat(timespec="seconds") if parsed_timestamp else ""
    experiment = {
        "run_id": run_id, "result_type": schema, "experiment_type": experiment_type,
        "status": status, "timestamp": normalized_timestamp, "groups": [], "group_names": [],
        "configured_shares": {}, "metrics": metrics, "source_file": str(path),
        "relative_path": str(path.relative_to(root)) if path.is_relative_to(root) else str(path),
        "source_kind": "test fixture" if _fixture_path(path, root) else "experiment data",
        "row_count": len(iterations), "raw_rows": [], "text_preview": text,
    }
    return experiment, None


def _manifest_record(path, root):
    warnings = []
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(manifest, dict):
            raise ValueError("JSON root is not an object")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return None, {"kind": "JSON manifest", "status": "malformed", "timestamp": "",
                      "run_id": "", "experiment_type": "Unknown", "metrics": "None",
                      "relative_path": str(path.relative_to(root)), "source_kind": "experiment data",
                      "path": str(path)}, [f"{path}: could not read JSON manifest ({exc})"]

    mode = str(manifest.get("mode", "unknown"))
    run_id = str(manifest.get("run_id", ""))
    status = str(manifest.get("measurement_status", "unknown"))
    if mode == "prepare":
        history_status = "prepared; not run"
    elif mode == "run":
        history_status = status
    else:
        history_status = status if status != "unknown" else f"manifest ({mode})"
    groups = manifest.get("scheduler_groups", [])
    group_names = [group.get("name", "") for group in groups if isinstance(group, dict)] if isinstance(groups, list) else []
    timestamp = _timestamp([], manifest, path)
    source_kind = "test fixture" if _fixture_path(path, root) else "experiment data"
    history = {
        "kind": "JSON manifest", "status": history_status, "timestamp": timestamp,
        "run_id": run_id, "experiment_type": "Custom sched_ext", "metrics": "No measurements in manifest",
        "relative_path": str(path.relative_to(root)) if path.is_relative_to(root) else str(path),
        "source_kind": source_kind, "path": str(path), "manifest": manifest,
    }
    run = None
    if mode == "run" and run_id:
        run = {
            "run_id": run_id, "result_type": "custom_scheduler", "experiment_type": "Custom sched_ext",
            "status": history_status, "timestamp": timestamp, "groups": [], "group_names": group_names,
            "configured_shares": {
                group.get("name"): group.get("configured_scheduler_share")
                for group in groups if isinstance(group, dict) and group.get("name")
            } if isinstance(groups, list) else {},
            "metrics": {}, "source_file": str(path), "relative_path": history["relative_path"],
            "source_kind": source_kind, "row_count": 0, "raw_rows": [], "manifest": manifest,
        }
    return run, history, warnings


def load_experiments(results_dir=DEFAULT_RESULTS_DIR, include_fixtures=False):
    """Load supported CSV runs and JSON manifests without modifying source files."""
    root = Path(results_dir).expanduser().resolve()
    data = {"experiments": [], "history": [], "warnings": [], "fixture_count": 0, "results_dir": str(root)}
    if not root.exists():
        data["warnings"].append(f"Results directory does not exist: {root}")
        return data
    if not root.is_dir():
        data["warnings"].append(f"Results path is not a directory: {root}")
        return data

    paths = sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in {".csv", ".json", ".txt"})
    selected = []
    for path in paths:
        if _fixture_path(path, root) and not include_fixtures:
            data["fixture_count"] += 1
        else:
            selected.append(path)

    runs_by_key = {}
    run_priority = {}
    for path in selected:
        if path.suffix.lower() == ".csv":
            schema, rows, warnings = _read_csv(path)
            data["warnings"].extend(warnings)
            if schema is None:
                data["history"].append({
                    "kind": "CSV result", "status": "unsupported", "timestamp": "",
                    "run_id": "", "experiment_type": "Unknown", "metrics": "Unavailable",
                    "relative_path": str(path.relative_to(root)),
                    "source_kind": "test fixture" if _fixture_path(path, root) else "experiment data",
                    "path": str(path),
                })
                continue
            by_run = defaultdict(list)
            for row in rows:
                by_run[str(row.get("run_id", "")).strip() or f"{path.name}:unidentified"].append(row)
            if not by_run:
                data["history"].append({
                    "kind": "CSV result", "status": "empty", "timestamp": "",
                    "run_id": "", "experiment_type": schema, "metrics": "None",
                    "relative_path": str(path.relative_to(root)),
                    "source_kind": "test fixture" if _fixture_path(path, root) else "experiment data",
                    "path": str(path),
                })
            for run_id, run_rows in by_run.items():
                experiment = _experiment(run_id, schema, run_rows, path, root)
                key = (schema, run_id)
                priority = 1 if "processed" in path.parts else 0
                if key not in runs_by_key or priority < run_priority[key]:
                    runs_by_key[key] = experiment
                    run_priority[key] = priority
                metric_names = ", ".join(experiment["metrics"].keys()) or "None"
                data["history"].append({
                    "kind": "CSV result", "status": experiment["status"],
                    "timestamp": experiment["timestamp"], "run_id": run_id,
                    "experiment_type": experiment["experiment_type"], "metrics": metric_names,
                    "relative_path": experiment["relative_path"], "source_kind": experiment["source_kind"],
                    "path": str(path), "experiment_key": key,
                })
        elif path.suffix.lower() == ".json":
            run, history, warnings = _manifest_record(path, root)
            data["warnings"].extend(warnings)
            data["history"].append(history)
            if run is not None:
                key = ("custom_scheduler", run["run_id"])
                if key not in runs_by_key:
                    runs_by_key[key] = run
                    run_priority[key] = 2
        else:
            experiment, warning = _read_text_run(path, root)
            if warning:
                data["warnings"].append(warning)
            if experiment is not None:
                key = (experiment["result_type"], experiment["run_id"])
                runs_by_key[key] = experiment
                data["history"].append({
                    "kind": "Text result", "status": experiment["status"],
                    "timestamp": experiment["timestamp"], "run_id": experiment["run_id"],
                    "experiment_type": experiment["experiment_type"],
                    "metrics": ", ".join(experiment["metrics"].keys()) or "None",
                    "relative_path": experiment["relative_path"], "source_kind": experiment["source_kind"],
                    "path": str(path), "experiment_key": key,
                })

    data["experiments"] = sorted(
        runs_by_key.values(), key=lambda run: (_parse_timestamp(run["timestamp"]) or dt.datetime.min.replace(tzinfo=dt.timezone.utc), run["run_id"]),
    )
    data["history"].sort(key=lambda item: (item.get("timestamp", ""), item.get("relative_path", "")), reverse=True)
    return data


def _metric_label(metric):
    return dict(RESULT_METRICS).get(metric, metric)


def main():
    import pandas as pd
    import plotly.graph_objects as go
    import streamlit as st

    st.set_page_config(page_title="Container-Aware Group CPU Scheduler", layout="wide")
    st.markdown("""
    <style>
    .stApp { background: #101719; color: #e8efed; }
    [data-testid="stSidebar"] { background: #172124; border-right: 1px solid #2a393a; }
    [data-testid="stMetric"] { background: #172124; border: 1px solid #2a393a; padding: 14px; border-radius: 6px; }
    div[data-testid="stVerticalBlock"] > div:has(> div[data-testid="stMarkdownContainer"] h2) { border-top: 1px solid #2a393a; padding-top: 10px; }
    .stCaption, [data-testid="stCaptionContainer"] { color: #a4b4b1; }
    </style>
    """, unsafe_allow_html=True)
    st.title("CONTAINER-AWARE GROUP CPU SCHEDULER")
    st.caption("Read-only view of repository experiment records. Measurements are never inferred from missing fields.")

    with st.sidebar:
        st.subheader("Data source")
        results_dir = st.text_input("Results directory", value=str(DEFAULT_RESULTS_DIR))
        include_fixtures = st.checkbox("Include paths marked as test fixtures", value=False)
        st.caption("Default Linux group and cgroup runs do not demonstrate custom sched_ext behavior.")

    data = load_experiments(results_dir, include_fixtures=include_fixtures)
    experiments = data["experiments"]
    real_experiments = [run for run in experiments if run["source_kind"] == "experiment data"]
    latest = max((run["timestamp"] for run in real_experiments if run["timestamp"]), default="")
    detected_groups = sorted({name for run in real_experiments for name in run["group_names"]})
    measured_count = sum(bool(run["metrics"]) for run in real_experiments)
    if measured_count:
        availability = f"Available ({measured_count} measured run{'s' if measured_count != 1 else ''})"
    elif any(item["kind"] in ("CSV result", "JSON manifest") for item in data["history"]):
        availability = "Files found; no valid measurements"
    else:
        availability = "No real experiment results"

    st.header("Project Overview")
    st.write("This project explores CPU accounting and weighted sharing between workload groups. The custom Linux sched_ext/eBPF scheduler is experimental and has not been runtime-validated; ordinary and cgroup runs use the active default Linux scheduler.")
    overview = st.columns(5)
    overview[0].metric("Scheduler status", scheduler_status())
    overview[1].metric("Latest experiment", latest[:19].replace("T", " ") if latest else "None detected")
    overview[2].metric("Experiment runs", len(real_experiments))
    overview[3].metric("Workload groups", len(detected_groups))
    overview[4].metric("Data availability", availability)
    if not real_experiments:
        st.info("No real experiment CSV results or run manifests are available in this directory. No measurements are shown. Prepared manifests are not counted as runs.")
    if data["fixture_count"] and not include_fixtures:
        st.warning(f"Excluded {data['fixture_count']} file(s) in paths marked as test fixtures. They are not included in project metrics.")

    choices = [run for run in experiments if run["source_kind"] == "experiment data"]
    selected = None
    with st.sidebar:
        if choices:
            choice_labels = [f"{run['run_id']} | {run['experiment_type']} | {run['status']}" for run in choices]
            chosen_label = st.selectbox("Experiment detail", choice_labels, index=len(choice_labels) - 1)
            selected = choices[choice_labels.index(chosen_label)]
        else:
            st.selectbox("Experiment detail", ["No experiment runs detected"], disabled=True)

    st.header("CPU Allocation")
    if selected and selected["groups"]:
        group_frame = pd.DataFrame(selected["groups"])
        display_frame = group_frame.rename(columns={
            "group": "Group", "configured_share": "Configured weight/share",
            "expected_allocation_pct": "Expected allocation (%)", "cpu_time_s": "Measured CPU time (s)",
            "observed_allocation_pct": "Measured allocation (%)",
            "allocation_deviation_pct_points": "Deviation (percentage points)",
        })
        st.dataframe(display_frame, hide_index=True, use_container_width=True)
        chart = go.Figure()
        plotted = False
        for field, label, color in (
            ("expected_allocation_pct", "Configured allocation", "#e69a72"),
            ("observed_allocation_pct", "Measured allocation", "#55c9ad"),
        ):
            values = group_frame[field].tolist()
            if any(value is not None for value in values):
                chart.add_bar(name=label, x=values, y=group_frame["group"], orientation="h", marker_color=color)
                plotted = True
        if plotted:
            chart.update_layout(barmode="group", xaxis_title="Share of configured/measured group total (%)",
                                yaxis_title="", paper_bgcolor="#101719", plot_bgcolor="#101719",
                                font_color="#e8efed", legend_title="", margin=dict(l=12, r=12, t=10, b=24))
            st.plotly_chart(chart, use_container_width=True)
        if not selected["configured_shares"]:
            st.info("Configured weights are not recorded for this CSV schema. Group counts are workload metadata, not CPU weights; only measured CPU allocation is shown.")
        elif not selected["metrics"]:
            st.info("Configured shares are available, but this run does not have a complete set of valid measurements for comparison.")
        else:
            st.caption("Expected percentages normalize recorded group weights among the listed groups. A custom scheduler's fallback share is not included in that denominator. These are measurements, not proof of policy effectiveness.")
    elif selected:
        st.info("This run has no complete group-level measurement set. Configured shares, if present, are metadata only; missing measurements are not imputed.")
    else:
        st.info("Group allocation will appear when supported experiment CSV results are available.")

    st.header("Experiment Comparison")
    measured_runs = [run for run in real_experiments if run["metrics"]]
    metric_keys = [key for key, _label in RESULT_METRICS if any(key in run["metrics"] for run in measured_runs)]
    if metric_keys:
        metric = st.selectbox("Metric", metric_keys, format_func=_metric_label)
        chart = go.Figure()
        for experiment_type in sorted({run["experiment_type"] for run in measured_runs if metric in run["metrics"]}):
            subset = [run for run in measured_runs if run["experiment_type"] == experiment_type and metric in run["metrics"]]
            chart.add_bar(name=experiment_type, x=[run["run_id"] for run in subset], y=[run["metrics"][metric] for run in subset])
        chart.update_layout(barmode="group", xaxis_title="Run identifier", yaxis_title=_metric_label(metric),
                            paper_bgcolor="#101719", plot_bgcolor="#101719", font_color="#e8efed",
                            legend_title="Experiment type", margin=dict(l=12, r=12, t=10, b=24))
        st.plotly_chart(chart, use_container_width=True)
        st.caption("Only complete runs with this recorded or safely derived metric are plotted. Runs may use different workloads or durations; compare like-for-like configurations.")
    else:
        st.info("No valid structured experiment metrics are available to compare.")
    st.caption("Baseline and CPU-competition text records expose iterations; competition throughput uses the recorded total elapsed time. CPU time is not available in those files. Cgroup records are labeled as default-Linux cgroup controls; custom sched_ext results require confirmed attachment metadata.")

    st.header("Fairness Analysis")
    fairness = selected["metrics"].get("jain_fairness_index") if selected else None
    if fairness is not None:
        st.metric("Jain's fairness index", f"{fairness:.4f}")
        st.write("Calculated from complete, nonnegative group CPU-time totals: $(\\sum x_i)^2 / (n\\sum x_i^2)$. Values approach 1 when group CPU times are equal.")
        st.caption("This is equality across measured groups, not share-weighted fairness, latency fairness, or evidence that sched_ext was active. At least two valid groups are required.")
    else:
        st.info("Jain's group-level index is unavailable: it requires at least two groups with a complete valid measurement set and nonzero CPU time.")
    if selected and selected["groups"]:
        deviations = [{"Group": item["group"], "Measured allocation (%)": item["observed_allocation_pct"],
                       "Expected allocation (%)": item["expected_allocation_pct"],
                       "Deviation (percentage points)": item["allocation_deviation_pct_points"]}
                      for item in selected["groups"]]
        st.dataframe(pd.DataFrame(deviations), hide_index=True, use_container_width=True)
        if not selected["configured_shares"]:
            st.caption("Expected allocation and weighted deviations are unavailable because this result schema does not record configured shares.")

    st.header("Experiment History")
    if data["history"]:
        history_rows = [{key: item.get(key) for key in (
            "kind", "experiment_type", "run_id", "timestamp", "status", "metrics", "source_kind", "relative_path",
        )} for item in data["history"]]
        st.dataframe(pd.DataFrame(history_rows), hide_index=True, use_container_width=True)
        history_labels = [f"{item['kind']} | {item.get('run_id') or '(no run id)'} | {item['relative_path']}" for item in data["history"]]
        selected_history_label = st.selectbox("Inspect a result or manifest", history_labels)
        history_item = data["history"][history_labels.index(selected_history_label)]
        st.caption(f"Status: {history_item['status']} · Source: {history_item['source_kind']} · Timestamp: {history_item['timestamp'] or 'unavailable'}")
        matching = next((run for run in experiments if run["source_file"] == history_item.get("path")
                         and (not history_item.get("run_id") or run["run_id"] == history_item["run_id"])), None)
        if history_item.get("manifest") is not None:
            st.json(history_item["manifest"])
        elif matching:
            st.write(f"{matching['row_count']} row(s); metrics: {', '.join(matching['metrics']) or 'none'}")
            if matching["raw_rows"]:
                st.dataframe(pd.DataFrame(matching["raw_rows"]), hide_index=True, use_container_width=True)
            elif matching.get("text_preview"):
                st.code(matching["text_preview"], language="text")
        else:
            st.info("This file is listed in history but does not contain a supported complete result schema.")
    else:
        st.info("No CSV results or JSON manifests were found in the configured directory.")

    if data["warnings"]:
        with st.expander(f"Data warnings ({len(data['warnings'])})"):
            for warning in data["warnings"]:
                st.write(f"- {warning}")


if __name__ == "__main__":
    main()