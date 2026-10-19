#!/usr/bin/env bash

set -Eeuo pipefail

usage() {
    printf 'Usage: %s [duration_seconds]\n' "${0##*/}" >&2
}

if (( $# > 1 )); then
    usage
    exit 2
fi

if (( $# == 0 )); then
    duration=3
else
    duration=$1
fi
if [[ ! $duration =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]] \
    || ! awk -v value="$duration" 'BEGIN { exit !(value + 0 > 0) }'; then
    printf 'Error: duration must be a positive number of seconds.\n' >&2
    usage
    exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
worker="$repo_root/workloads/cpu_worker"

if [[ ! -x $worker ]]; then
    printf 'Error: workload executable is missing or not executable: %s\n' "$worker" >&2
    printf 'Build it with: gcc -O2 -Wall -Wextra -std=c11 workloads/cpu_worker.c -o workloads/cpu_worker\n' >&2
    exit 1
fi

results_dir="$repo_root/results/raw"
mkdir -p -- "$results_dir"
timestamp=$(date '+%Y%m%dT%H%M%S.%N%z')
result_file=$(mktemp "$results_dir/cpu_baseline_${timestamp}_XXXXXX.txt")
printf -v command_display '%q %q' "$worker" "$duration"

{
    printf 'Date/time: %s\n' "$(date --iso-8601=seconds)"
    printf 'Kernel version: %s\n' "$(uname -r)"
    printf 'Logical CPUs: %s\n' "$(getconf _NPROCESSORS_ONLN)"
    printf 'Duration argument (seconds): %s\n' "$duration"
    printf 'Command: %s\n' "$command_display"
    printf '\nProgram output (stdout and stderr):\n'

    if "$worker" "$duration"; then
        worker_status=0
    else
        worker_status=$?
    fi

    printf '\nExit status: %s\n' "$worker_status"
} >"$result_file" 2>&1

printf 'Saved result: %s\n' "$result_file"
exit "$worker_status"
