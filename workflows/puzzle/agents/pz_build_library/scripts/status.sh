#!/bin/bash
# status.sh —— pz_build_library 状态确定性判定（gate + 完成 + 存活 三合一，agent.md Step 1）。
# 输出一行判定（互斥）：
#   GATE_SKIP         run_bld.sh 不存在（viability self-gate → skipped）
#   BLD_COMPLETE      BLD 真正完成：rc==0 且进程已退出 且 ckpt 存在且 torch.load 可读
#   BLD_ALIVE         BLD 进程活着（→ 健康检查）
#   BLD_INCOMPLETE    未完成（ckpt 可能是中断残留 → 续训/启动）
# 依赖：ORCA_ARTIFACTS_DIR（orca spawn / orca_env.sh 注入）。
set +e
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }

if [ ! -f run_bld.sh ]; then
  printf "run_bld.sh absent; bld not viable: run_bld.sh not generated." \
    > .pz_build_library_assessment.txt
  echo "GATE_SKIP"
  exit 0
fi

# 完成判定：rc==0 且 进程已退出 且 ckpt 有效。rc 由 launch.sh 的 wrapper 在脚本退出后写。
# 必须排除在跑进程——前次 attempt 可能留 rc=0（续训场景），训练中途 ckpt 变有效会被
# stale rc 误判提前完成（孤儿进程会覆盖 ckpt）。
RC="$(cat runs/bld/.bld_rc 2>/dev/null || echo missing)"
PID="$(cat runs/bld/.bld_pid 2>/dev/null || echo none)"
ALIVE=""
if [ -n "$PID" ] && [ "$PID" != "none" ] && kill -0 "$PID" 2>/dev/null; then ALIVE=1; fi

# ckpt 契约路径固定 = runs/bld/bld_complete.pt（BLD 无 search_config，不由 yaml 解析）。
CKPT="$ORCA_ARTIFACTS_DIR/runs/bld/bld_complete.pt"

if [ -z "$ALIVE" ] && [ "$RC" = "0" ] && [ -s "$CKPT" ] 2>/dev/null \
   && python3 -c "
import sys, torch
sd = torch.load(sys.argv[1], map_location='cpu', weights_only=False)
state = sd.get('state_dict', sd) if isinstance(sd, dict) else sd
assert state, 'empty state_dict'
print('CKPT_VALID')
" "$CKPT" 2>/dev/null | grep -q CKPT_VALID; then
  # 落 ckpt 路径 marker（纯路径，单行）——emit_result.py 优先读此 marker，与 status.sh 共用同一解析。
  printf '%s' "$CKPT" > .pz_build_library_ckpt_resolved.txt
  printf 'bld completed (rc=0, process exited, ckpt valid): %s' "$CKPT" > .pz_build_library_assessment.txt
  echo "BLD_COMPLETE ckpt=$CKPT"
elif [ -n "$ALIVE" ]; then
  echo "BLD_ALIVE pid=$PID"
else
  echo "BLD_INCOMPLETE rc=$RC ckpt=$CKPT"
fi
