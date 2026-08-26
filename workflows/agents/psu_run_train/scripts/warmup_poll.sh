#!/bin/bash
# warmup_poll.sh —— warmup 单轮轮询（agent 重复调用直到 stdout 出现 WARMUP_OK / WARMUP_FAIL；
# 每次调用含 sleep 240，撞不到 bash 工具超时上限）。
# 判据（互斥）：
#   WARMUP_FAIL reason=process-exit rc=<RC>   进程已退出（崩或正常结束）
#   WARMUP_FAIL reason=metric-diverged        progress.jsonl 任一指标 NaN/Infinity（用户指标不可
#                                             预测——loss/reward/gain 皆可能，查结构化全量；文件
#                                             缺失回落 prose 行结构化匹配）
#   WARMUP_FAIL reason=progress-contract      progress.jsonl 缺/空/某行不符 {step:int,metrics:dict<number>}
#                                             契约（telemetry≥2 时校验；漏写=生成代码 bug→HEAL-LOOP 修）
#   WARMUP_OK epoch_cnt>=2                    ≥2 个进度标记（epoch 或 step，可测每单位耗时）+ progress.jsonl 契约过
#   WARMUP_RUNNING epoch_cnt=<n>              还在跑但进度标记不足
# LOG_MTIME / LOG_SIZE 供 agent 在"无进度标记"时判 log 是否在增长（格式未契约化兜底）。
# 依赖：ORCA_ARTIFACTS_DIR（orca spawn / orca_env.sh 注入）+ bash/python3/GNU 或 BSD stat
# （双平台兼容 `stat -c` / `stat -f`）。进度行契约见
# psu_train_script references train_supernet_script_generation.md Logging 节。
set +e
cd "$ORCA_ARTIFACTS_DIR" || exit 1
PID="$(cat runs/train/.train_pid 2>/dev/null)"
LOG="$(ls -t runs/train/train.attempt*.log 2>/dev/null | head -1)"
[ -n "$LOG" ] || LOG="runs/train/train.attempt1.log"
echo "LOG=$LOG"
MTIME="$(stat -c %Y "$LOG" 2>/dev/null || stat -f %m "$LOG" 2>/dev/null || echo none)"
SIZE="$(stat -c %s "$LOG" 2>/dev/null || stat -f %z "$LOG" 2>/dev/null || echo none)"
echo "LOG_MTIME=$MTIME LOG_SIZE=$SIZE"

if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
  RC="$(cat runs/train/.train_rc 2>/dev/null || echo unknown)"
  echo "WARMUP_FAIL reason=process-exit rc=$RC"
  tail -30 "$LOG" 2>/dev/null
  exit 0
fi

sleep 240   # 4 min；禁改更大（撞 bash 工具超时）
# 发散检测：progress.jsonl 任一指标 NaN/Infinity（用户指标不可预测，查结构化全量而非字面 loss）；
# 文件缺失（训练未写首点）回落到 prose 行结构化匹配。
PROGRESS="runs/train/progress.jsonl"
if [ -f "$PROGRESS" ]; then
  DIVERGED="$(grep -E ':[[:space:]]*-?(NaN|Infinity)' "$PROGRESS" 2>/dev/null | tail -1)"
else
  DIVERGED="$(grep -iE '(epoch|step)[[:space:]]+[0-9]+/[0-9]+[[:space:]]+[^[:space:]]+[[:space:]]+[-+]?(nan|inf)' "$LOG" 2>/dev/null | tail -1)"
fi
if [ -n "$DIVERGED" ]; then
  echo "WARMUP_FAIL reason=metric-diverged"
  tail -8 "$LOG" 2>/dev/null
  exit 0
fi
EPOCH_LINES="$(grep -iE '(epoch|step)[^0-9]*[0-9]+' "$LOG" 2>/dev/null | tail -5)"
METRIC_LINE="$(grep -iE '(epoch|step)[[:space:]]+[0-9]+/[0-9]+[[:space:]]+[^[:space:]]+[[:space:]]+[-+]?[0-9]' "$LOG" 2>/dev/null | tail -1)"
echo "---EPOCH_MARKERS---"
echo "$EPOCH_LINES"
echo "---LAST_METRIC---"
echo "$METRIC_LINE"
echo "---TAIL---"
tail -8 "$LOG" 2>/dev/null
MTIME2="$(stat -c %Y "$LOG" 2>/dev/null || stat -f %m "$LOG" 2>/dev/null || echo none)"
SIZE2="$(stat -c %s "$LOG" 2>/dev/null || stat -f %z "$LOG" 2>/dev/null || echo none)"
echo "LOG_MTIME_AFTER=$MTIME2 LOG_SIZE_AFTER=$SIZE2"

if [ -n "$EPOCH_LINES" ]; then
  # 计数：提取数字 → 去前导零归并（ckpt 快照名 supernet_epoch_0005.pth 的 0005 归并为 5，
  # 与真实进度行 epoch 5 去重一致）→ 数字排序去重
  EPOCH_CNT="$(printf '%s\n' "$EPOCH_LINES" | grep -oiE '(epoch|step)[^0-9]*[0-9]+' | grep -oiE '[0-9]+' | sed 's/^0*//' | grep -v '^$' | sort -un | wc -l)"
  if [ "$EPOCH_CNT" -ge 2 ]; then
    # 收紧：telemetry≥2 时 progress.jsonl 必按契约有合法行（§3(b) 每 unit 写）。
    # 漏写/格式错 = 生成代码 bug → WARMUP_FAIL → HEAL-LOOP 修 train/retrain 脚本。
    if python3 "$ORCA_AGENT_RESOURCES/scripts/check_progress_contract.py" --progress "$PROGRESS"; then
      echo "WARMUP_OK epoch_cnt=$EPOCH_CNT"
    else
      echo "WARMUP_FAIL reason=progress-contract"
      tail -8 "$LOG" 2>/dev/null
    fi
  else
    echo "WARMUP_RUNNING epoch_cnt=$EPOCH_CNT"
  fi
else
  echo "WARMUP_RUNNING epoch_cnt=0"
fi
