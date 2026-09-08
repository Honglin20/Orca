#!/bin/bash
# status.sh —— ns_run_train 状态确定性判定（gate + 完成 + 存活 三合一，agent.md Step 1）。
# 输出一行判定（互斥）：
#   GATE_SKIP         run_train_supernet.sh 不存在（viability self-gate → skipped）
#   TRAIN_COMPLETE    训练真正完成：rc==0 且进程已退出 且 ckpt 存在且 torch.load 可读
#   TRAIN_ALIVE       训练进程活着（→ 健康检查）
#   TRAIN_INCOMPLETE  未完成（ckpt 可能是中断残留 → 续训/启动）
# 依赖：ORCA_ARTIFACTS_DIR（orca spawn / orca_env.sh 注入）。
set +e
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }

if [ ! -f run_train_supernet.sh ]; then
  printf "run_train_supernet.sh absent; training not viable: run_train_supernet.sh not generated." \
    > .ns_run_train_assessment.txt
  echo "GATE_SKIP"
  exit 0
fi

# 完成判定：rc==0 且 进程已退出 且 ckpt 有效。rc 由 launch.sh 的 wrapper 在脚本退出后写。
# 必须排除在跑进程——前次 attempt 可能留 rc=0（续训场景），训练中途 ckpt 变有效会被
# stale rc 误判提前完成（孤儿进程会覆盖 ckpt）。
RC="$(cat runs/train/.train_rc 2>/dev/null || echo missing)"
PID="$(cat runs/train/.train_pid 2>/dev/null || echo none)"
ALIVE=""
if [ -n "$PID" ] && [ "$PID" != "none" ] && kill -0 "$PID" 2>/dev/null; then ALIVE=1; fi

CKPT="$(python3 - "$ORCA_ARTIFACTS_DIR" <<'PY'
import os, re, sys
ad = sys.argv[1]
ckpt = "runs/train/supernet_best.pth"
cfg = os.path.join(ad, "search_config.yaml")
try:
    for ln in open(cfg, encoding="utf-8", errors="replace"):
        m = re.search(r"supernet_ckpt_path:\s*\"?([^\s\"#]+)\"?", ln)
        if m:
            ckpt = m.group(1)
            break
except FileNotFoundError:
    pass
print(ckpt if os.path.isabs(ckpt) else os.path.join(ad, ckpt))
PY
)"

if [ -z "$ALIVE" ] && [ "$RC" = "0" ] && [ -s "$CKPT" ] 2>/dev/null \
   && python3 -c "
import sys, torch
sd = torch.load(sys.argv[1], map_location='cpu', weights_only=False)
state = sd.get('state_dict', sd) if isinstance(sd, dict) else sd
assert state, 'empty state_dict'
print('CKPT_VALID')
" "$CKPT" 2>/dev/null | grep -q CKPT_VALID; then
  # 落 ckpt 路径 marker（纯路径，单行）——emit_result.py 优先读此 marker，与 status.sh 共用同一解析。
  printf '%s' "$CKPT" > .ns_run_train_ckpt_resolved.txt
  printf 'training completed (rc=0, process exited, ckpt valid): %s' "$CKPT" > .ns_run_train_assessment.txt
  echo "TRAIN_COMPLETE ckpt=$CKPT"
elif [ -n "$ALIVE" ]; then
  echo "TRAIN_ALIVE pid=$PID"
else
  echo "TRAIN_INCOMPLETE rc=$RC ckpt=$CKPT"
fi
