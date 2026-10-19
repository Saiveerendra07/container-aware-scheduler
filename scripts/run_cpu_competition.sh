#!/usr/bin/env bash

set -Eeuo pipefail

usage() {
    printf 'Usage: %s [worker_count [duration_seconds_per_worker]]\n' "${0##*/}" >&2
    printf 'worker_count must be an integer from 1 to 32.\n' >&2
}

if (( $# > 2 )); then
    printf 'Error: too many arguments.\n' >&2
    usage
    exit 2
fi

if (( $# >= 1 )); then
    worker_count=$1
else
    worker_count=2
fi

if [[ ! $worker_count =~ ^[0-9]+$ ]]; then
    printf 'Error: worker_count must be an integer from 1 to 32.\n' >&2
    usage
    exit 2
fi

# Remove leading zeroes before arithmetic, so even very long inputs are safe.
worker_count_normalized=$worker_count
while [[ ${#worker_count_normalized} -gt 1 && $worker_count_normalized == 0* ]]; do
    worker_count_normalized=${worker_count_normalized#0}
done
if (( ${#worker_count_normalized} > 2 )) \
    || (( 10#$worker_count_normalized < 1 || 10#$worker_count_normalized > 32 )); then
    printf 'Error: worker_count must be an integer from 1 to 32.\n' >&2
    usage
    exit 2
fi
worker_count=$((10#$worker_count_normalized))

if (( $# >= 2 )); then
    duration=$2
else
    duration=3
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
result_file=$(mktemp "$results_dir/cpu_competition_${timestamp}_XXXXXX.txt")
uptime_centiseconds() {
    awk '{ printf "%.0f\n", $1 * 100 }' /proc/uptime
}

start_cs=$(uptime_centiseconds)

worker_pids=()
worker_logs=()
worker_statuses=()
worker_finished=()
signal_status=0
result_written=0
overall_status=0

write_result() {
    local end_cs elapsed_cs index

    end_cs=$(uptime_centiseconds)
    elapsed_cs=$((end_cs - start_cs))

    {
        printf 'Timestamp: %s\n' "$(date --iso-8601=seconds)"
        printf 'Kernel version: %s\n' "$(uname -r)"
        printf 'Logical CPUs: %s\n' "$(getconf _NPROCESSORS_ONLN)"
        printf 'Requested workers: %s\n' "$worker_count"
        printf 'Requested duration per worker (seconds): %s\n' "$duration"
        printf 'Workers started: %s\n' "${#worker_pids[@]}"

        for ((index = 0; index < ${#worker_pids[@]}; index++)); do
            printf '\nWorker %s PID: %s\n' "$((index + 1))" "${worker_pids[index]}"
            printf '%s\n' 'Worker output (stdout and stderr):'
            cat -- "${worker_logs[index]}"
            printf '\nWorker %s exit status: %s\n' \
                "$((index + 1))" "${worker_statuses[index]}"
        done

        printf '\nTotal elapsed time (monotonic, seconds): %d.%02d\n' \
            "$((elapsed_cs / 100))" "$((elapsed_cs % 100))"
    } >"$result_file"
}

job_is_running() {
    local target_pid=$1 running_pid

    while IFS= read -r running_pid; do
        if [[ $running_pid == "$target_pid" ]]; then
            return 0
        fi
    done < <(jobs -pr)

    return 1
}

terminate_unfinished_workers() {
    local index

    for ((index = 0; index < ${#worker_pids[@]}; index++)); do
        if (( worker_finished[index] == 0 )); then
            kill -TERM "${worker_pids[index]}" 2>/dev/null || true
        fi
    done
}

cleanup() {
    local exit_status=$? index status

    trap - EXIT INT TERM
    set +e

    if (( result_written == 0 )); then
        for ((index = 0; index < ${#worker_pids[@]}; index++)); do
            if (( worker_finished[index] == 0 )); then
                kill -TERM "${worker_pids[index]}" 2>/dev/null
            fi
        done

        for ((index = 0; index < ${#worker_pids[@]}; index++)); do
            if (( worker_finished[index] == 0 )); then
                if wait "${worker_pids[index]}"; then
                    status=0
                else
                    status=$?
                fi
                worker_statuses[index]=$status
                worker_finished[index]=1
            fi
        done

        write_result
        result_written=1
        printf 'Saved result: %s\n' "$result_file" >&2
    fi

    for log_file in "${worker_logs[@]}"; do
        rm -f -- "$log_file"
    done

    exit "$exit_status"
}

trap cleanup EXIT
trap 'signal_status=130; overall_status=130' INT
trap 'signal_status=143; overall_status=143' TERM

for ((index = 0; index < worker_count; index++)); do
    if (( signal_status != 0 )); then
        exit "$signal_status"
    fi

    log_file=$(mktemp "$results_dir/.cpu_competition_${timestamp}_worker$((index + 1))_XXXXXX")
    worker_logs+=("$log_file")
    "$worker" "$duration" >"$log_file" 2>&1 &
    worker_pids+=("$!")
    worker_statuses+=(pending)
    worker_finished+=(0)
done

while :; do
    if (( signal_status != 0 )); then
        exit "$signal_status"
    fi

    pending_workers=0
    for ((index = 0; index < ${#worker_pids[@]}; index++)); do
        if (( worker_finished[index] == 0 )); then
            if job_is_running "${worker_pids[index]}"; then
                pending_workers=1
            else
                if wait "${worker_pids[index]}"; then
                    status=0
                else
                    status=$?
                fi

                worker_statuses[index]=$status
                worker_finished[index]=1
                if (( status != 0 )); then
                    overall_status=1
                    terminate_unfinished_workers
                fi
            fi
        fi
    done

    if (( pending_workers == 0 )); then
        break
    fi
    sleep 0.02 || (( signal_status != 0 ))
done

write_result
result_written=1
printf 'Saved result: %s\n' "$result_file"
exit "$overall_status"
