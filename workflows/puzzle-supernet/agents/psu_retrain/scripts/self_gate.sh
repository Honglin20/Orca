#!/usr/bin/env bash
# self_gate.sh — psu_retrain Step 0 self-gate on upstream-generated scripts.
# MISSING_UPSTREAM_SCRIPTS if retrain.py or run_retrain.sh absent; OK otherwise.
set -uo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

if [ ! -s retrain.py ] || [ ! -s run_retrain.sh ]; then
  printf "%s" "upstream psu_retrain_script did not produce retrain.py/run_retrain.sh" > "$ARTIFACTS_DIR/.psu_retrain_assessment.txt"
  echo "MISSING_UPSTREAM_SCRIPTS"
else
  echo "OK"
fi
