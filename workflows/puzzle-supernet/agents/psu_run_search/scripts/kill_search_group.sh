#!/usr/bin/env bash
# kill_search_group.sh —— psu_run_search 假死 / HEAL 整组杀（按 .search_pid 里的 PGID），带 run 归属门。
# 负 PGID = 杀整个进程组（wrapper + 脚本 + python 搜索进程一并死）——绝不只杀 wrapper
#（孤儿 python 搜索进程会残留占资源）。组隔离：全新唯一 PGID，不波及 chart daemon。
# 活性检查语义（poll_search.sh 的 `kill -0 $PID`）不变：首领 PID == PGID，首领活着 ⇔ 搜索在跑。
# 归属门（对齐 psu_run_train/kill_train_group.sh）：ORCA_ARTIFACTS_DIR 是 project-scoped，
# 同项目并发 run 共享 artifacts 目录时 .search_pid 里的组可能是**另一 run** 的搜索进程——
# 按 /proc/<pgid>/environ 的 ORCA_RUN_ID 判定：
#   - 无 pid / 组已死 → KILL_SKIP + exit 0
#   - cmdline 非本 workflow 搜索 wrapper（PID 复用防误杀）→ exit 0
#   - environ ORCA_RUN_ID ≠ 当前 → FOREIGN_RUN_ALIVE + exit 1（跨 run 杀进程禁用）
# 退出码契约：0 = 已处理（已杀 / 无需杀）；1 = FOREIGN（另一 run 搜索中，未杀）。
# 依赖：ORCA_ARTIFACTS_DIR + ORCA_RUN_ID（orca_env.sh 注入）+ /proc。
set -u

cd "$ORCA_ARTIFACTS_DIR" || exit 1
PGID="$(cat runs/search/.search_pid 2>/dev/null || echo '')"
if [ -z "$PGID" ]; then
  echo "KILL_SKIP no .search_pid (nothing recorded to kill)"
  exit 0
fi
if ! kill -0 "$PGID" 2>/dev/null; then
  echo "KILL_SKIP pgid=$PGID already dead"
  exit 0
fi

# cmdline 校验防 PID 复用误杀：首领进程必须是本 workflow 的搜索 wrapper。
CMDLINE="$(tr '\0' ' ' < "/proc/$PGID/cmdline" 2>/dev/null || true)"
case "$CMDLINE" in
  *"run_search_supernet"*) ;;
  *) exit 0 ;;
esac

# run 归属：environ ORCA_RUN_ID 与当前比对。
PREV_RUN="$(tr '\0' '\n' < "/proc/$PGID/environ" 2>/dev/null \
  | grep -x "ORCA_RUN_ID=.*" | head -1 || true)"
if [ -n "${ORCA_RUN_ID:-}" ] && [ -n "$PREV_RUN" ] \
   && [ "$PREV_RUN" != "ORCA_RUN_ID=$ORCA_RUN_ID" ]; then
  echo "FOREIGN_RUN_ALIVE pgid=$PGID（另一 run 的搜索在跑；跨 run 杀进程已禁用）"
  exit 1
fi

kill -- -"$PGID" 2>/dev/null || true
echo "KILLED_GROUP pgid=$PGID"
