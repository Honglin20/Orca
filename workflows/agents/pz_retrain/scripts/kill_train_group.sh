#!/bin/bash
# kill_train_group.sh <pid> —— 带 run 归属门的整组杀（防跨 run 误杀）。
#
# launch.sh 残留清理 + agent Step 2 假死 / self-heal 共用的**唯一** GKD 进程 kill 入口。
# 退出码契约：0 = 已处理（已杀 / 无需杀）；1 = FOREIGN（另一 run GKD 中，未杀）。
# 依赖：ORCA_RUN_ID（orca_env.sh 注入）+ /proc（Linux 训练机为既有假设）。
set -u

PID="${1:-}"
if [ -z "$PID" ]; then
  exit 0
fi
kill -0 "$PID" 2>/dev/null || exit 0

# cmdline 校验防 PID 复用误杀：必须含本 workflow 的 retrain 脚本名（wrapper 是
# `setsid nohup bash -c '…bash run_retrain.sh…'`，脚本文本留在 cmdline 里）。
CMDLINE="$(tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null || true)"
case "$CMDLINE" in
  *"run_retrain"*) ;;
  *) exit 0 ;;
esac

PREV_RUN="$(tr '\0' '\n' < "/proc/$PID/environ" 2>/dev/null \
  | grep -x "ORCA_RUN_ID=.*" | head -1 || true)"
if [ -n "${ORCA_RUN_ID:-}" ] && [ -n "$PREV_RUN" ] \
   && [ "$PREV_RUN" != "ORCA_RUN_ID=$ORCA_RUN_ID" ]; then
  echo "FOREIGN_RUN_ALIVE pid=$PID（另一 run 的 GKD 在跑；跨 run 杀进程已禁用）"
  exit 1
fi

kill -- -"$PID" 2>/dev/null || kill "$PID" 2>/dev/null || true
for _ in 1 2 3 4 5; do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
echo "KILLED_STALE_PID=$PID" >&2
exit 0
