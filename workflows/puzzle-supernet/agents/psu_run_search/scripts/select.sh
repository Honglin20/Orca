#!/usr/bin/env bash
# select.sh — psu_run_search Step 2.8 architecture selection (deterministic).
# Arg 1: target latency; Arg 2: latency unit. Success → stdout JSON lands in .selected_arch.json;
# failure → write null sentinel (node_failed forbidden).
set -uo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"
TARGET_LATENCY="${1:?}"
LATENCY_UNIT="${2:-ms}"

cd "$ARTIFACTS_DIR" || exit 1

if python3 "$ARTIFACTS_DIR/select_architecture.py" \
    --target-latency "$TARGET_LATENCY" \
    --latency-unit "$LATENCY_UNIT" \
    --search-results "$ARTIFACTS_DIR/search_results.jsonl" \
    > "$ARTIFACTS_DIR/.selected_arch.json" 2>"$ARTIFACTS_DIR/.select_stderr.txt"; then
  echo "SELECT_OK"
else
  # failure safety net: write falsy sentinel (node_failed forbidden)
  printf '%s\n' "{\"selected_arch\":null,\"selected_acc\":0,\"selected_latency\":0,\"latency_unit\":\"$LATENCY_UNIT\",\"pareto_size\":0,\"select_reason\":\"none\"}" \
    > "$ARTIFACTS_DIR/.selected_arch.json"
  echo "SELECT_FAILED — wrote null sentinel to .selected_arch.json"
fi
printf 'true' > "$ARTIFACTS_DIR/.select_attempt"
