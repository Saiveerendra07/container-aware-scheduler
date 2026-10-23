#!/usr/bin/env python3
"""Build cgroup experiment CSV rows from captured metadata and cpu.stat files."""

import argparse
import csv
import os


COUNTERS = (
    "usage_usec", "user_usec", "system_usec", "nr_periods",
    "nr_throttled", "throttled_usec",
)
METADATA_COLUMNS = (
    "run_id", "timestamp_utc", "kernel_version", "logical_cpu_count",
    "requested_duration_s", "control_setting", "group", "cgroup_path",
    "workload_elapsed_s", "exit_status", "measurement_status",
)
COLUMNS = METADATA_COLUMNS + ("cpu_stat_status",) + tuple(
    f"{counter}_{phase}" for counter in COUNTERS
    for phase in ("before", "after", "delta")
)


def parse_cpu_stat(text):
    """Parse known nonnegative integer counters; malformed/duplicate keys are unavailable."""
    values = {}
    invalid = set()
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] not in COUNTERS:
            continue
        key = fields[0]
        if len(fields) != 2:
            values.pop(key, None)
            invalid.add(key)
            continue
        raw = fields[1]
        if (key in values or key in invalid or not raw.isascii()
                or not raw.isdecimal() or len(raw) > 20):
            values.pop(key, None)
            invalid.add(key)
            continue
        value = int(raw)
        if value > 18446744073709551615:
            values.pop(key, None)
            invalid.add(key)
            continue
        values[key] = value
    return values


def read_cpu_stat(path):
    if not path or path == "NA":
        return {}
    try:
        with open(path, encoding="ascii") as stream:
            return parse_cpu_stat(stream.read())
    except (OSError, UnicodeError):
        return {}


def counter_delta(before, after, key):
    first = before.get(key)
    last = after.get(key)
    if first is None or last is None or last < first:
        return None
    return last - first


def row_from_metadata(metadata):
    before = read_cpu_stat(metadata[11])
    after = read_cpu_stat(metadata[12])
    row = dict(zip(METADATA_COLUMNS, metadata[:11]))
    available = 0
    for counter in COUNTERS:
        first = before.get(counter)
        last = after.get(counter)
        delta = counter_delta(before, after, counter)
        for phase, value in (("before", first), ("after", last), ("delta", delta)):
            row[f"{counter}_{phase}"] = "NA" if value is None else str(value)
        if delta is not None:
            available += 1
    row["cpu_stat_status"] = (
        "complete" if available == len(COUNTERS)
        else "partial" if available
        else "unavailable"
    )
    return row


def write_csv(metadata_path, output_path):
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(metadata_path, newline="", encoding="utf-8") as source, \
            open(output_path, "w", newline="", encoding="utf-8") as target:
        reader = csv.reader(source, delimiter="\t")
        writer = csv.DictWriter(target, fieldnames=COLUMNS)
        writer.writeheader()
        for line_number, metadata in enumerate(reader, start=1):
            if len(metadata) != 13:
                raise ValueError(f"metadata row {line_number} has {len(metadata)} columns; expected 13")
            writer.writerow(row_from_metadata(metadata))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata_tsv")
    parser.add_argument("output_csv")
    args = parser.parse_args()
    write_csv(args.metadata_tsv, args.output_csv)


if __name__ == "__main__":
    main()
