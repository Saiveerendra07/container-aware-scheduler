#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# Create two temporary cgroup-v2 groups, run the real sched_ext experiment,
# and remove only the empty cgroups created by this script.
#
# Invoke with sudo because sched_ext attachment and cgroup creation require
# kernel privileges. The experiment harness itself still verifies every
# cgroup ID/path pair and refuses to start workers unless attachment is proven.

set -Eeuo pipefail

usage() {
    cat <<'EOF'
Usage:
  sudo bash scripts/run_custom_scheduler_demo.sh [options]

Options:
  --duration SEC       Worker duration per group (default: 10)
    --worker-timeout SEC  Maximum supervisor runtime (default: duration + 30)
  --share-a N          Scheduler share for group latency (default: 100)
  --share-b N          Scheduler share for group batch (default: 300)
  --fallback-share N   Share for all unmatched tasks (default: 100)
  --parent DIR         Existing cgroup-v2 parent (default: /sys/fs/cgroup)
  --loader PATH        Scheduler loader (default: detected build directory)
  --bpf-object PATH    BPF object (default: detected build directory)
    --libbpf-libdir DIR  Directory containing the matching libbpf.so.1
  --worker PATH        CPU worker (default: build/cpu_worker)
  --supervisor PATH    CPU supervisor (default: build/cpu_supervisor)
  -h, --help           Show this help

The script creates two temporary cgroups under --parent. It does not enable
controllers, change cpu.weight/cpu.max, or move unrelated processes. The
workers run in the two cgroups; the loader and supervisor remain in the parent.
EOF
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }

duration=10
worker_timeout=
share_a=100
share_b=300
fallback_share=100
parent=/sys/fs/cgroup
loader=
bpf_object=
libbpf_libdir=${CAS_LIBBPF_LIBDIR:-}
worker=
supervisor=

while (($#)); do
    case "$1" in
        --duration|--worker-timeout|--share-a|--share-b|--fallback-share|--parent|--loader|--bpf-object|--libbpf-libdir|--worker|--supervisor)
            (($# >= 2)) || fail "$1 requires a value"
            option=$1
            value=$2
            shift 2
            case "$option" in
                --duration) duration=$value ;;
                --worker-timeout) worker_timeout=$value ;;
                --share-a) share_a=$value ;;
                --share-b) share_b=$value ;;
                --fallback-share) fallback_share=$value ;;
                --parent) parent=$value ;;
                --loader) loader=$value ;;
                --bpf-object) bpf_object=$value ;;
                --libbpf-libdir) libbpf_libdir=$value ;;
                --worker) worker=$value ;;
                --supervisor) supervisor=$value ;;
            esac
            ;;
        -h|--help) usage; exit 0 ;;
        *) fail "unknown option: $1" ;;
    esac
done

[[ $EUID -eq 0 ]] || fail "run this script with sudo; it must create cgroups and attach sched_ext"
[[ $duration =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]] \
    && awk -v value="$duration" 'BEGIN { exit !(value > 0) }' \
    || fail "duration must be a positive finite number"
