#!/usr/bin/env bash
# push_final_charts.sh — psu_retrain Step 3.5 final comparison charts (deterministic, fail-soft).
# Args: 1=selected_acc, 2=selected_latency, 3=latency_unit.
set -uo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"
SEL_ACC="${1:?}"
SEL_LATENCY="${2:?}"
LATENCY_UNIT="${3:-ms}"

cd "$ARTIFACTS_DIR" || exit 1

python3 "$SCRIPTS_DIR/metrics_bar.py" --artifacts-dir "$ARTIFACTS_DIR" --selected-acc "$SEL_ACC" > /dev/null || true
python3 "$SCRIPTS_DIR/compare_table.py" --artifacts-dir "$ARTIFACTS_DIR" --selected-latency "$SEL_LATENCY" --selected-acc "$SEL_ACC" --latency-unit "$LATENCY_UNIT" > /dev/null || true
# subnet_profile.py: materializes the selected subnet, writes subnet_structure.md (read by psu_report) + pushes a table chart. fail-soft.
python3 "$SCRIPTS_DIR/subnet_profile.py" --artifacts-dir "$ARTIFACTS_DIR" --latency-unit "$LATENCY_UNIT" > /dev/null || true
