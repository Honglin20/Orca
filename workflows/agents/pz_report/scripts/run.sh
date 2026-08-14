#!/bin/bash
# 跑预写 gate_report.py 恰好一次；stdout 单行 JSON 即最终回复。
# 依赖：ORCA_ARTIFACTS_DIR / ORCA_WORKFLOWS_ROOT（orca spawn 注入）。
set -euo pipefail
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

UNIT="${1:-ms}"
LATENCY_SCRIPT="${2:-}"
REDUCTION="${3:-0.5}"

shopt -s nullglob
OPT_FLAT=( "$ORCA_ARTIFACTS_DIR/"*_optimized_flat.py )
[ "${#OPT_FLAT[@]}" -ge 1 ] || { echo "FATAL: no *_optimized_flat.py in $ORCA_ARTIFACTS_DIR" >&2; exit 1; }
OPT_FLAT="${OPT_FLAT[0]}"

python3 "$ORCA_WORKFLOWS_ROOT/agents/_puzzle_scripts/gate_report.py" \
  --final_model "$ORCA_ARTIFACTS_DIR/runs/retrain/final_model.pt" \
  --baseline_metrics "$ORCA_ARTIFACTS_DIR/baseline_metrics.json" \
  --optimized_flat "$OPT_FLAT" \
  --adapters "$ORCA_ARTIFACTS_DIR/puzzle_adapters.py" \
  --manifest "$ORCA_ARTIFACTS_DIR/manifest.yaml" \
  --latency_unit "$UNIT" \
  --latency_script_path "$LATENCY_SCRIPT" \
  --latency_reduction_target "$REDUCTION" \
  --output_dir "$ORCA_ARTIFACTS_DIR"
