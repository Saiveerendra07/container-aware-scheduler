#!/usr/bin/env bash

set -Eeuo pipefail

usage() {
    cat <<'EOF'
Usage: generate_experiment_report.sh [options] [raw_group_or_cgroup_competition.csv ...]

Analyze existing group-competition and cgroup-competition CSV results, save a unique summary under
results/processed/, and plot that exact summary. No workloads are run.

Options:
  --processed-dir DIR  Summary parent directory (default: results/processed/)
  --graphs-dir DIR     Figure directory (default: graphs/)
  --repeated-summary   Also write repeated-run statistics and Markdown report
  -h, --help           Show this help

With no CSV paths, the analyzer reads both supported result formats from results/raw/.
EOF
}

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
analyzer="$script_dir/analyze_results.py"
plotter="$script_dir/plot_results.py"
processed_dir="$repo_root/results/processed"
graphs_dir=
repeated_summary=0
inputs=()

while (( $# )); do
    case $1 in
        --processed-dir|--graphs-dir)
            option=$1
            if (( $# < 2 )) || [[ -z $2 || $2 == -* ]]; then
                printf 'Error: %s requires a directory path.\n' "$option" >&2
                usage >&2
                exit 2
            fi
            if [[ $option == --processed-dir ]]; then
                processed_dir=$2
            else
                graphs_dir=$2
            fi
            shift 2
            ;;
        --repeated-summary)
            repeated_summary=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            inputs+=("$@")
            break
            ;;
        -* )
            printf 'Error: unknown option: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
        *)
            inputs+=("$1")
            shift
            ;;
    esac
done

python_cmd=${PYTHON:-python3}
if ! command -v "$python_cmd" >/dev/null 2>&1; then
    printf 'Error: Python interpreter not found: %s\n' "$python_cmd" >&2
    exit 127
fi
if [[ ! -f $analyzer || ! -f $plotter ]]; then
    printf 'Error: analyzer or plotting script is missing from %s.\n' "$script_dir" >&2
    exit 1
fi

mkdir -p -- "$processed_dir"
timestamp=$(date -u '+%Y%m%dT%H%M%S.%NZ')
report_dir=$(mktemp -d "$processed_dir/experiment_report_${timestamp}_XXXXXX")
summary="$report_dir/analyzed_results.csv"

printf 'Analyzing existing results; no workloads will be run.\n'
if "$python_cmd" "$analyzer" "${inputs[@]}" --output "$summary"; then
    :
else
    status=$?
    printf 'Analysis failed (exit %s); report directory: %s\n' "$status" "$report_dir" >&2
    exit "$status"
fi

printf 'Summary CSV: %s\n' "$summary"
if (( repeated_summary )); then
    repeated_script="$script_dir/summarize_repeated_runs.py"
    if "$python_cmd" "$repeated_script" "${inputs[@]}" --output-dir "$report_dir"; then
        :
    else
        status=$?
        printf 'Repeated-run analysis failed (exit %s); report directory: %s\n' \
            "$status" "$report_dir" >&2
        exit "$status"
    fi
fi
if [[ -n $graphs_dir ]]; then
    if "$python_cmd" "$plotter" "$summary" --graphs-dir "$graphs_dir"; then
        :
    else
        status=$?
        printf 'Plot generation failed (exit %s). The summary remains at %s\n' \
            "$status" "$summary" >&2
        exit "$status"
    fi
else
    if "$python_cmd" "$plotter" "$summary"; then
        :
    else
        status=$?
        printf 'Plot generation failed (exit %s). The summary remains at %s\n' \
            "$status" "$summary" >&2
        exit "$status"
    fi
fi
