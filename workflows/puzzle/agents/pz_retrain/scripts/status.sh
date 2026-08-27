#!/bin/bash
# status.sh —— pz_retrain 状态确定性判定（完成 + 存活 二合一，agent.md Step 1）。
# 输出一行判定（互斥）：
#   RETRAIN_COMPLETE    GKD 真正完成：rc==0 且进程已退出 且 final ckpt 存在且 torch.load 可读
#   RETRAIN_ALIVE       GKD 进程活着（→ 健康检查）
#   RETRAIN_INCOMPLETE  未完成（无活进程；脚本缺失=首启待生成，ckpt 残留=中断待续训）
# 依赖：ORCA_ARTIFACTS_DIR（orca spawn / orca_env.sh 注入）。
# ckpt 契约路径 = runs/retrain/final_model.pt（pz_retrain agent.md 生成 run_retrain.sh 时强制）。
set +e
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }

# 完成判定：rc==0 且 进程已退出 且 ckpt 有效。rc 由 launch.sh 的 wrapper 在脚本退出后写。
# 必须排除在跑进程——前次 attempt 可能留 rc=0（续训场景），训练中途 ckpt 变有效会被
# stale rc 误判提前完成（孤儿进程会覆盖 ckpt）。
RC="$(cat runs/retrain/.retrain_rc 2>/dev/null || echo missing)"
PID="$(cat runs/retrain/.retrain_pid 2>/dev/null || echo none)"
ALIVE=""
if [ -n "$PID" ] && [ "$PID" != "none" ] && kill -0 "$PID" 2>/dev/null; then ALIVE=1; fi

CKPT="runs/retrain/final_model.pt"
if [ -s .pz_retrain_ckpt_resolved.txt ]; then
  M="$(cat .pz_retrain_ckpt_resolved.txt | tr -d '\r\n ')"
  [ -n "$M" ] && CKPT="$M"
fi
# marker 可能留旧 run 相对路径（历史残留）；强制绝对路径（emit_result.py 的 exists() 以
# cwd 无关方式校验，相对路径会误判）。
case "$CKPT" in
  /*) ;;
  *) CKPT="$ORCA_ARTIFACTS_DIR/$CKPT" ;;
esac

if [ -z "$ALIVE" ] && [ "$RC" = "0" ] && [ -s "$CKPT" ] 2>/dev/null \
   && python3 -c "
import sys, torch
sd = torch.load(sys.argv[1], map_location='cpu', weights_only=False)
state = sd.get('state_dict', sd) if isinstance(sd, dict) else sd
assert state, 'empty state_dict'
print('CKPT_VALID')
" "$CKPT" 2>/dev/null | grep -q CKPT_VALID; then
  # 落 ckpt 路径 marker（纯路径，单行）——emit_result.py 优先读此 marker，与 status.sh 共用同一解析。
  printf '%s' "$CKPT" > .pz_retrain_ckpt_resolved.txt
  printf 'retrain completed (rc=0, process exited, ckpt valid): %s' "$CKPT" > .pz_retrain_assessment.txt
  echo "RETRAIN_COMPLETE ckpt=$CKPT"
elif [ -n "$ALIVE" ]; then
  echo "RETRAIN_ALIVE pid=$PID"
else
  echo "RETRAIN_INCOMPLETE rc=$RC ckpt=$CKPT"
fi
