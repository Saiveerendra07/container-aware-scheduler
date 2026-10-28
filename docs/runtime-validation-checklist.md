# First runtime validation checklist

This checklist is for the first carefully controlled runtime validation of the
custom `sched_ext` scheduler. The scheduler, its weighted behavior, and its
fairness results have **not** been proven at runtime. Commands in the read-only
and build sections do not attach the scheduler. The explicit `run` command
near the end does attach it and must only be used when every stop condition
below has been cleared.

Run commands from the repository root. Keep the terminal running the
experiment open so its signal handling and cleanup can complete. Do not run
the first test on a production or otherwise critical system.

## 1. Record repository state and run non-privileged checks

First identify existing changes and raw results. Preserve them; do not clean,
reset, or overwrite them as part of this validation:

```sh
git status --short
git diff --check
git diff --cached --check
find results/raw -maxdepth 1 -type f -printf '%f\n' 2>/dev/null | sort
```

If the working tree is not clean, record which changes are intentional and
ensure the validation will use this exact source revision. Stop if you cannot
distinguish existing raw results from files produced by this test.

Run the complete non-privileged test and syntax checks before building:

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
bash -n scripts/*.sh
gcc -O2 -Wall -Wextra -Werror -std=c11 -fsyntax-only workloads/cpu_worker.c
gcc -O2 -Wall -Wextra -Werror -std=c11 -fsyntax-only workloads/cpu_supervisor.c
git diff --check
```

These tests use synthetic/mocked cases in temporary directories. They do not
test real cgroup migration, BPF attachment, sched_ext dispatch, or shutdown.
Stop if any check fails; do not treat test success as runtime evidence.

## 2. Check host kernel and build prerequisites

These are read-only diagnostics. Do not load modules or change kernel, sysctl,
cgroup, or security settings to make a check pass.

Start with the consolidated project diagnostic:

```sh
python3 scripts/check_scheduler_readiness.py
```

Use `--scx-root /absolute/path/to/scx-c-examples` if the reference checkout is
not at the Makefile default (`~/scx-c-examples`). This command reports every
missing prerequisite and a suggested next action, and exits nonzero if a
required prerequisite is missing, the sched_ext state interface is unreadable,
or its state is not `disabled`. It does not install or change anything. Its
WSL2 detection is heuristic only. Passing this check is not proof that the
kernel, verifier, or scheduler attachment is compatible; continue with the
individual checks below and stop at any failed gate.

```sh
uname -a
uname -r
test -r /sys/kernel/sched_ext/state && cat /sys/kernel/sched_ext/state
test -r /sys/kernel/btf/vmlinux && ls -lh /sys/kernel/btf/vmlinux
```

The runner requires `/sys/kernel/sched_ext/state` to be readable and report
`disabled` before check/run. The project documents the task-cgroup lookup kfunc
as available on Linux 6.12 and later, but the version number alone does not
prove the running kernel has the needed configuration and kfunc. The readable
state file and the later explicit attach attempt are separate gates. If the
state file is missing, unreadable, or not `disabled`, stop.

If the host publishes its kernel config, inspect it without changing it:

```sh
if test -r "/boot/config-$(uname -r)"; then
  grep -E '^CONFIG_SCHED_CLASS_EXT=' "/boot/config-$(uname -r)"
elif test -r /proc/config.gz; then
  zgrep -E '^CONFIG_SCHED_CLASS_EXT=' /proc/config.gz
else
  echo 'Kernel config is not exposed at the usual paths; availability remains unverified.'
fi
```

Check compiler, BPF target, bpftool, pkg-config, and libbpf:

```sh
cc --version
gcc --version
clang --version
clang -print-targets | grep -i bpf
bpftool version
pkg-config --version
pkg-config --modversion libbpf
pkg-config --cflags --libs libbpf
```

The Makefile requires libbpf 1.5 or newer. `clang -print-targets` should show
the BPF target. A command being present is not enough: stop if the BPF target
is absent, `bpftool` cannot run, pkg-config cannot find libbpf, or the version
is below the requirement.

### Build libbpf privately when needed

The Makefile requires libbpf **1.5 or newer**, not a particular patch release.
The following uses the exact upstream `v1.5.0` tag, which satisfies that
minimum, and stages its headers, library, and pkg-config file under `/tmp`.
It does not install a package or alter system library paths. The source build
uses the host's existing `libelf` and `zlib` development metadata; stop if
those are unavailable rather than changing system packages during validation.

```sh
DEPS_ROOT=/tmp/container-aware-scheduler-deps
LIBBPF_SRC="$DEPS_ROOT/libbpf"
LIBBPF_BUILD="$DEPS_ROOT/libbpf-build"
LIBBPF_PREFIX="$DEPS_ROOT/prefix"
LIBBPF_COMMIT=09b9e83102eb8ab9e540d36b4559c55f3bcdb95d
mkdir -p "$DEPS_ROOT"
git clone --depth 1 --branch v1.5.0 \
  https://github.com/libbpf/libbpf.git "$LIBBPF_SRC"
test "$(git -C "$LIBBPF_SRC" rev-parse HEAD)" = "$LIBBPF_COMMIT"
make -C "$LIBBPF_SRC/src" OBJDIR="$LIBBPF_BUILD" \
  PREFIX="$LIBBPF_PREFIX" LIBDIR="$LIBBPF_PREFIX/lib" \
  INCLUDEDIR="$LIBBPF_PREFIX/include"
make -C "$LIBBPF_SRC/src" install OBJDIR="$LIBBPF_BUILD" \
  PREFIX="$LIBBPF_PREFIX" LIBDIR="$LIBBPF_PREFIX/lib" \
  INCLUDEDIR="$LIBBPF_PREFIX/include"
export PKG_CONFIG_PATH="$LIBBPF_PREFIX/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
export LD_LIBRARY_PATH="$LIBBPF_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
pkg-config --modversion libbpf
pkg-config --cflags --libs libbpf
```

The version query should print `1.5.0`; pkg-config flags should point at the
private prefix and include `-L.../prefix/lib -lbpf`. `PKG_CONFIG_PATH` selects
the metadata for Make and the diagnostic, and the resulting `-I`/`-L` flags
select the private headers and link library. `LD_LIBRARY_PATH` is needed when
running the resulting dynamically linked loader so it resolves the same local
`libbpf.so.1`; it does not select headers or affect compilation. Keep these
exports in the shell used for preflight, build, and any later loader inspection.

The Makefile default reference-header root is `~/scx-c-examples`, from the
`sched-ext/scx-c-examples` project. The required path layout alone does not
identify a compatible revision: this scheduler repository has no pinned
reference commit or version. Do not use an arbitrary checkout or current
upstream `main` as an assumed match. Use the institute-approved checkout,
record its commit, and let the actual BPF and loader compilation below verify
the APIs. Set `SCX_ROOT` to that existing checkout and inspect the exact files
consumed by this Makefile:

```sh
SCX_ROOT=/absolute/path/to/institute-approved/scx-c-examples
git -C "$SCX_ROOT" rev-parse HEAD
test -f "$SCX_ROOT/scheds/include/scx/common.bpf.h"
test -f "$SCX_ROOT/scheds/include/scx/common.h"
test -d "$SCX_ROOT/scheds/include/bpf-compat"
test -d "$SCX_ROOT/scheds/include/lib"
```

If the checkout is elsewhere, set its real absolute path. Do not clone, install,
or update the sched_ext reference tree during the validation window without
the system owner's approval. The expected running-kernel BTF path is
`/sys/kernel/btf/vmlinux`; check it again before build:

```sh
test -r /sys/kernel/btf/vmlinux
```

Stop if BTF, either `scx` header, `bpf-compat`, `lib`, libbpf >= 1.5, Clang's
BPF backend, or bpftool is unavailable. The documented commands do not verify
that the reference checkout is the right revision for the target kernel; use
the institute's known sched_ext reference tree and record its commit:

```sh
git -C "$SCX_ROOT" rev-parse HEAD
```

## 3. Build from the inspected Makefile

The institute Linux host should use its known sched_ext reference checkout;
do not substitute an arbitrary or updated checkout without recording and
reviewing its revision. The Makefile expects these exact paths under
`$SCX_ROOT/scheds/include`:

```text
scx/common.bpf.h
scx/common.h
bpf-compat/                 (include directory)
lib/                        (include directory)
```

The default `SCX_ROOT` is `~/scx-c-examples`. The Makefile also requires
`pkg-config` metadata for libbpf **1.5 or newer**. `pkg-config --cflags
libbpf` supplies compile flags; `pkg-config --libs libbpf` supplies the libbpf
link flag. The userspace loader additionally links `-lelf -lz -lzstd -lpthread`.
Clang must expose its BPF target, bpftool must support BTF dumping and skeleton
generation, and `/sys/kernel/btf/vmlinux` must be readable. The preflight
prints the resolved commands, include paths, metadata, and outputs for both
the BPF object and userspace loader:

```sh
python3 scripts/check_scheduler_readiness.py --build-only --scx-root "$SCX_ROOT"
```

For build prerequisites alone, use `--build-only`; this still reports
`/sys/kernel/sched_ext/state` but does not treat `enabled` as a compile blocker.
The regular diagnostic gates runtime readiness on that state being readable
and `disabled`.

After the read-only checks pass, build the BPF scheduler and workload
executables. These commands write generated files only under `build/`; they do
not attach the scheduler or start the worker:

```sh
make SCX_ROOT="$SCX_ROOT" VMLINUX_BTF=/sys/kernel/btf/vmlinux
make workloads
```

The Makefile creates `build/vmlinux.h`,
`build/group_scheduler.bpf.o`, `build/group_scheduler.skel.h`,
`build/group_scheduler`, `build/cpu_worker`, and `build/cpu_supervisor`.
Check those actual outputs before proceeding:

```sh
test -x build/group_scheduler
test -r build/group_scheduler.bpf.o
test -x build/cpu_worker
test -x build/cpu_supervisor
ls -lh build/group_scheduler build/group_scheduler.bpf.o build/cpu_worker build/cpu_supervisor
```

Do not run `make clean` if `build/` contains anything that must be preserved.
If build fails, save the diagnostics and stop. Do not substitute different
headers or silently use a different kernel's BTF.

## 4. Prepare two explicit delegated cgroups

Use two **existing** cgroup-v2 directories inside an already delegated parent.
This experiment runner does not create cgroups, enable controllers, set
`cpu.weight`, set `cpu.max`, or move unrelated processes. Confirm that the
workload cgroups are available for this test and do not contain unrelated
tasks. The runner's supervisor moves each new worker into its configured
cgroup before the worker executes.

The attached scheduler is system-wide. Tasks outside the two configured cgroup
IDs are scheduled in one shared fallback group. Do not use a shared institute
PC until its operator confirms that a system-wide sched_ext experiment is
approved and the machine is available for that test. Read the current cgroup
membership and controls without changing them:

```sh
for cgroup in /sys/fs/cgroup/delegated/latency /sys/fs/cgroup/delegated/batch; do
  test -d "$cgroup"
  test -r "$cgroup/cgroup.procs"
  cat "$cgroup/cgroup.procs"
  cat "$cgroup/cpu.weight" 2>/dev/null || true
  cat "$cgroup/cpu.max" 2>/dev/null || true
done
```

If either group contains unrelated tasks, stop and coordinate before using it.
Do not move processes manually as part of this checklist.

Read the exact kernel IDs using the preparation tool. It uses
`name_to_handle_at(FILEID_KERNFS)` and verifies the mount is cgroup2; do not
substitute `stat` inode numbers or IDs copied from another host/run:

```sh
python3 scripts/prepare_custom_scheduler_experiment.py \
  --identify-cgroup /sys/fs/cgroup/delegated/latency
python3 scripts/prepare_custom_scheduler_experiment.py \
  --identify-cgroup /sys/fs/cgroup/delegated/batch
```

Copy the `cgroup_id` values printed for these exact paths. If the interface is
unavailable or either path is not verified as cgroup2, stop. Do not infer or
guess an ID.

The example below configures scheduler shares 100 and 300 plus fallback share
100. These are scheduler configuration values, **not** `cpu.weight` settings
or CPU quotas. The runner only reads `cpu.weight` and `cpu.max` for metadata.
Replace both example IDs with the IDs just verified on this host:

```sh
python3 scripts/run_custom_scheduler_experiment.py prepare --duration 2 \
  --fallback-share 100 --output-dir results/raw \
  --group latency:12345:100:/sys/fs/cgroup/delegated/latency \
  --group batch:12346:300:/sys/fs/cgroup/delegated/batch
```

`prepare` writes unique CSV, JSON manifest, and log-path placeholder files; it
does not attach or start workers. Record the printed paths. The manifest is a
record only: the runner does not take a manifest as input, so pass the same
IDs, shares, duration, and paths again to `check` and `run`. Do not edit or
reuse a previous result file.

The run/check process must be able to read kernel BTF and the sched_ext state,
open each cgroup's `cgroup.procs` for writing, and have an effective
`CAP_BPF` or `CAP_SYS_ADMIN` bit. The permission check opens but does not write
`cgroup.procs`. These capability bits are only a preflight indicator; LSM,
kernel policy, verifier, or attach restrictions may still reject the load.
Do not use `sudo` to bypass a failed check unless the institute's system owner
explicitly authorizes the test and recovery arrangement.

Before attachment, perform the full non-attaching check with the same inputs:

```sh
python3 scripts/run_custom_scheduler_experiment.py check --duration 2 \
  --fallback-share 100 --output-dir results/raw \
  --group latency:12345:100:/sys/fs/cgroup/delegated/latency \
  --group batch:12346:300:/sys/fs/cgroup/delegated/batch
```

`check` verifies the loader and BPF object, worker and supervisor, BTF, the
disabled sched_ext state, cgroup identity, writable `cgroup.procs`, shares,
and a capability bit. It does not load BPF or start a workload. Stop if it
fails or if the state changes after the check.

## 5. Staged runtime validation

Do not skip stages. At every stop condition, preserve logs/results and do not
continue to repeated runs.

### Stage A: explicit short run

Only after all previous checks pass, the two cgroups are confirmed safe, and a
recovery operator/path is available, explicitly request the short run:

```sh
python3 scripts/run_custom_scheduler_experiment.py run --duration 2 \
  --fallback-share 100 --output-dir results/raw \
  --group latency:12345:100:/sys/fs/cgroup/delegated/latency \
  --group batch:12346:300:/sys/fs/cgroup/delegated/batch
```

This is the first command in this checklist that may attach the scheduler or
start CPU workloads. The runner repeats prerequisite checks. The loader emits
`GROUP_SCHEDULER_ATTACHED` only after
`bpf_map__attach_struct_ops()` succeeds; the runner starts no worker if that
readiness line is absent. Attachment failure is an unsuccessful run, not a
request to use ordinary Linux scheduling. After readiness, one cgroup-aware
supervisor per group verifies worker membership in `cgroup.procs` before
releasing that worker to exec.

The runner prints the unique CSV and JSON manifest paths. Preserve the
associated `.scheduler.log`. A nonzero runner exit, `measurement_status`
other than `complete`, unexpected loader exit, missing row, or shutdown status
other than `stopped` is a failed validation. Do not interpret those rows as
scheduler fairness data.

### Stage B: verify shutdown and raw output

After the runner exits, check the kernel state and that no loader remains:

```sh
cat /sys/kernel/sched_ext/state
ps -C group_scheduler -o pid=,args= || true
```

Proceed only when sched_ext reports `disabled` and the experiment loader is no
longer running. Inspect the exact printed raw CSV and manifest, for example:

```sh
head -n 5 results/raw/PASTE_PRINTED_CSV_PATH_HERE
python3 -m json.tool results/raw/PASTE_PRINTED_MANIFEST_PATH_HERE
```

Confirm two rows with the exact group names, verified cgroup IDs, configured
shares 100/300, `fallback_share` 100, separate cgroup-control columns, valid
elapsed and CPU times, zero exit status and termination signal, complete
status, `scheduler_attached=true`, and `scheduler_shutdown_status=stopped`.
The CSV is accepted by the fairness evaluator. Write evaluation output to a
new path, never over the raw input:

```sh
python3 scripts/evaluate_fairness.py \
  results/raw/PASTE_PRINTED_CSV_PATH_HERE \
  --output results/processed/PASTE_NEW_ANALYSIS_PATH_HERE.csv
```

This evaluation describes only the measured run. It does not by itself prove
that observed allocation is correct or repeatable. If the CSV has `NA`, an
incomplete status, or errors, stop and diagnose before trying again.

### Stage C: repeated runs

Only after the first run has clean shutdown, complete raw measurements, and
reviewed output, perform additional runs with new unique outputs. Record the
host/kernel/reference-header commits, exact command, configured shares,
duration, cgroup paths/IDs, cgroup controls, runner exit code, and manifest for
each run. Do not pool incomplete runs or overwrite prior raw data. Decide the
repeat count and analysis plan before collecting repeated measurements.

## 6. Recovery and stop conditions

### Attachment/load failure

If the loader exits before its readiness line, the runner must not have
started workers. Keep the scheduler log and unsuccessful CSV/manifest. Check
the reported libbpf/verifier error, kernel log, BTF, reference headers, kernel
configuration, capabilities, LSM policy, and cgroup identity. Confirm
`/sys/kernel/sched_ext/state` is `disabled` before another attempt. Do not
retry with guessed IDs, changed permissions, or a different scheduler binary
without repeating the whole check.

### Interruption

Use Ctrl+C in the foreground runner and allow it to finish. The runner handles
SIGINT/SIGTERM by terminating its supervisors; each supervisor cleans up its
worker process group. The runner then sends SIGTERM to the loader and waits for
loader exit/link destruction. Do not close the session or use SIGKILL as the
first response. After the runner exits, verify the state is `disabled` and
`ps -C group_scheduler -o pid=,args=` prints no loader process.

If the foreground runner is unresponsive but the loader is still present,
identify the exact loader PID before signaling it; do not signal a PID based
only on a copied example:

```sh
ps -C group_scheduler -o pid=,args=
ps -fp 12345
kill -TERM 12345
```

Replace `12345` in both commands with the single PID identified in the first
command; do not run the example PID literally.

This asks the loader to destroy its link. The runner should observe that exit,
stop workers, and mark the run incomplete. Then verify both the runner/loader
process state and sched_ext state. Never start another test while the state is
not `disabled`.

### Scheduler does not shut down cleanly

If the runner reports `scheduler_shutdown_status` other than `stopped`, or
does not return after SIGTERM, leave the terminal/session available and do not
start another scheduler. Preserve the manifest and scheduler log. Check the
loader process, `/sys/kernel/sched_ext/state`, kernel log, and institute
operator guidance. The loader reports `bpf_link__destroy()` errors and the
runner waits instead of intentionally exiting while its loader may still own
the link. This code has not been runtime-proven to recover from a kernel hang,
uninterruptible task, external SIGKILL, or power loss.

Do not improvise a sysfs write, unload a kernel module, or use `kill -9` as a
recovery step. If the link remains active or shutdown safety is uncertain,
stop and use the institute's documented, kernel-version-specific sched_ext
recovery procedure with the system operator. If no such procedure is
available, do not begin runtime testing on that host.

### Hard stop conditions

Stop before attachment if any of these apply:

- sched_ext state interface is missing, unreadable, or not `disabled`;
- kernel BTF, compatible sched_ext headers, BPF-capable Clang, bpftool, or
  libbpf >= 1.5 is missing or its source/version cannot be established;
- cgroup IDs cannot be read from the exact cgroup-v2 paths or verification
  reports a mismatch;
- two distinct delegated cgroups are not available, or `cgroup.procs` cannot
  be opened for writing by the experiment user;
- loader/object/worker/supervisor is missing, check mode fails, or the exact
  configured shares and fallback share are not recorded;
- there is no operator-approved recovery path or safe place to preserve unique
  raw results.

Stop after attachment if readiness is absent, any workload placement fails,
the loader exits unexpectedly, any group measurement is incomplete, or the
scheduler cannot be shown disabled after shutdown. Do not proceed to repeated
runs until the cause is resolved and the short run passes all inspection
steps.

## Runtime claims

This checklist prepares for validation; it is not evidence of runtime success.
Build success and synthetic tests do not establish safe attachment, accurate
cgroup dispatch, weighted CPU allocation, fairness, or reliable teardown.
Those remain unproven until the complete staged procedure succeeds on the
target supported kernel and the raw records are independently reviewed.
