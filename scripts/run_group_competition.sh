#!/usr/bin/env bash

set -Eeuo pipefail

usage() {
    printf 'Usage: %s name=count [name=count ...] duration_seconds\n' "${0##*/}" >&2
    printf 'Groups: latency, batch, background; each count must be from 1 to 32.\n' >&2
}

if (( $# < 2 || $# > 4 )); then
    printf 'Error: provide one to three group definitions and a duration.\n' >&2
    usage
    exit 2
fi

duration=${!#}
group_arg_count=$(($# - 1))

if [[ ! $duration =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]] \
    || ! awk -v value="$duration" 'BEGIN { exit !(value + 0 > 0) }'; then
    printf 'Error: duration must be a positive number of seconds.\n' >&2
    usage
    exit 2
fi

group_names=()
group_counts=()
seen_latency=0
seen_batch=0
seen_background=0

for ((arg_index = 1; arg_index <= group_arg_count; arg_index++)); do
    group_spec=${!arg_index}
    if [[ ! $group_spec =~ ^([a-z]+)=([0-9]+)$ ]]; then
        printf 'Error: malformed group definition: %s\n' "$group_spec" >&2
        usage
        exit 2
    fi

    group_name=${BASH_REMATCH[1]}
    group_count=${BASH_REMATCH[2]}
    case $group_name in
        latency)
            if (( seen_latency )); then
                printf 'Error: duplicate group definition: %s\n' "$group_name" >&2
                usage
                exit 2
            fi
            seen_latency=1
            ;;
        batch)
            if (( seen_batch )); then
                printf 'Error: duplicate group definition: %s\n' "$group_name" >&2
                usage
                exit 2
            fi
            seen_batch=1
            ;;
        background)
            if (( seen_background )); then
                printf 'Error: duplicate group definition: %s\n' "$group_name" >&2
                usage
                exit 2
            fi
            seen_background=1
            ;;
        *)
            printf 'Error: unsupported group name: %s\n' "$group_name" >&2
            usage
            exit 2
            ;;
    esac

    # Normalize leading zeroes before arithmetic to avoid octal and overflow issues.
    group_count_normalized=$group_count
    while [[ ${#group_count_normalized} -gt 1 && $group_count_normalized == 0* ]]; do
        group_count_normalized=${group_count_normalized#0}
    done
    if (( ${#group_count_normalized} > 2 )) \
        || (( 10#$group_count_normalized < 1 || 10#$group_count_normalized > 32 )); then
        printf 'Error: count for %s must be an integer from 1 to 32.\n' "$group_name" >&2
        usage
        exit 2
    fi

    group_names+=("$group_name")
    group_counts+=("$((10#$group_count_normalized))")
done

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
result_file=$(mktemp "$results_dir/group_competition_${timestamp}_XXXXXX.txt")

uptime_centiseconds() {
    awk '{ printf "%.0f\n", $1 * 100 }' /proc/uptime
}

start_cs=$(uptime_centiseconds)
worker_pids=()
worker_groups=()
worker_numbers=()
worker_logs=()
worker_statuses=()
worker_finished=()
signal_status=0
result_written=0
overall_status=0

write_result() {
    local end_cs elapsed_cs group_index worker_index

    end_cs=$(uptime_centiseconds)
    elapsed_cs=$((end_cs - start_cs))

    {
        printf 'Timestamp: %s\n' "$(date --iso-8601=seconds)"
        printf 'Kernel version: %s\n' "$(uname -r)"
        printf 'Logical CPUs: %s\n' "$(getconf _NPROCESSORS_ONLN)"
        printf 'Requested duration per worker (seconds): %s\n' "$duration"
        printf 'Requested groups and worker counts:\n'
        for ((group_index = 0; group_index < ${#group_names[@]}; group_index++)); do
            printf '  %s: %s\n' "${group_names[group_index]}" "${group_counts[group_index]}"
        done
        printf 'Workers started: %s\n' "${#worker_pids[@]}"

        for ((worker_index = 0; worker_index < ${#worker_pids[@]}; worker_index++)); do
            printf '\nGroup %s worker %s PID: %s\n' \
                "${worker_groups[worker_index]}" "${worker_numbers[worker_index]}" \
                "${worker_pids[worker_index]}"
            printf '%s\n' 'Worker output (stdout and stderr):'
            cat -- "${worker_logs[worker_index]}"
            printf '\nGroup %s worker %s exit status: %s\n' \
                "${worker_groups[worker_index]}" "${worker_numbers[worker_index]}" \
                "${worker_statuses[worker_index]}"
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
    local worker_index

    for ((worker_index = 0; worker_index < ${#worker_pids[@]}; worker_index++)); do
        if (( worker_finished[worker_index] == 0 )); then
            kill -TERM "${worker_pids[worker_index]}" 2>/dev/null || true
        fi
    done
}

cleanup() {
    local exit_status=$? worker_index status log_file

    trap - EXIT INT TERM
    set +e

    if (( result_written == 0 )); then
        terminate_unfinished_workers
        for ((worker_index = 0; worker_index < ${#worker_pids[@]}; worker_index++)); do
            if (( worker_finished[worker_index] == 0 )); then
                if wait "${worker_pids[worker_index]}"; then
                    status=0
                else
                    status=$?
                fi
                worker_statuses[worker_index]=$status
                worker_finished[worker_index]=1
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

for ((group_index = 0; group_index < ${#group_names[@]}; group_index++)); do
    group_name=${group_names[group_index]}
    group_count=${group_counts[group_index]}
    for ((worker_number = 1; worker_number <= group_count; worker_number++)); do
        if (( signal_status != 0 )); then
            exit "$signal_status"
        fi

        log_file=$(mktemp "$results_dir/.group_competition_${timestamp}_${group_name}_${worker_number}_XXXXXX")
        worker_logs+=("$log_file")
        "$worker" "$duration" >"$log_file" 2>&1 &
        worker_pids+=("$!")
        worker_groups+=("$group_name")
        worker_numbers+=("$worker_number")
        worker_statuses+=(pending)
        worker_finished+=(0)
    done
done

while :; do
    if (( signal_status != 0 )); then
        exit "$signal_status"
    fi

    pending_workers=0
    for ((worker_index = 0; worker_index < ${#worker_pids[@]}; worker_index++)); do
        if (( worker_finished[worker_index] == 0 )); then
            if job_is_running "${worker_pids[worker_index]}"; then
                pending_workers=1
            else
                if wait "${worker_pids[worker_index]}"; then
                    status=0
                else
                    status=$?
                fi

                worker_statuses[worker_index]=$status
                worker_finished[worker_index]=1
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
