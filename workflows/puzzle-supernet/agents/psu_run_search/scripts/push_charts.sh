#!/usr/bin/env bash
# push_charts.sh — psu_run_search Step 2.7 chart push (deterministic, fail-soft).
# Arg 1: latency unit (ms/us/s). stdout/stderr fully discarded by the caller.
set -uo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"
LATENCY_UNIT="${1:-ms}"

cd "$ARTIFACTS_DIR" || exit 1

python3 "$SCRIPTS_DIR/pareto.py" --artifacts-dir "$ARTIFACTS_DIR" --latency-unit "$LATENCY_UNIT" > /dev/null || true
python3 "$SCRIPTS_DIR/search_table.py" --artifacts-dir "$ARTIFACTS_DIR" --latency-unit "$LATENCY_UNIT" > /dev/null || true
python3 "$SCRIPTS_DIR/latency_dist.py" --artifacts-dir "$ARTIFACTS_DIR" --latency-unit "$LATENCY_UNIT" > /dev/null || true
# full_supernet_latency.py: measure the fully-expanded supernet's real latency, writes .full_supernet_latency.json
# for psu_retrain's compare_table to prefer. fail-soft: torch missing/measurement fails → no file written + exit 0.
python3 "$SCRIPTS_DIR/full_supernet_latency.py" --artifacts-dir "$ARTIFACTS_DIR" --latency-unit "$LATENCY_UNIT" > /dev/null || true
