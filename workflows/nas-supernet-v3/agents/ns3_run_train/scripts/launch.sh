#!/bin/bash
# launch.sh —— detach（一次短调用，秒级返回，禁 wait/sleep）。
# attempt 记在 .train_attempt（仅 log 命名计数 + 审计，**无上限**——不阻断 detach）。
# 依赖：ORCA_ARTIFACTS_DIR（orca spawn / orca_env.sh 注入）。
set -e
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }
mkdir -p runs/train

# ── 残留进程归属判定（防跨 run 误杀）─────────────────────────
# 同项目并发 run 共享 artifacts 目录（engine project-scoped `<project_root>/artifacts/<wf>`）
# → `.train_pid` 里可能是**别的 run** 的训练 wrapper。统一走带 run 归属门的
# kill_train_group.sh（与 agent Step 2 假死 / self-heal 同源）：本 run 残留才整组杀；
# 别的 run 的活训练 → FOREIGN_RUN_ALIVE + exit 1 abort（不 kill；abort 在 attempt 计数
# **之前**）。cmdline/PID 复用防误杀在脚本内。
PREV_PID="$(cat runs/train/.train_pid 2>/dev/null || echo '')"
if [ -n "$PREV_PID" ] \
   && ! bash "$ORCA_AGENT_RESOURCES/scripts/kill_train_group.sh" "$PREV_PID"; then
  exit 1
fi

# N = 上次 attempt + 1（首启无记录 → N=1）。无上限——仅 log 命名 + 审计计数。
PREV="$(cat runs/train/.train_attempt 2>/dev/null || echo 0)"
N=$((PREV + 1))
echo "$N" > runs/train/.train_attempt

# 清本 run 审计痕迹（续训**不**删 ckpt，只清 marker；progress.jsonl 每 attempt 清零；
# .train_rc 也要清——resume 时 stale rc 残留会让 monitor 每 60s 误判进程退出、旁路 cheap 活性）。
rm -f .ns_run_train_healed.txt .ns_run_train_fidelity.flag .ns_run_train_assessment.txt .ns_run_train_ckpt_resolved.txt runs/train/progress.jsonl runs/train/.train_rc

# detach：setsid 起新会话（wrapper 成进程组首领）——后续 kill -- -PID 能整组杀（含训练 python），
# 防"只杀 wrapper、孤儿训练进程残留 → 下轮重复 detach"（铁律 6 盲区）。
# wrapper 末尾 `echo $? > .train_rc` 捕获脚本退出码——status.sh 完成判定的权威信号。
# 训练前先启动 progress_watcher（同进程组：组杀一并清；done-marker 驱动退出；fail-soft
# 绝不碰训练 rc）——tail progress.jsonl 边训练边推实时曲线到前端（每指标一张独立图）。
setsid nohup bash -c 'python3 "$ORCA_AGENT_RESOURCES/scripts/progress_watcher.py" --progress "runs/train/progress.jsonl" --done-marker "runs/train/.train_rc" --label "nas-supernet-v3/train" --title "Training Metrics (attempt '"$N"')" >/dev/null 2>&1 & bash run_train_supernet.sh > "runs/train/train.attempt'"$N"'.log" 2>&1; echo $? > runs/train/.train_rc' >/dev/null 2>&1 &
echo $! > runs/train/.train_pid
echo "DETACHED pid=$(cat runs/train/.train_pid) attempt=$N"
