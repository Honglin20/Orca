#!/bin/bash
# monitor_until_done.sh —— 有界轮询块（~9min/调用），可续接。cheap 活性 + 发散检测。
# 性能要点：每 60s 只做 cheap 活性检查（kill -0 + rc 文件存在性），**不调 status.sh**——避免 torch.load
#   每 60s 与 GKD 抢 CPU/显存。仅当进程退出（rc 出现或 pid 死）才委托 status.sh 做 torch.load ckpt 完整判定。
# stdout（互斥；**任何分支/异常都输出一个 token**，C-loop 总有 default 兜底）：
#   *COMPLETE* ckpt=<path>   → agent: Step3.5 最终图→Step4 executed
#   *INCOMPLETE*              → agent: HEAL-LOOP（进程死）
#   *STUCK* <reason>          → agent: 复核 → HEAL-LOOP（NaN/error 或 log 停滞）
#   STILL_RUNNING <摘要>      → agent: 再发 / turn 到顶 C-end
# 依赖：ORCA_ARTIFACTS_DIR + ORCA_AGENT_RESOURCES（orca spawn / orca_env.sh 注入）。
set -u
cd "$ORCA_ARTIFACTS_DIR" || { echo "STILL_RUNNING artifacts-unreachable"; exit 0; }

DEADLINE=$((SECONDS + ${ORCA_MONITOR_BUDGET_S:-540}))
INTERVAL=60
STALL_POLLS="${ORCA_TRAIN_STALL_POLLS:-3}"
# ── 路径（pz_retrain 镜像）──
ATT="$(cat runs/retrain/.retrain_attempt 2>/dev/null || echo 1)"
LOG="runs/retrain/retrain.attempt${ATT}.log"
PIDF="runs/retrain/.retrain_pid"; RCF="runs/retrain/.retrain_rc"
LAST_SIZE=0; STALL=0

while [ "$SECONDS" -lt "$DEADLINE" ]; do
  PID="$(cat "$PIDF" 2>/dev/null || echo '')"
  ALIVE=0; { [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; } && ALIVE=1
  RC_EXISTS=0; { [ -f "$RCF" ]; } && RC_EXISTS=1

  if [ "$RC_EXISTS" = "1" ] || [ "$ALIVE" = "0" ]; then
    out="$(bash "$ORCA_AGENT_RESOURCES/scripts/status.sh" 2>/dev/null || echo '')"
    case "$out" in
      *COMPLETE*)   echo "$out"; exit 0;;
      *INCOMPLETE*) echo "$out"; exit 0;;
      *)            echo "STILL_RUNNING status-ambiguous"; exit 0;;
    esac
  fi
  CUR_SIZE="$(stat -c %s "$LOG" 2>/dev/null || stat -f %z "$LOG" 2>/dev/null || echo 0)"
  if tail -n 50 "$LOG" 2>/dev/null | grep -Eqi '(^|[[:space:]:=,(])(nan|inf|overflow|out of memory|cuda error|runtime[[:space:]]*error|traceback|exception|error[[:space:]]*:)([[:space:],).:]|$)'; then
    echo "RETRAIN_STUCK nan-or-error-in-log"; exit 0
  fi
  if [ "$CUR_SIZE" -le "$LAST_SIZE" ]; then
    STALL=$((STALL + 1))
    if [ "$STALL" -ge "$STALL_POLLS" ]; then echo "RETRAIN_STUCK log-stalled polls=$STALL"; exit 0; fi
  else
    STALL=0
  fi
  LAST_SIZE="$CUR_SIZE"
  sleep "$INTERVAL"
done
echo "STILL_RUNNING pid=$PID log=$LOG"
