#!/bin/bash
# 跑预写 mip_select.py 恰好一次；stdout 单行 JSON 即最终回复。
# 依赖：ORCA_ARTIFACTS_DIR / ORCA_WORKFLOWS_ROOT（orca spawn 注入）。
set -euo pipefail
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

TARGET_LAT="${1:-}"
REDUCTION="${2:-0.5}"
UNIT="${3:-ms}"

TARGET_LAT_ARG=""
if [ -n "$TARGET_LAT" ]; then
  TARGET_LAT_ARG="--target-latency $TARGET_LAT"
fi

python3 "$ORCA_WORKFLOWS_ROOT/agents/_puzzle_scripts/mip_select.py" \
  --scores "$ORCA_ARTIFACTS_DIR/scores.jsonl" \
  --latency-table "$ORCA_ARTIFACTS_DIR/latency_table.jsonl" \
  --baseline-metrics "$ORCA_ARTIFACTS_DIR/baseline_metrics.json" \
  --latency_reduction_target "$REDUCTION" \
  --latency-unit "$UNIT" \
  --output_dir "$ORCA_ARTIFACTS_DIR" \
  $TARGET_LAT_ARG
