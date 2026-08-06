#!/bin/bash
# health.sh —— 唤醒健康检查（训练进程活着时）：抓进度标记（epoch/step）/ loss / log 尾部。
# 输出为纯信息（agent 判健康/卡死）；无 WARMUP_* 语义。
# LOG_MTIME / LOG_SIZE 供 agent 在"无进度标记"时判 log 是否在增长（格式未契约化兜底）。
# 依赖：ORCA_ARTIFACTS_DIR（orca spawn / orca_env.sh 注入）+ bash/python3/GNU 或 BSD stat。
# 进度行契约见 agent.md Step 3a（retrain.py 生成要求）：每 progress unit 必打
# `epoch <cur>/<total> loss <v>` / `step <cur>/<total> loss <v>`。
set +e
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }
LOG="$(ls -t runs/retrain/retrain.attempt*.log 2>/dev/null | head -1)"
[ -n "$LOG" ] || LOG="runs/retrain/retrain.attempt1.log"
echo "LOG=$LOG"
MTIME="$(stat -c %Y "$LOG" 2>/dev/null || stat -f %m "$LOG" 2>/dev/null || echo none)"
SIZE="$(stat -c %s "$LOG" 2>/dev/null || stat -f %z "$LOG" 2>/dev/null || echo none)"
echo "LOG_MTIME=$MTIME LOG_SIZE=$SIZE"
echo "---EPOCH_MARKERS---"
grep -iE '(epoch|step)[^0-9]*[0-9]+' "$LOG" 2>/dev/null | tail -5
echo "---LAST_LOSS---"
grep -iE 'loss[^0-9]*[-+]?[0-9]' "$LOG" 2>/dev/null | tail -1
echo "---LOSS_DIVERGED---"
grep -iE 'loss[^0-9-]*(nan|inf)' "$LOG" 2>/dev/null | tail -2
echo "---TAIL---"
tail -8 "$LOG" 2>/dev/null
