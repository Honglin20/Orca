#!/bin/bash
# poll_search.sh <attempt> —— psu_run_search Step 2b 短轮询（单次调用 ≤5min，由 agent 重复
# 发起直到 DONE；每次调用是独立的短调用，单调用内无 while 循环）。
# stdout 契约（互斥）：
#   DONE rc=<0|非0|unknown> + attempt 日志尾 30 行 → 进程退出，agent 进 2c 成败判定
#   RUNNING + attempt 日志尾 8 行 → 还在跑，agent 再发一次本调用
# 跨 shell rc：轮询调用与 detach 是不同 bash 子 shell（wait 无效）→ rc 只读 .search_rc。
# 依赖：ORCA_ARTIFACTS_DIR（orca spawn / orca_env.sh 注入）。
set -u
N="${1:?usage: poll_search.sh <attempt>}"

cd "$ORCA_ARTIFACTS_DIR" || exit 1
PID="$(cat runs/search/.search_pid 2>/dev/null)"
if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
  echo "DONE rc=$(cat runs/search/.search_rc 2>/dev/null || echo unknown)"
  tail -30 "runs/search/search.attempt${N}.stdout.log" 2>/dev/null
else
  sleep 240   # 4min：留足余量，不触 bash 工具单调用 ~10min 上限；不要再加大
  echo "RUNNING"
  tail -8 "runs/search/search.attempt${N}.stdout.log" 2>/dev/null
fi
