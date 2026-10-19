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
        latency) (( seen_latency )) && { printf 'Error: duplicate group definition: %s\n' "$group_name" >&2; usage; exit 2; }; seen_latency=1 ;;
        batch) (( seen_batch )) && { printf 'Error: duplicate group definition: %s\n' "$group_name" >&2; usage; exit 2; }; seen_batch=1 ;;
        background) (( seen_background )) && { printf 'Error: duplicate group definition: %s\n' "$group_name" >&2; usage; exit 2; }; seen_background=1 ;;
        *) printf 'Error: unsupported group name: %s\n' "$group_name" >&2; usage; exit 2 ;;
    esac
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

signal_status=0
overall_status=0
trap 'signal_status=130; overall_status=130' INT
trap 'signal_status=143; overall_status=143' TERM

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
worker="$repo_root/workloads/cpu_worker"
source_file="$repo_root/workloads/cpu_supervisor.c"
worker_available=1
if [[ ! -x $worker ]]; then
    printf 'Error: workload executable is missing or not executable: %s\n' "$worker" >&2
    printf 'Build it with: gcc -O2 -Wall -Wextra -std=c11 workloads/cpu_worker.c -o workloads/cpu_worker\n' >&2
    worker_available=0
fi

results_dir="$repo_root/results/raw"
mkdir -p -- "$results_dir"
timestamp=$(date -u '+%Y%m%dT%H%M%SZ')
result_file=$(mktemp "$results_dir/group_competition_${timestamp}_XXXXXX.csv")
run_id=${result_file##*/}
run_id=${run_id%.csv}
supervisor_bin="$results_dir/.${run_id}_supervisor"
cancel_file="$results_dir/.${run_id}.cancel"
( set -o noclobber; printf '0\n' >"$cancel_file" )
supervisor_pids=()
worker_groups=()
worker_indexes=()
worker_ids=()
worker_logs=()
worker_metadata=()
worker_started=()
worker_done=()
worker_status=()
worker_supervisor_status=()
worker_launch_error=()
finalized=0
cancel_requested=0

csv_field() {
    local value=${1//\"/\"\"}
    printf '"%s"' "$value"
}

append_csv_row() {
    local field separator=''
    for field in "$@"; do
        printf '%s' "$separator" >>"$result_file"
        csv_field "$field" >>"$result_file"
        separator=,
    done
    printf '\n' >>"$result_file"
}

job_is_running() {
    local target=$1 current
    while IFS= read -r current; do
        [[ $current == "$target" ]] && return 0
    done < <(jobs -pr)
    return 1
}

request_cancellation() {
    if (( cancel_requested == 0 )); then
        if printf '1\n' >"$cancel_file"; then
            cancel_requested=1
        else
            overall_status=1
        fi
    fi
}

read_metadata() {
    local index=$1
    META_PID=NA META_ELAPSED=NA META_USER=NA META_SYSTEM=NA
    META_EXIT=NA META_SIGNAL=0 META_EXEC_ERR=0 META_SUPERVISOR_ERR=0
    [[ -s ${worker_metadata[index]} ]] || return 1
    IFS=$'\t' read -r META_PID META_ELAPSED META_USER META_SYSTEM META_EXIT \
        META_SIGNAL META_EXEC_ERR META_SUPERVISOR_ERR <"${worker_metadata[index]}" || true
    [[ -n $META_PID ]] || META_PID=NA
    [[ -n $META_ELAPSED ]] || META_ELAPSED=NA
    [[ -n $META_USER ]] || META_USER=NA
    [[ -n $META_SYSTEM ]] || META_SYSTEM=NA
    [[ -n $META_EXIT ]] || META_EXIT=NA
    [[ -n $META_SIGNAL ]] || META_SIGNAL=0
    [[ -n $META_EXEC_ERR ]] || META_EXEC_ERR=0
    [[ -n $META_SUPERVISOR_ERR ]] || META_SUPERVISOR_ERR=0
    return 0
}

finalize_results() {
    local group_summary='' i j count index iterations status_text
    (( finalized )) && return
    finalized=1
    for ((i=0; i<${#group_names[@]}; i++)); do
        [[ -n $group_summary ]] && group_summary+=";"
        group_summary+="${group_names[i]}=${group_counts[i]}"
    done
    printf '%s\n' 'run_id,timestamp_utc,kernel_version,logical_cpu_count,requested_duration_s,group_counts,worker_id,group,group_worker_index,supervisor_pid,worker_pid,user_cpu_s,system_cpu_s,cpu_time_s,elapsed_s,iterations,exit_status,termination_signal,measurement_status,output_log' >"$result_file"
    index=0
    for ((i=0; i<${#group_names[@]}; i++)); do
        count=${group_counts[i]}
        for ((j=1; j<=count; j++)); do
            status_text='not_started'
            iterations=NA
            read_metadata "$index" || true
            if [[ -n ${worker_logs[index]} && -s ${worker_logs[index]} ]]; then
                iterations=$(awk -F= '$1 == "iterations" && $2 ~ /^[0-9]+$/ { value=$2 } END { if (value != "") print value; else print "NA" }' "${worker_logs[index]}")
            fi
            if [[ ${worker_started[index]} == 1 ]]; then
                status_text='complete'
                [[ $iterations != NA ]] || status_text='iterations_unavailable'
                [[ $META_ELAPSED != NA && $META_USER != NA && $META_SYSTEM != NA ]] || status_text='timing_unavailable'
                if [[ $META_SUPERVISOR_ERR != 0 ]]; then
                    status_text='supervisor_error'
                elif [[ $META_EXEC_ERR != 0 ]]; then
                    status_text='exec_failed'
                elif [[ $META_EXIT != 0 || $META_SIGNAL != 0 ]]; then
                    status_text='worker_failed'
                elif (( ${worker_supervisor_status[index]:-0} == 125 )) \
                    && [[ $META_EXIT == NA ]]; then
                    status_text='supervisor_error'
                fi
            elif [[ -n ${worker_launch_error[index]:-} ]]; then
                status_text='launch_failed'
            fi
            if (( signal_status != 0 )) \
                && { [[ ${worker_done[index]:-0} == 0 ]] || [[ ${worker_started[index]:-0} == 0 ]] || [[ $META_SIGNAL != 0 ]]; }; then
                status_text='interrupted'
            fi
            local user_s system_s cpu_s elapsed_worker exit_field pid_field sup_pid
            user_s=NA system_s=NA cpu_s=NA elapsed_worker=NA
            [[ $META_USER =~ ^[0-9]+$ ]] && user_s=$(awk -v n="$META_USER" 'BEGIN {printf "%.6f",n/1000000}')
            [[ $META_SYSTEM =~ ^[0-9]+$ ]] && system_s=$(awk -v n="$META_SYSTEM" 'BEGIN {printf "%.6f",n/1000000}')
            if [[ $user_s != NA && $system_s != NA ]]; then
                cpu_s=$(awk -v u="$user_s" -v s="$system_s" 'BEGIN {printf "%.6f",u+s}')
            fi
            [[ $META_ELAPSED =~ ^[0-9]+$ ]] && elapsed_worker=$(awk -v n="$META_ELAPSED" 'BEGIN {printf "%.6f",n/1000000000}')
            exit_field=$META_EXIT
            pid_field=$META_PID
            sup_pid=${supervisor_pids[index]:-NA}
            [[ -n $exit_field ]] || exit_field=NA
            [[ -n $pid_field ]] || pid_field=NA
            append_csv_row "$run_id" "$timestamp" "$(uname -r)" "$(getconf _NPROCESSORS_ONLN)" \
                "$duration" "$group_summary" "${worker_ids[index]}" "${worker_groups[index]}" \
                "${worker_indexes[index]}" "$sup_pid" "$pid_field" "$user_s" "$system_s" "$cpu_s" \
                "$elapsed_worker" "$iterations" "$exit_field" "$META_SIGNAL" "$status_text" "${worker_logs[index]:-NA}"
            if [[ $exit_field != 0 || $META_SIGNAL != 0 || $status_text == launch_failed || $status_text == supervisor_error || $status_text == exec_failed ]]; then
                overall_status=1
            fi
            index=$((index + 1))
        done
    done
    for file in "${worker_metadata[@]}"; do [[ -n $file ]] && rm -f -- "$file"; done
    [[ -e $supervisor_bin ]] && rm -f -- "$supervisor_bin"
    [[ -e $cancel_file ]] && rm -f -- "$cancel_file"
    printf 'Saved result: %s\n' "$result_file" >&2
}

cleanup() {
    local prior_status=$? final_status i status pending
    # Keep INT/TERM handlers installed while cleanup runs. A repeated signal
    # requests cancellation again instead of aborting waits and CSV finalizing.
    trap - EXIT
    set +e
    if (( finalized == 0 )); then
        request_cancellation
        pending=1
        while (( pending )); do
            pending=0
            for ((i=0; i<${#supervisor_pids[@]}; i++)); do
                [[ ${worker_done[i]:-0} == 1 ]] && continue
                [[ ${supervisor_pids[i]:-NA} =~ ^[0-9]+$ ]] || continue
                if job_is_running "${supervisor_pids[i]}"; then
                    pending=1
                    continue
                fi
                if wait "${supervisor_pids[i]}"; then status=0; else status=$?; fi
                worker_supervisor_status[i]=$status
                worker_done[i]=1
            done
            (( pending == 0 )) || sleep 0.02 || true
        done
        finalize_results
    fi
    final_status=$prior_status
    (( signal_status != 0 )) && final_status=$signal_status
    exit "$final_status"
}
trap cleanup EXIT
trap 'signal_status=130; overall_status=130; request_cancellation' INT
trap 'signal_status=143; overall_status=143; request_cancellation' TERM

for ((i=0; i<${#group_names[@]}; i++)); do
    for ((j=1; j<=${group_counts[i]}; j++)); do
        index=${#worker_ids[@]}
        printf -v worker_id 'w%04d' "$((index + 1))"
        worker_ids+=("$worker_id")
        worker_groups+=("${group_names[i]}")
        worker_indexes+=("$j")
        worker_started+=(0)
        worker_done+=(0)
        worker_status+=(not_started)
        worker_supervisor_status+=(NA)
        worker_launch_error+=("")
        worker_logs+=("")
        worker_metadata+=("")
    done
done

for ((index=0; index<${#worker_ids[@]}; index++)); do
    log_file=$(mktemp "$results_dir/${run_id}_${worker_ids[index]}_XXXXXX.log") || {
        worker_launch_error[index]=log_create_failed
        overall_status=1
        continue
    }
    meta_file=$(mktemp "$results_dir/.${run_id}_${worker_ids[index]}_XXXXXX.meta") || {
        worker_launch_error[index]=metadata_create_failed
        worker_logs[index]=$log_file
        overall_status=1
        continue
    }
    worker_logs[index]=$log_file
    worker_metadata[index]=$meta_file
done

if ! gcc -O2 -Wall -Wextra -Werror -std=c11 "$source_file" -o "$supervisor_bin"; then
    printf 'Error: failed to build cpu_supervisor.\n' >&2
    overall_status=1
    for ((index=0; index<${#worker_ids[@]}; index++)); do
        worker_launch_error[index]=supervisor_build_failed
    done
elif (( worker_available == 0 )); then
    overall_status=1
    for ((index=0; index<${#worker_ids[@]}; index++)); do
        worker_launch_error[index]=worker_missing
    done
else
    for ((index=0; index<${#worker_ids[@]}; index++)); do
        if (( signal_status != 0 )); then
            overall_status=$signal_status
            break
        fi
        if [[ -n ${worker_launch_error[index]} ]]; then
            continue
        fi
        if "$supervisor_bin" "${worker_metadata[index]}" "$worker" "$duration" "$cancel_file" >"${worker_logs[index]}" 2>&1 & then
            supervisor_pids[index]=$!
            worker_started[index]=1
        else
            supervisor_pids[index]=NA
            worker_launch_error[index]=supervisor_launch_failed
            overall_status=1
        fi
    done
fi

if (( overall_status != 0 )); then
    request_cancellation
fi

if (( ${#supervisor_pids[@]} > 0 )); then
    pending=1
    while (( pending )); do
        pending=0
        for ((index=0; index<${#supervisor_pids[@]}; index++)); do
            [[ ${supervisor_pids[index]:-NA} =~ ^[0-9]+$ ]] || continue
            [[ ${worker_done[index]} == 1 ]] && continue
            if job_is_running "${supervisor_pids[index]}"; then
                pending=1
                continue
            fi
            if wait "${supervisor_pids[index]}"; then status=0; else status=$?; fi
            worker_supervisor_status[index]=$status
            worker_done[index]=1
            worker_status[index]=$status
            if (( status != 0 )); then
                overall_status=1
                request_cancellation
            fi
        done
        (( pending == 0 )) || sleep 0.02 || true
    done
fi
if (( signal_status != 0 )); then overall_status=$signal_status; fi
finalize_results
exit "$overall_status"