for item in "$share_a" "$share_b" "$fallback_share"; do
    [[ $item =~ ^[0-9]+$ ]] || fail "shares must be integers from 1 to 10000"
    ((10#$item >= 1 && 10#$item <= 10000)) || fail "shares must be integers from 1 to 10000"
done
if [[ -n $worker_timeout ]]; then
    [[ $worker_timeout =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]] \
        && awk -v value="$worker_timeout" 'BEGIN { exit !(value > 0) }' \
        || fail "worker timeout must be a positive finite number"
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
cd "$repo_root"
build_dir=${CAS_BUILD_DIR:-$repo_root/build}
# Reuse the supplied institute build directory when the repository build does
# not contain the scheduler artifact. CAS_BUILD_DIR can select another build.
if [[ -z ${CAS_BUILD_DIR:-} && ! -x $build_dir/group_scheduler \
    && -x /tmp/cas-build-linux70/group_scheduler ]]; then
    build_dir=/tmp/cas-build-linux70
fi
loader=${loader:-$build_dir/group_scheduler}
bpf_object=${bpf_object:-$build_dir/group_scheduler.bpf.o}
worker=${worker:-$build_dir/cpu_worker}
supervisor=${supervisor:-$build_dir/cpu_supervisor}
preparer=$repo_root/scripts/prepare_custom_scheduler_experiment.py
runner=$repo_root/scripts/run_custom_scheduler_experiment.py
results_dir=$repo_root/results/raw
if [[ -z $libbpf_libdir && -r /tmp/container-aware-scheduler-deps/libbpflib64/libbpf.so.1 ]]; then
    libbpf_libdir=/tmp/container-aware-scheduler-deps/libbpflib64
fi

[[ -d $parent && -r $parent/cgroup.procs ]] || fail "not a readable cgroup-v2 parent: $parent"
awk -v target="$parent" '$0 ~ / - cgroup2 / { found=1 } END { exit !found }' /proc/self/mountinfo \
    || fail "no cgroup-v2 mount is available"
for path in "$loader" "$bpf_object" "$worker" "$supervisor" "$preparer" "$runner"; do
    [[ -r $path ]] || fail "required file is missing or unreadable: $path"
done
for path in "$loader" "$worker" "$supervisor"; do
    [[ -x $path ]] || fail "required executable is missing or not executable: $path"
done
if [[ -n $libbpf_libdir ]]; then
    [[ -d $libbpf_libdir && -r $libbpf_libdir/libbpf.so.1 ]] \
        || fail "libbpf directory is missing libbpf.so.1: $libbpf_libdir"
    libbpf_libdir=$(cd -- "$libbpf_libdir" && pwd)
    export LD_LIBRARY_PATH="$libbpf_libdir${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
resolved_libbpf=$(ldd "$loader" 2>/dev/null | awk '$1 == "libbpf.so.1" && $2 == "=>" { print $3; exit }')
[[ -n $resolved_libbpf ]] || fail "cannot resolve libbpf.so.1 for loader: $loader"
if [[ -n $libbpf_libdir ]]; then
    expected_libbpf=$(readlink -f -- "$libbpf_libdir/libbpf.so.1")
    actual_libbpf=$(readlink -f -- "$resolved_libbpf")
    [[ $actual_libbpf == "$expected_libbpf" ]] \
        || fail "loader resolved libbpf at $actual_libbpf, expected $expected_libbpf"
fi
printf 'Loader libbpf: %s\n' "$(readlink -f -- "$resolved_libbpf")"
state=/sys/kernel/sched_ext/state
[[ -r $state ]] || fail "sched_ext state interface is unavailable: $state"
[[ $(<"$state") == disabled ]] || fail "sched_ext must report disabled before this run (state: $(<"$state"))"

run_tag="cas_demo_${BASHPID}_${RANDOM}"
group_a=$parent/${run_tag}_latency
group_b=$parent/${run_tag}_batch
created=()
capture=$(mktemp "${TMPDIR:-/tmp}/cas-demo-output.XXXXXX")

cleanup() {
    local status=$? path members cleanup_failed=0
    trap - EXIT
    set +e
    for path in "${created[@]}"; do
        [[ -d $path ]] || continue
        if ! members=$(cat -- "$path/cgroup.procs" 2>/dev/null); then
            printf 'WARNING: cannot read cgroup membership; leaving cgroup untouched: %s\n' "$path" >&2
            cleanup_failed=1
            continue
        fi
        if [[ -n $members ]]; then
            printf 'WARNING: leaving non-empty cgroup untouched: %s (PIDs: %s)\n' "$path" "$members" >&2
            cleanup_failed=1
        elif ! rmdir -- "$path"; then
            printf 'WARNING: could not remove temporary cgroup: %s\n' "$path" >&2
            cleanup_failed=1
        fi
    done

    # sudo sets these variables to the invoking user's numeric IDs. Restore
    # ownership only for this run's artifacts, never the whole results folder.
    if [[ ${SUDO_UID:-} =~ ^[0-9]+$ && ${SUDO_GID:-} =~ ^[0-9]+$ ]]; then
        local artifact manifest scheduler_log
        while IFS= read -r artifact; do
            [[ -f $artifact ]] && chown "$SUDO_UID:$SUDO_GID" "$artifact" 2>/dev/null || true
        done < <(sed -n 's/^Result CSV: //p; s/^Manifest: //p' "$capture" 2>/dev/null)
        manifest=$(sed -n 's/^Manifest: //p' "$capture" 2>/dev/null | tail -n 1)
        if [[ -n $manifest && -f $manifest ]]; then
            scheduler_log=$(python3 - "$manifest" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        print(json.load(stream).get("scheduler_log", ""))
except (OSError, ValueError):
    pass
PY
)
            [[ -n $scheduler_log && -f $scheduler_log ]] && chown "$SUDO_UID:$SUDO_GID" "$scheduler_log" 2>/dev/null || true
        fi
    fi
    rm -f -- "$capture"
    if (( cleanup_failed )); then
        exit 1
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -- "$group_a" || fail "cannot create cgroup under $parent; this parent is not writable even as root"
created+=("$group_a")
mkdir -- "$group_b" || fail "cannot create second cgroup under $parent"
created+=("$group_b")
[[ -f $group_a/cgroup.procs && -f $group_b/cgroup.procs ]] || fail "created directories are not cgroup-v2 groups"

identity_a=$(python3 "$preparer" --identify-cgroup "$group_a") || fail "could not obtain kernel cgroup identity for latency group"
identity_b=$(python3 "$preparer" --identify-cgroup "$group_b") || fail "could not obtain kernel cgroup identity for batch group"
id_a=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["cgroup_id"])' <<<"$identity_a")
id_b=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["cgroup_id"])' <<<"$identity_b")
[[ $id_a =~ ^[1-9][0-9]*$ && $id_b =~ ^[1-9][0-9]*$ && $id_a != "$id_b" ]] \
    || fail "kernel returned invalid or duplicate cgroup IDs"

printf 'Temporary groups created and kernel identities verified:\n'
printf '  latency: cgroup_id=%s share=%s path=%s\n' "$id_a" "$share_a" "$group_a"
printf '  batch:   cgroup_id=%s share=%s path=%s\n' "$id_b" "$share_b" "$group_b"
printf '  fallback share: %s\n' "$fallback_share"
printf 'Starting the custom sched_ext experiment. Workers will not start unless attachment is confirmed.\n'

worker_timeout_args=()
if [[ -n $worker_timeout ]]; then
    worker_timeout_args=(--worker-timeout "$worker_timeout")
fi
set +e
python3 "$runner" run \
    --duration "$duration" \
    --fallback-share "$fallback_share" \
    --group "latency:$id_a:$share_a:$group_a" \
    --group "batch:$id_b:$share_b:$group_b" \
    --loader "$loader" --bpf-object "$bpf_object" \
    --worker "$worker" --supervisor "$supervisor" \
    --output-dir "$results_dir" "${worker_timeout_args[@]}" 2>&1 | tee "$capture"
status=${PIPESTATUS[0]}
set -e
exit "$status"
