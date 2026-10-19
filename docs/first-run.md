# First CPU workload run

## Environment

- Operating system: Ubuntu 24.04.5 LTS (Noble Numbat)
- Kernel: `6.6.87.2-microsoft-standard-WSL2`
- Available logical CPUs: 12 (`getconf _NPROCESSORS_ONLN`)
- Compiler: GCC 13.3.0 (`gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0`)

## Build

Command:

```sh
gcc -O2 -Wall -Wextra -std=c11 workloads/cpu_worker.c -o workloads/cpu_worker
```

Result: exit status 0; no compiler warnings or errors were printed.

## Run

Command: `./workloads/cpu_worker 3`

Concurrent workers: 1

Actual program output:

```text
duration_seconds=3.000
iterations=106272851
result=3665561955183556197
```

Exit status: 0.

## Observations

The program reported its requested duration as 3.000 seconds and counted 106272851 loop iterations. The iteration count and result are the program's measured/reported output from this run. This workload does not collect CPU usage, request latency, fairness, throttling, or scheduler overhead, so no conclusions about those metrics can be drawn from this run.

Errors: none observed during compilation or execution.
