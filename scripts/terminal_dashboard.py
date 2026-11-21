#!/usr/bin/env python3
"""Interactive terminal dashboard for Container-Aware Scheduler results."""

import argparse
import os
import platform
import select
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dashboard.app import jain_index, load_experiments, scheduler_status  # noqa: E402


def weighted_share_compliance(groups, tolerance_pct=5.0):
    """Return maximum absolute deviation and pass status for configured shares."""
    deviations = []
    for group in groups:
        observed = group.get("observed_allocation_pct")
        expected = group.get("expected_allocation_pct")
        if observed is None or expected is None:
            continue
        deviations.append(abs(observed - expected))
    if not deviations:
        return None, "NOT TESTED"
    worst = max(deviations)
    status = "PASS" if worst <= tolerance_pct else ("WARNING" if worst <= tolerance_pct * 2 else "FAIL")
    return worst, status


def system_snapshot():
    """Read current system information without requiring privileged access."""
    snapshot = {
        "kernel": platform.uname().release,
        "architecture": platform.machine(),
        "logical_cpus": os.cpu_count() or 0,
        "load_average": tuple(os.getloadavg()),
        "memory": {},
    }
    try:
        values = {}
        with open("/proc/meminfo", encoding="ascii") as stream:
            for line in stream:
                name, _, value = line.partition(":")
                if name in {"MemTotal", "MemAvailable", "MemFree", "Buffers", "Cached"}:
                    values[name] = int(value.strip().split()[0]) * 1024
        snapshot["memory"] = values
    except (OSError, UnicodeError, ValueError):
        pass
    try:
        snapshot["cpu_usage_pct"] = _cpu_usage_snapshot()
    except (OSError, ValueError):
        snapshot["cpu_usage_pct"] = None
    return snapshot


def _cpu_usage_snapshot():
    """Read total CPU utilization by comparing two /proc/stat samples."""
    def sample():
        with open("/proc/stat", encoding="ascii") as stream:
            fields = stream.readline().split()
        values = [int(value) for value in fields[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

    total_before, idle_before = sample()
    time.sleep(0.05)
    total_after, idle_after = sample()
    total_delta = total_after - total_before
    if total_delta <= 0:
        return None
    return 100 * (total_delta - (idle_after - idle_before)) / total_delta


def memory_gib(snapshot):
    memory = snapshot.get("memory", {})
    total = memory.get("MemTotal")
    available = memory.get("MemAvailable", memory.get("MemFree"))
    if not total:
        return "unavailable"
    return f"{available / (1024 ** 3):.1f} GiB available / {total / (1024 ** 3):.1f} GiB total"


def format_table(rows, headers):
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(str(value)))
    separator = "-+-".join("-" * width for width in widths)
    def format_row(row):
        return " | ".join(str(value).ljust(widths[index]) for index, value in enumerate(row))
    return "\n".join((format_row(headers), separator, *[format_row(row) for row in rows]))


def latest_experiment(data):
    experiments = data.get("experiments", [])
    if not experiments:
        return None
    return experiments[-1]


def render_dashboard(results_dir=REPO_ROOT / "results", tolerance_pct=5.0, once=False):
    data = load_experiments(results_dir)
    system = system_snapshot()
    run = latest_experiment(data)

    lines = [
        "=" * 78,
        "CONTAINER-AWARE SCHEDULER",
        "=" * 78,
        "Project status",
        f"  Kernel: {system['kernel']}",
        f"  Architecture: {system['architecture']} | Logical CPUs: {system['logical_cpus']}",
        f"  Scheduler: {scheduler_status()}",
        f"  CPU usage: {system['cpu_usage_pct']:.1f}%" if system["cpu_usage_pct"] is not None else "  CPU usage: unavailable",
        f"  Load average: " + ", ".join(f"{value:.2f}" for value in system["load_average"]),
        f"  Memory: {memory_gib(system)}",
    ]

    if run is None:
        lines.extend(["", "Experiment summary", "  No experiment data available.", "  Recent result files may be missing or malformed."])
    else:
        lines.extend([
            "",
            "Experiment summary",
            f"  ID: {run['run_id']}",
            f"  Timestamp: {run['timestamp'] or 'unknown'}",
            f"  Type: {run['experiment_type']}",
            f"  Status: {run['status'].upper()}",
        ])
        rows = []
        for group in run["groups"]:
            share = group["configured_share"]
            expected = group["expected_allocation_pct"]
            observed = group["observed_allocation_pct"]
            deviation = group["allocation_deviation_pct_points"]
            worst_status = "NOT TESTED"
            if expected is not None and observed is not None:
                worst_status = "PASS" if abs(deviation or 0) <= tolerance_pct else (
                    "WARNING" if abs(deviation or 0) <= tolerance_pct * 2 else "FAIL")
            rows.append((
                group["group"],
                "N/A" if share is None else str(share),
                "N/A" if expected is None else f"{expected:.2f}",
                "N/A" if observed is None else f"{observed:.2f}",
                "N/A" if deviation is None else f"{deviation:+.2f}",
                worst_status,
            ))
        lines.extend(["", "Expected versus actual",
                      format_table(rows, ("Group", "Share", "Expected %", "Actual %", "Error", "Status"))])
        worst, compliance = weighted_share_compliance(run["groups"], tolerance_pct)
        lines.append(
            f"  Weighted-share compliance: {compliance}"
            + (f" (worst deviation {worst:.2f} percentage points)" if worst is not None else "")
        )
        fairness = run["metrics"].get("jain_fairness_index")
        lines.append(f"  Jain fairness index: {fairness:.4f}" if fairness is not None else "  Jain fairness index: N/A")

    lines.extend(["", "Controls: r refresh | h history | d details | q quit"])
    if data.get("warnings"):
        lines.extend(["", "Warnings"])
        lines.extend(f"  {warning}" for warning in data["warnings"][-5:])
    return "\n".join(lines)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, default=REPO_ROOT / "results")
    parser.add_argument("--refresh", type=float, default=2.0, help="live refresh interval in seconds")
    parser.add_argument("--tolerance", type=float, default=5.0, help="weighted-share tolerance in percentage points")
    parser.add_argument("--once", action="store_true", help="render once and exit")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.once:
        print(render_dashboard(args.results_dir, args.tolerance, True))
        return 0

    selected = args.results_dir
    try:
        while True:
            os.system("clear")
            print(render_dashboard(selected, args.tolerance), flush=True)
            ready, _, _ = select.select([sys.stdin], [], [], args.refresh)
            if ready:
                key = sys.stdin.readline().strip().lower()
                if key == "q":
                    return 0
                if key == "h":
                    data = load_experiments(selected)
                    print("\nExperiment history")
                    history_rows = [
                        (run["run_id"], run["timestamp"] or "unknown", run["experiment_type"], run["status"])
                        for run in data["experiments"][-20:]
                    ]
                    print(format_table(history_rows, ("ID", "Timestamp", "Type", "Status")))
                    input("\nPress Enter to return...")
                elif key == "d":
                    data = load_experiments(selected)
                    run = latest_experiment(data)
                    if run:
                        print("\nDetailed group results")
                        for group in run["groups"]:
                            print(group)
                    input("\nPress Enter to return...")
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
