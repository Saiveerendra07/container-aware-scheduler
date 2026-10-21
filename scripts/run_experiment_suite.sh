#!/usr/bin/env bash

set -Eeuo pipefail

usage() {
    cat <<'EOF'
Usage: run_experiment_suite.sh [options]

Runs the existing CPU baseline and CPU competition runners. These experiments
use the active default Linux scheduler; they do not load the custom scheduler.

Options:
  --baseline-duration SEC       Baseline worker duration (default: 3)
  --competition-workers COUNT   Competing workers, 1..32 (default: 2)
  --competition-duration SEC   Duration per competing worker (default: 3)
  --repetitions COUNT           Number of baseline/competition pairs (default: 1)
  --dry-run                     Print planned commands; do not run or write files
  -h, --help                    Show this help
EOF
}

baseline_duration=3
competition_workers=2
competition_duration=3
repetitions=1
dry_run=0

while (( $# )); do
    case $1 in
        --baseline-duration|--competition-workers|--competition-duration|--repetitions)
            option=$1
            (( $# >= 2 )) || { printf 'Error: %s requires a value.\n' "$option" >&2; usage >&2; exit 2; }
            value=$2
            shift 2
            case $option in
                --baseline-duration) baseline_duration=$value ;;
                --competition-workers) competition_workers=$value ;;
                --competition-duration) competition_duration=$value ;;
                --repetitions) repetitions=$value ;;
            esac
            ;;
        --dry-run) dry_run=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Error: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

valid_duration() {
    [[ $1 =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]] \
        && awk -v value="$1" 'BEGIN { exit !(value + 0 > 0) }'
}

normalize_count() {
    local value=$1
    [[ $value =~ ^[0-9]+$ ]] || return 1
    while [[ ${#value} -gt 1 && $value == 0* ]]; do value=${value#0}; done
    (( ${#value} <= 3 )) || return 1
    (( 10#$value >= 1 && 10#$value <= 999 )) || return 1
    printf '%s' "$((10#$value))"
}

if ! valid_duration "$baseline_duration"; then
    printf 'Error: baseline duration must be a positive number of seconds.\n' >&2; exit 2
fi
if ! valid_duration "$competition_duration"; then
    printf 'Error: competition duration must be a positive number of seconds.\n' >&2; exit 2
fi
if ! competition_workers=$(normalize_count "$competition_workers") \
    || (( competition_workers > 32 )); then
    printf 'Error: competition workers must be an integer from 1 to 32.\n' >&2; exit 2
fi
if ! repetitions=$(normalize_count "$repetitions"); then
    printf 'Error: repetitions must be an integer from 1 to 999.\n' >&2; exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
baseline_runner="$script_dir/run_cpu_baseline.sh"
competition_runner="$script_dir/run_cpu_competition.sh"

printf 'Scheduler: default Linux scheduler (custom scheduler is not loaded)\n'
printf 'Plan: %s repetition(s), baseline duration=%s s, competition=%s worker(s) x %s s\n' \
    "$repetitions" "$baseline_duration" "$competition_workers" "$competition_duration"

if (( dry_run )); then
    for ((rep=1; rep<=repetitions; rep++)); do
        printf 'DRY RUN baseline %d: %q %q\n' "$rep" "$baseline_runner" "$baseline_duration"
        printf 'DRY RUN competition %d: %q %q %q\n' \
            "$rep" "$competition_runner" "$competition_workers" "$competition_duration"
    done
    exit 0
fi

results_dir="$repo_root/results/processed"
mkdir -p -- "$results_dir"
timestamp=$(date -u '+%Y%m%dT%H%M%SZ')
suite_dir=$(mktemp -d "$results_dir/experiment_suite_${timestamp}_XXXXXX")
manifest="$suite_dir/manifest.tsv"
printf 'suite_id\tscheduler\trepetition\texperiment\tcommand\texit_status\trunner_log\traw_result\n' >"$manifest"
suite_id=${suite_dir##*/}
suite_status=0

record_run() {
    local experiment=$1 repetition=$2 runner=$3 log=$4
    shift 4
    local status raw_result command_display arg
    command_display=$(printf '%q ' "$runner" "$@")
    command_display=${command_display% }
    if "$runner" "$@" >"$log" 2>&1; then
        status=0
    else
        status=$?
        suite_status=1
    fi
    raw_result=$(sed -n 's/^Saved result: //p' "$log" | tail -n 1)
    printf '%s\tdefault-linux-scheduler\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$suite_id" "$repetition" "$experiment" "$command_display" "$status" \
        "$log" "${raw_result:-NA}" >>"$manifest"
    printf '%s: status=%s runner_log=%s raw_result=%s\n' \
        "$experiment repetition $repetition" "$status" "$log" "${raw_result:-NA}"
}

for ((rep=1; rep<=repetitions; rep++)); do
    printf -v rep_label '%03d' "$rep"
    record_run baseline "$rep" "$baseline_runner" "$suite_dir/baseline_${rep_label}.log" \
        "$baseline_duration"
    record_run competition "$rep" "$competition_runner" "$suite_dir/competition_${rep_label}.log" \
        "$competition_workers" "$competition_duration"
done

printf 'Suite manifest: %s\n' "$manifest"
printf 'Scheduler: default Linux scheduler; this suite does not test the custom scheduler.\n'
exit "$suite_status"
