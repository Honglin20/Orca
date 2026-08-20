#!/bin/bash
# 终端 reporter 唯一入口：成功路径（final ckpt + baseline_metrics + optimized_flat 齐）先跑
# gate_report.py 产 gate_result.json；失败路径（缺任一）跳过 gate，直接 emit_report.py 判终态。
# stdout 单行 JSON（emit_report.py 打印）即最终回复。
# 依赖：ORCA_ARTIFACTS_DIR / ORCA_WORKFLOWS_ROOT / ORCA_AGENT_RESOURCES（orca spawn 注入）。
set -uo pipefail
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

UNIT="${1:-ms}"
LATENCY_SCRIPT="${2:-}"
REDUCTION="${3:-0.5}"

# 成功路径判定：final ckpt + baseline_metrics + optimized_flat 三者齐（gate 前置件）。
if [ -f "$ORCA_ARTIFACTS_DIR/runs/retrain/final_model.pt" ] && [ -f "$ORCA_ARTIFACTS_DIR/baseline_metrics.json" ]; then
  shopt -s nullglob
  OPT_FLAT=( "$ORCA_ARTIFACTS_DIR/"*_optimized_flat.py )
  if [ "${#OPT_FLAT[@]}" -ge 1 ]; then
    python3 "$ORCA_WORKFLOWS_ROOT/agents/_puzzle_scripts/gate_report.py" \
      --final_model "$ORCA_ARTIFACTS_DIR/runs/retrain/final_model.pt" \
      --baseline_metrics "$ORCA_ARTIFACTS_DIR/baseline_metrics.json" \
      --optimized_flat "${OPT_FLAT[0]}" \
      --adapters "$ORCA_ARTIFACTS_DIR/puzzle_adapters.py" \
      --manifest "$ORCA_ARTIFACTS_DIR/manifest.yaml" \
      --latency_unit "$UNIT" \
      --latency_script_path "$LATENCY_SCRIPT" \
      --latency_reduction_target "$REDUCTION" \
      --output_dir "$ORCA_ARTIFACTS_DIR" || { echo "GATE_FAILED rc=$?" >&2; }
  fi
fi

# 终态判定 + 结构化报告（唯一 stdout 产物）
python3 "$ORCA_AGENT_RESOURCES/scripts/emit_report.py"
