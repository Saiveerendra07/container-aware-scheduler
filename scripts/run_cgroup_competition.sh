#!/usr/bin/env bash

set -Eeuo pipefail

usage() {
    cat <<'EOF'
Usage:
  run_cgroup_competition.sh --check [--parent DIR]
  run_cgroup_competition.sh --parent DIR [--weight-a N --weight-b N | --quota-a Q/P --quota-b Q/P] [--duration SEC]

Run two cpu_worker processes in delegated cgroup-v2 groups using the default
Linux scheduler. This script never enables controllers or attaches sched_ext.
EOF
}

fail() { printf 'Error: %s\n' "$*" >&2; exit 2; }
normalize_positive() {
    [[ $1 =~ ^[0-9]+$ ]] || return 1
    local value=$1
    while [[ ${#value} -gt 1 && $value == 0* ]]; do value=${value#0}; done
    (( ${#value} <= 9 )) || return 1
    (( 10#$value > 0 )) || return 1
    printf '%s' "$((10#$value))"
}
valid_quota() {
    [[ $1 =~ ^([0-9]+|max)/([0-9]+)$ ]] || return 1
    local quota period
    quota=${BASH_REMATCH[1]}
    period=${BASH_REMATCH[2]}
    while [[ ${#period} -gt 1 && $period == 0* ]]; do period=${period#0}; done
    (( ${#period} <= 9 )) && (( 10#$period >= 1000 && 10#$period <= 1000000 ))
    if [[ $quota != max ]]; then
        while [[ ${#quota} -gt 1 && $quota == 0* ]]; do quota=${quota#0}; done
        (( ${#quota} <= 9 )) && (( 10#$quota > 0 ))
    fi
}

mode=run
dry_run=0
parent=/sys/fs/cgroup
weight_a= weight_b= quota_a= quota_b= duration=3
while (( $# )); do
    case $1 in
        --check) [[ $mode == run ]] || fail '--check may be specified once'; mode=check; shift ;;
        --dry-run) dry_run=1; shift ;;
        --parent|--weight-a|--weight-b|--quota-a|--quota-b|--duration)
            (( $# >= 2 )) || fail "$1 requires a value"
            option=$1 value=$2; shift 2
            case $option in
                --parent) parent=$value ;;
                --weight-a) weight_a=$value ;;
                --weight-b) weight_b=$value ;;
                --quota-a) quota_a=$value ;;
                --quota-b) quota_b=$value ;;
                --duration) duration=$value ;;
            esac
            ;;
        -h|--help) usage; exit 0 ;;
        *) fail "unknown option: $1" ;;
    esac
done

if [[ $mode == check ]]; then
    [[ -r /proc/self/mountinfo ]] || fail '/proc/self/mountinfo is unavailable'
    [[ -r $parent/cgroup.controllers && -r $parent/cgroup.subtree_control ]] || \
        fail "not a cgroup-v2 directory or cpu controller interfaces missing: $parent"
    awk '$0 ~ / - cgroup2 / { found=1 } END { exit !found }' /proc/self/mountinfo || \
        fail 'no cgroup v2 mount is listed in /proc/self/mountinfo'
    controllers=$(<"$parent/cgroup.controllers")
    [[ " $controllers " == *' cpu '* ]] || fail "cpu controller is unavailable beneath $parent"
    subtree=$(<"$parent/cgroup.subtree_control")
    [[ " $subtree " == *' cpu '* ]] || fail "cpu controller is not already enabled for children of $parent"
    [[ -w $parent ]] || fail "parent is not writable by this user: $parent"
    [[ -w $parent/cgroup.procs && -r $parent/cgroup.procs ]] || fail 'cgroup.procs is not accessible'
    [[ -r $parent/cpu.max && -r $parent/cpu.stat ]] || fail 'CPU controller interfaces cpu.max/cpu.stat are unavailable'
    printf 'cgroup v2: available\ncpu controller: enabled for child cgroups\nparent writable: yes (%s)\nsched_ext: not checked or used\n' "$parent"
    exit 0
fi

[[ -n $weight_a || -n $weight_b || -n $quota_a || -n $quota_b ]] || { weight_a=100; weight_b=100; }
if [[ -n $weight_a || -n $weight_b ]]; then
    [[ -z $quota_a && -z $quota_b && -n $weight_a && -n $weight_b ]] || fail 'specify both --weight-a and --weight-b, or both quotas, but not both modes'
    weight_a=$(normalize_positive "$weight_a") || fail 'weight-a must be an integer from 1 to 10000'
    weight_b=$(normalize_positive "$weight_b") || fail 'weight-b must be an integer from 1 to 10000'
    (( weight_a <= 10000 && weight_b <= 10000 )) || fail 'CPU weights must be from 1 to 10000'
else
    [[ -n $quota_a && -n $quota_b ]] || fail 'specify both --quota-a and --quota-b'
    valid_quota "$quota_a" || fail 'quota-a must be QUOTA/PERIOD (use max for unlimited; period 1000..1000000 us)'
    valid_quota "$quota_b" || fail 'quota-b must be QUOTA/PERIOD (use max for unlimited; period 1000..1000000 us)'
fi
[[ $duration =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]] && awk -v d="$duration" 'BEGIN { exit !(d>0) }' || fail 'duration must be a positive number of seconds'

[[ -d $parent ]] || fail "parent cgroup directory does not exist: $parent"
[[ -r $parent/cgroup.controllers && -r $parent/cgroup.subtree_control ]] || fail 'parent does not expose cgroup-v2 controller files'
controllers=$(<"$parent/cgroup.controllers")
subtree=$(<"$parent/cgroup.subtree_control")
[[ " $controllers " == *' cpu '* && " $subtree " == *' cpu '* ]] || fail 'cpu controller must already be enabled in the delegated parent'
[[ -w $parent ]] || fail "parent is not writable: $parent"
if (( dry_run )); then
    printf 'DRY RUN: create two temporary cgroups under %s\n' "$parent"
    if [[ -n $weight_a ]]; then printf 'DRY RUN: cpu.weight A=%s B=%s\n' "$weight_a" "$weight_b"
    else printf 'DRY RUN: cpu.max A=%s B=%s\n' "$quota_a" "$quota_b"; fi
    printf 'DRY RUN: run one cpu_worker per group for %s seconds; no writes or scheduler attach\n' "$duration"
    exit 0
fi
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
worker="$repo_root/workloads/cpu_worker"
[[ -x $worker ]] || fail "missing workload executable: build workloads/cpu_worker first"
command -v python3 >/dev/null 2>&1 || fail 'Python 3 is required to write the result CSV'

run_id="codex_cgroup_${BASHPID}_$(date +%s)"
group_a="$parent/${run_id}_a"
group_b="$parent/${run_id}_b"
results_dir="$repo_root/results/raw"
mkdir -p -- "$results_dir"
timestamp=$(date -u '+%Y%m%dT%H%M%SZ')
result_file=$(mktemp "$results_dir/cgroup_competition_${timestamp}_XXXXXX.csv")
run_id=${result_file##*/}
run_id=${run_id%.csv}
tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/cgroup_metrics_${BASHPID}_XXXXXX")
metadata_file="$tmp_dir/metadata.tsv"
metrics_helper="$script_dir/cgroup_metrics.py"
printf 'run_id\ttimestamp_utc\tkernel_version\tlogical_cpu_count\trequested_duration_s\tcontrol_setting\tgroup\tcgroup_path\tworkload_elapsed_s\texit_status\tmeasurement_status\tbefore_stat\tafter_stat\n' >"$metadata_file"
created=()
pids=()
groups=("$group_a" "$group_b")
group_names=(A B)
start_files=("$tmp_dir/a.timing" "$tmp_dir/b.timing")
before_files=("$tmp_dir/a.before" "$tmp_dir/b.before")
after_files=("$tmp_dir/a.after" "$tmp_dir/b.after")
worker_statuses=(NA NA)
run_timestamp=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
kernel_version=$(uname -r)
logical_cpu_count=$(getconf _NPROCESSORS_ONLN)
setting=
if [[ -n $weight_a ]]; then setting="cpu.weight A=$weight_a B=$weight_b"
else setting="cpu.max A=$quota_a B=$quota_b"; fi

uptime_ticks() { awk '{ printf "%.0f", $1 * 100 }' /proc/uptime; }
job_is_running() {
    local target=$1 running
    while IFS= read -r running; do
        [[ $running == "$target" ]] && return 0
    done < <(jobs -pr)
    return 1
}

cleanup() {
    local status=$? pid index start_ticks end_ticks elapsed worker_status measure_status
    trap - EXIT INT TERM
    set +e
    for pid in "${pids[@]}"; do
        if job_is_running "$pid"; then
            kill -TERM "$pid" 2>/dev/null
        fi
    done
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null; done
    for index in 0 1; do
        [[ -d ${groups[index]} ]] && cat -- "${groups[index]}/cpu.stat" >"${after_files[index]}" 2>/dev/null
        start_ticks= end_ticks= worker_status=NA elapsed=NA measure_status=incomplete
        if [[ -r ${start_files[index]} ]]; then
            IFS=$'\t' read -r start_ticks end_ticks worker_status <"${start_files[index]}" || true
            if [[ $start_ticks =~ ^[0-9]+$ && $end_ticks =~ ^[0-9]+$ ]] && (( end_ticks >= start_ticks )); then
                elapsed=$(awk -v s="$start_ticks" -v e="$end_ticks" 'BEGIN { printf "%.2f", (e-s)/100 }')
                measure_status=complete
            fi
        fi
        [[ $worker_status =~ ^[0-9]+$ ]] || worker_status=NA
        [[ $measure_status == complete && $worker_status == 0 ]] || measure_status=incomplete
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
            "$run_id" "$run_timestamp" "$kernel_version" "$logical_cpu_count" "$duration" \
            "$setting" "${group_names[index]}" "${groups[index]}" "$elapsed" "$worker_status" \
            "$measure_status" "${before_files[index]}" "${after_files[index]}" >>"$metadata_file"
    done
    if ! python3 "$metrics_helper" "$metadata_file" "$result_file"; then
        printf 'Failed to write cgroup metrics CSV: %s\n' "$result_file" >&2
        status=1
    else
        printf 'Saved result: %s\n' "$result_file" >&2
    fi
    for ((i=${#created[@]}-1; i>=0; i--)); do rmdir -- "${created[i]}" 2>/dev/null || {
        printf 'Cleanup could not remove empty cgroup %s (check for remaining processes).\n' "${created[i]}" >&2
        status=1
    }; done
    rm -rf -- "$tmp_dir"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -- "$group_a"; created+=("$group_a")
mkdir -- "$group_b"; created+=("$group_b")
for group in "$group_a" "$group_b"; do
    [[ -w $group/cgroup.procs && -w $group/cpu.weight && -w $group/cpu.max && -r $group/cpu.stat ]] || fail "required cgroup CPU interfaces missing in $group"
done
if [[ -n $weight_a ]]; then
    printf '%s\n' "$weight_a" >"$group_a/cpu.weight"
    printf '%s\n' "$weight_b" >"$group_b/cpu.weight"
    setting="cpu.weight A=$weight_a B=$weight_b"
else
    printf '%s\n' "${quota_a/// }" >"$group_a/cpu.max"
    printf '%s\n' "${quota_b/// }" >"$group_b/cpu.max"
    setting="cpu.max A=$quota_a B=$quota_b"
fi
cat -- "$group_a/cpu.stat" >"${before_files[0]}" 2>/dev/null || true
cat -- "$group_b/cpu.stat" >"${before_files[1]}" 2>/dev/null || true
printf 'Scheduler: default Linux scheduler (ordinary cgroup CPU control; custom sched_ext is not attached)\n'
printf 'Parent: %s\nSetting: %s\nDuration: %s seconds\n' "$parent" "$setting" "$duration"
(
    worker_pid=
    start_ticks=
    stop_worker() {
        [[ -n $worker_pid ]] && kill -TERM "$worker_pid" 2>/dev/null
        [[ -n $worker_pid ]] && wait "$worker_pid" 2>/dev/null
        end_ticks=$(uptime_ticks)
        printf '%s\t%s\t143\n' "$start_ticks" "$end_ticks" >"${start_files[0]}"
        exit 143
    }
    trap stop_worker INT TERM
    if ! printf '%s\n' "$BASHPID" >"$group_a/cgroup.procs"; then
        printf 'NA\tNA\t125\n' >"${start_files[0]}"
        exit 125
    fi
    start_ticks=$(uptime_ticks)
    "$worker" "$duration" & worker_pid=$!
    if wait "$worker_pid"; then worker_status=0; else worker_status=$?; fi
    end_ticks=$(uptime_ticks)
    printf '%s\t%s\t%s\n' "$start_ticks" "$end_ticks" "$worker_status" >"${start_files[0]}"
    exit "$worker_status"
) & pids+=("$!")
(
    worker_pid=
    start_ticks=
    stop_worker() {
        [[ -n $worker_pid ]] && kill -TERM "$worker_pid" 2>/dev/null
        [[ -n $worker_pid ]] && wait "$worker_pid" 2>/dev/null
        end_ticks=$(uptime_ticks)
        printf '%s\t%s\t143\n' "$start_ticks" "$end_ticks" >"${start_files[1]}"
        exit 143
    }
    trap stop_worker INT TERM
    if ! printf '%s\n' "$BASHPID" >"$group_b/cgroup.procs"; then
        printf 'NA\tNA\t125\n' >"${start_files[1]}"
        exit 125
    fi
    start_ticks=$(uptime_ticks)
    "$worker" "$duration" & worker_pid=$!
    if wait "$worker_pid"; then worker_status=0; else worker_status=$?; fi
    end_ticks=$(uptime_ticks)
    printf '%s\t%s\t%s\n' "$start_ticks" "$end_ticks" "$worker_status" >"${start_files[1]}"
    exit "$worker_status"
) & pids+=("$!")
status=0
for pid in "${pids[@]}"; do wait "$pid" || status=1; done
printf 'Workers finished; temporary cgroups will be removed.\n'
exit "$status"
