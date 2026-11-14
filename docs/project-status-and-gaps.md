# Project scope, implementation status, and remaining gaps

This document maps the original two-page project brief to the implementation currently in this repository. It is intended to keep the final report and demonstration accurate.

## 1. Original project question

The brief asks whether group-level scheduling with changing shares can reduce tail latency and switching overhead compared with ordinary Linux scheduling and cgroup CPU controls.

The brief's motivation has two parts:

1. CPU quota throttling can pause all runnable threads in a container after its quota is consumed, potentially delaying requests even when CPUs are otherwise idle.
2. Scheduling large numbers of individual threads can increase scheduling and migration overhead.

These are motivations and hypotheses. They are not findings established by the current repository's measurements.

## 2. Scope-to-implementation map

| Item in the project brief | Current repository status | Evidence boundary |
| --- | --- | --- |
| Linux sched_ext/eBPF group scheduler | Implemented as experimental BPF struct-ops scheduler and userspace loader | Compiled successfully on the previously checked institute Linux 7.0 host; not runtime-attached |
| Per-container/group CPU shares | Static per-group shares and fallback share are accepted by the loader | Do not describe them as dynamically changing shares |
| CPU usage accounting and dispatch by group | Group scheduler source includes weighted virtual-time accounting and per-group dispatch queues | Runtime behavior remains unverified |
| Multi-threaded web-style service and load generator | Not implemented in the current documented workload set | Current workload is synthetic CPU-bound worker; no request latency distribution |
| Batch workload | Synthetic CPU-bound workload can represent a compute/batch-style task at a basic level | Not a realistic compile/backup benchmark |
| Noisy container with hundreds of threads | Existing competition runner supports concurrent CPU workers, but the target noisy-container scenario has not been validated as specified in the brief | Do not claim the large-thread stress scenario is complete |
| Dynamic share controller based on demand/latency | Not implemented | Shares are configured as run parameters; no controller watches latency or updates them |
| Normal Linux baseline | Implemented and measured with CPU workload runners | These are baseline results under the active default scheduler |
| cgroup CPU weights / limits | Cgroup-v2 experiment runner and analysis path are implemented | Depends on delegated writable cgroup-v2 access; distinguish these from sched_ext runs |
| Custom scheduler comparison | Experiment harness is implemented and guarded by attachment/identity/completion/shutdown checks | No successful custom-scheduler runtime run or performance comparison yet |
| p99 request latency | Not measured | No request-serving workload or latency load generator currently provides p99 |
| Fairness / weighted share error | Analysis tools exist for compatible group/cgroup measurements | Existing baseline records do not prove custom scheduler fairness; no custom scheduler data |
| Context switches / CPU migrations / cache misses | Not currently collected as a validated custom scheduler comparison | Do not claim reduced switching or migration overhead |
| Repeated runs and charts | Analysis, plotting, and repeated-run summary tooling exist | Only analyze compatible completed records; no statistical significance claim from descriptive summaries |

## 3. What is currently demonstrable

The following can be demonstrated from the current WSL2 environment:

- CPU worker and supervisor build.
- Baseline CPU competition run under the ordinary active Linux scheduler.
- Shell syntax checks.
- Python automated test suite (106 tests passed in the latest recorded WSL run).
- Dashboard and analysis workflow using available baseline/fixture data.
- Custom scheduler source, loader, experiment harness, and root-run demo wrapper can be inspected; the wrapper's help and syntax can be checked.

The current WSL2 kernel does not expose `/sys/kernel/sched_ext/state`, so it cannot provide evidence of a real sched_ext attach or custom-scheduler run.

The institute host previously compiled the custom scheduler against its target-kernel BTF and a compatible sched_ext header worktree. However, the observed cgroup-v2 mount was read-only for the experiment setup and the user lacked the necessary capabilities/delegation. No custom scheduler runtime result was obtained there either.

## 4. Claims that are safe for the final presentation

Use statements such as:

- "We implemented an experimental sched_ext/eBPF group scheduler with configured group shares and weighted virtual-time accounting."
- "We built a guarded experiment harness that checks cgroup identity and only launches workers after it observes scheduler attachment."
- "We validated workload builds and 106 automated tests in WSL2."
- "We compiled the BPF object and userspace loader on the previously checked institute Linux host."
- "Our baseline and cgroup experiment paths are implemented, but custom scheduler runtime validation is pending."
- "The current measurements do not establish lower p99 latency, lower context-switch/migration overhead, or improved fairness from the custom scheduler."

Avoid statements such as:

- "Our scheduler reduces p99 latency."
- "Our scheduler prevents CPU throttling."
- "Our scheduler dynamically adjusts shares."
- "Our scheduler reduces context switches or migrations."
- "The custom scheduler outperforms Linux or cgroup CPU controls."
- "The custom scheduler has been successfully deployed or tested at runtime."

## 5. Highest-priority remaining work

For the next implementation phase, prioritize in this order:

1. Obtain access to a compatible Linux host with sched_ext enabled, readable target-kernel BTF, required BPF capabilities, and an authorized writable cgroup-v2 parent.
2. Complete the staged short runtime test and verify attach, worker placement, measurement completion, and clean scheduler shutdown.
3. Add a request/latency workload and load generator; report p50, p95, and p99 latency rather than using CPU time as a proxy.
4. Implement a demand/latency-aware share controller only after static-share scheduling is runtime-validated.
5. Collect context-switch and CPU-migration counters if the project continues to claim switching-overhead benefits.
6. Run repeated, controlled comparisons for default Linux, cgroup `cpu.weight`, cgroup `cpu.max`, and the custom scheduler, documenting kernel, CPU topology, workload, duration, and system load.

If runtime access remains unavailable, present the work as an implementation and test-harness milestone, report the environment blocker, and explicitly list the runtime and performance evaluation as future work. A cgroup-only study is a possible fallback, but it is not a substitute for claiming that the custom scheduler was evaluated.

## 6. Relationship to the vBOIDs reference

The brief cites vBOIDs as prior work and explicitly says not to claim that container-level scheduling itself is novel. This project currently implements a different and narrower mechanism: a sched_ext group-share scheduler with weighted virtual-time accounting.

Do not claim to implement vBOID abstractions, two-level BOID scheduling, migration cooldown, or cache/TLB locality optimizations unless those mechanisms are added and validated separately. The final report should position vBOIDs as motivation/related work and describe this implementation on its own terms.
