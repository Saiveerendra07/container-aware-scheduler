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

run_id="codex_cgroup_${BASHPID}_$(date +%s)"
group_a="$parent/${run_id}_a"
group_b="$parent/${run_id}_b"
created=()
pids=()
cleanup() {
    local status=$? pid
    trap - EXIT INT TERM
    set +e
    for pid in "${pids[@]}"; do kill -TERM "$pid" 2>/dev/null; done
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null; done
    for ((i=${#created[@]}-1; i>=0; i--)); do rmdir -- "${created[i]}" 2>/dev/null || {
        printf 'Cleanup could not remove empty cgroup %s (check for remaining processes).\n' "${created[i]}" >&2
        status=1
    }; done
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -- "$group_a"; created+=("$group_a")
mkdir -- "$group_b"; created+=("$group_b")
for group in "$group_a" "$group_b"; do
    [[ -w $group/cgroup.procs && -w $group/cpu.weight && -w $group/cpu.max ]] || fail "required writable interfaces missing in $group"
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
printf 'Scheduler: default Linux scheduler (ordinary cgroup CPU control; custom sched_ext is not attached)\n'
printf 'Parent: %s\nSetting: %s\nDuration: %s seconds\n' "$parent" "$setting" "$duration"
( printf '%s\n' "$BASHPID" >"$group_a/cgroup.procs"; exec "$worker" "$duration" ) & pids+=("$!")
( printf '%s\n' "$BASHPID" >"$group_b/cgroup.procs"; exec "$worker" "$duration" ) & pids+=("$!")
status=0
for pid in "${pids[@]}"; do wait "$pid" || status=1; done
printf 'Workers finished; temporary cgroups will be removed.\n'
exit "$status"
