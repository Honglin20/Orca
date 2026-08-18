#!/bin/bash
# detach_search.sh <attempt> —— psu_run_search Step 2a 搜索 detach（一次短调用，秒级返回，
# 禁 wait/sleep）。attempt 号由调用方传入（Step R 续跑分支 / 自愈 N++ 循环各算各的）。
# 依赖：ORCA_ARTIFACTS_DIR（orca spawn / orca_env.sh 注入）。
set -u
N="${1:?usage: detach_search.sh <attempt>}"

cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }
mkdir -p runs/search
rm -f runs/search/.search_pid runs/search/.search_rc

# setsid：搜索自成会话/进程组（PGID == 会话首领 PID，由首领自己写入 .search_pid）。
# 假死 HEAL 时 `kill -- -<pgid>` 整组杀（wrapper + 脚本 + python + GPU worker 全死）——
# 修复旧 `nohup ... &` + `kill $!` 只杀 wrapper、reparent 的 python 搜索进程残留占 GPU 的问题。
# 组隔离：全新唯一 PGID，不跨 run/项目，不波及 chart daemon。
# wrapper 末尾 `echo $? > .search_rc` 捕获脚本退出码——跨 shell 轮询读 rc 的权威信号
#（轮询是另一个 bash 子 shell，wait 跨 shell 无效）。
setsid bash -c 'echo "$$" > runs/search/.search_pid; bash run_search_supernet.sh > "runs/search/search.attempt'"$N"'.stdout.log" 2>&1; echo $? > runs/search/.search_rc' </dev/null >/dev/null 2>&1 &

# 等首领写入 PGID（setsid 若需 fork，$! 可能是瞬时父进程；只信 .search_pid）。
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -f runs/search/.search_pid ] && break; sleep 0.2; done
echo "DETACHED pgid=$(cat runs/search/.search_pid 2>/dev/null || echo '?') attempt=$N"
