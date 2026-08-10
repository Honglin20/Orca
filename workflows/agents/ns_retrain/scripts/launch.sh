#!/bin/bash
# launch.sh —— 尝试预算 + detach（一次短调用，秒级返回，禁 wait/sleep）。
# 尝试预算（跨唤醒收敛）：N=1..3 记在 .retrain_attempt。所有"启动/重跑"共享预算——
# warmup 失败自愈、假死重启、中断续训、rc==0 无 ckpt 重跑都会 N++。
# N>3 仍无法跑到完成 → 不再 detach，输出 ATTEMPT_BUDGET_EXHAUSTED（agent 走 failed）。
# 依赖：ORCA_ARTIFACTS_DIR（orca spawn / orca_env.sh 注入）+ setsid（Linux 训练机既有假设）。
set -e
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }
mkdir -p runs/retrain

# N = 上次 attempt + 1（首启无记录 → N=1）。预算耗尽 → 不再 detach。
PREV="$(cat runs/retrain/.retrain_attempt 2>/dev/null || echo 0)"
N=$((PREV + 1))
if [ "$N" -gt 3 ]; then
  echo "ATTEMPT_BUDGET_EXHAUSTED n=$N"
  exit 0
fi
echo "$N" > runs/retrain/.retrain_attempt

# 清本 run 审计痕迹（续训**不**删 ckpt，只清 marker；`.ns_retrain_generated.txt` 保留——
# 生成产物跨 attempt 复用，不重新生成；`.ns_retrain_fidelity.flag` **不清**——3b 首启 fidelity
# 在 launch 前已写，清掉会让成功路径 fidelity_retriggered 失真；flag 是覆盖写（`printf "true" >`），
# 无 stale，语义 = "对当前脚本已跑过 fidelity"）。
rm -f .ns_retrain_healed.txt .ns_retrain_assessment.txt .ns_retrain_ckpt_resolved.txt runs/retrain/progress.jsonl

# 清前次残留 wrapper（防御，防 attempt 叠加并发训练——真实事故：两层重试下多个 wrapper
# 并存 → 端口撞 EADDRINUSE + ckpt 互踩）。正常流程只在 RETRAIN_INCOMPLETE（无活进程）后
# 才被调；若 .retrain_pid 记录的进程还活着 = 状态漂移（组杀未清干净），整组杀防第二份训练
# 起来。/proc cmdline 校验防 PID 复用误杀（Linux 训练机为既有假设）。
PREV_PID="$(cat runs/retrain/.retrain_pid 2>/dev/null || echo '')"
if [ -n "$PREV_PID" ] && kill -0 "$PREV_PID" 2>/dev/null \
   && grep -q "run_retrain" "/proc/$PREV_PID/cmdline" 2>/dev/null; then
  kill -- -"$PREV_PID" 2>/dev/null || kill "$PREV_PID" 2>/dev/null || true
  for _ in 1 2 3 4 5; do kill -0 "$PREV_PID" 2>/dev/null || break; sleep 1; done
  echo "KILLED_STALE_PID=$PREV_PID" >&2
fi

# detach：setsid 起新会话（wrapper 成进程组首领）——后续 kill -- -PID 能整组杀（含训练 python），
# 防"只杀 wrapper、孤儿训练进程残留 → 下轮重复 detach"（铁律 6 盲区）。
# wrapper 末尾 `echo $? > .retrain_rc` 捕获脚本退出码——status.sh 完成判定的权威信号。
# 训练前先启动 progress_watcher（同进程组：组杀一并清；done-marker 驱动退出；fail-soft
# 绝不碰训练 rc）——tail progress.jsonl 边重训边推实时曲线到前端（每指标一张独立图）。
setsid nohup bash -c 'python3 "$ORCA_AGENT_RESOURCES/scripts/progress_watcher.py" --progress "runs/retrain/progress.jsonl" --done-marker "runs/retrain/.retrain_rc" --label "nas-supernet/retrain" --title "Retrain Metrics (attempt '"$N"')" >/dev/null 2>&1 & bash run_retrain.sh > "runs/retrain/retrain.attempt'"$N"'.log" 2>&1; echo $? > runs/retrain/.retrain_rc' >/dev/null 2>&1 &
echo $! > runs/retrain/.retrain_pid
echo "DETACHED pid=$(cat runs/retrain/.retrain_pid) attempt=$N"
