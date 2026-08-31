#!/bin/bash
# kill_train_group.sh <pid> —— 带 run 归属门的整组杀（防跨 run 误杀）。
#
# launch.sh 残留清理 + agent Step 2 假死 / self-heal 共用的**唯一** BLD 进程 kill 入口。
# 背景：ORCA_ARTIFACTS_DIR 是 engine project-scoped（同项目并发 run 共享 `artifacts/<wf>/`），
# pid 文件 / status.sh 报的 pid 可能是**另一 run** 的 BLD wrapper —— 名字级 cmdline 匹配
# 不足以区分归属。本脚本按 /proc/<pid>/environ 的 ORCA_RUN_ID 判定（wrapper 继承 agent
# 的 env，新老 wrapper 通用）：
#
#   - pid 无效 / 已死 / cmdline 非本 workflow BLD wrapper（PID 复用防误杀）→ exit 0（无需杀）
#   - environ ORCA_RUN_ID == 当前（或 ORCA_RUN_ID 缺失的 legacy 场景）→ 整组杀 + exit 0
#   - 别的 run 的 BLD → **不杀**，stdout `FOREIGN_RUN_ALIVE` + exit 1（调用方按 FOREIGN 语义处理）
#
# 退出码契约：0 = 已处理（已杀 / 无需杀）；1 = FOREIGN（另一 run BLD 中，未杀）。
# 依赖：ORCA_RUN_ID（orca_env.sh 注入）+ /proc（Linux 训练机为既有假设）。
set -u

PID="${1:-}"
if [ -z "$PID" ]; then
  exit 0
fi
kill -0 "$PID" 2>/dev/null || exit 0

# cmdline 校验防 PID 复用误杀：必须含本 workflow 的 BLD 脚本名（wrapper 是
# `setsid nohup bash -c '…bash run_bld.sh…'`，脚本文本留在 cmdline 里）。
CMDLINE="$(tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null || true)"
case "$CMDLINE" in
  *"run_bld"*) ;;
  *) exit 0 ;;
esac

# run 归属：environ ORCA_RUN_ID 与当前比对。
PREV_RUN="$(tr '\0' '\n' < "/proc/$PID/environ" 2>/dev/null \
  | grep -x "ORCA_RUN_ID=.*" | head -1 || true)"
if [ -n "${ORCA_RUN_ID:-}" ] && [ -n "$PREV_RUN" ] \
   && [ "$PREV_RUN" != "ORCA_RUN_ID=$ORCA_RUN_ID" ]; then
  echo "FOREIGN_RUN_ALIVE pid=$PID（另一 run 的 BLD 在跑；跨 run 杀进程已禁用）"
  exit 1
fi

# 本 run 残留（或 ORCA_RUN_ID 缺失的 legacy 场景）→ 整组杀。launch.sh 用 setsid 起进程组，
# `kill -- -PID` 整组杀——含 BLD python，防"只杀 wrapper、孤儿 BLD 进程残留 → 下轮重复 detach"。
kill -- -"$PID" 2>/dev/null || kill "$PID" 2>/dev/null || true
for _ in 1 2 3 4 5; do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
echo "KILLED_STALE_PID=$PID" >&2
exit 0
