# Release Note — 2026-08-10：launch.sh 防跨 run 误杀（残留清理只杀本 run）

## 问题

nas-supernet 训练进程清理按 pid 文件（`.train_pid` / `.retrain_pid`）+ `/proc/<pid>/cmdline`
**名字级**匹配判断归属。而 `ORCA_ARTIFACTS_DIR` 是 engine project-scoped
（`<project_root>/artifacts/<wf>`）——**同项目并发 run 共享同一 artifacts 目录**，
pid 文件 / status.sh 报的 pid 可能是**另一 run** 的训练 wrapper（cmdline 同样含
`run_train_supernet` / `run_retrain`）→ 本 run 的清理会 `kill -- -PID` **整组杀掉隔壁 run
的活训练**（含训练 python）。kill 点共 3 处：launch.sh 残留清理、agent Step 2 假死 kill、
agent self-heal kill（Step 2 的 `$PID` 来自 status.sh 的 liveness，无 run 校验 → 误认
ALIVE → 健康检查读共享 log 误判 stuck → 杀隔壁 run 的健康训练，是跨 run 误杀主路径）。

## 修复（新增带 run 归属门的唯一 kill 入口，3 个 kill 点全收敛）

- 新增 `scripts/kill_train_group.sh`（ns_run_train / ns_retrain 各一份镜像）：`kill_train_group.sh <pid>`
  - pid 无效 / 已死 / cmdline 非本 workflow 训练 wrapper（PID 复用防误杀）→ exit 0（无需杀）；
  - 读 `/proc/<pid>/environ` 的 `ORCA_RUN_ID`（wrapper 继承 agent 的 env——**新老 wrapper 通用**）
    与当前比对：**本 run** → 整组杀（`kill -- -PID`，含训练 python）+ exit 0；
  - **别的 run** → **不杀**，stdout `FOREIGN_RUN_ALIVE` + exit 1；
  - `ORCA_RUN_ID` 缺失（legacy env）→ 保持旧行为（kill），零回归。
- `launch.sh` × 2：残留清理改调 helper——FOREIGN → abort（判定在尝试预算计数**之前**，
  不烧本 run 预算，下个 CRON 周期再试）。
- agent.md × 2：Step 2 假死判定 + self-heal 的裸 `kill -- -PID` 全部改调 helper——
  FOREIGN_RUN_ALIVE → 不杀、不判假死、更新 MD + 重注册 CRON + 下周期再试。

## 行为变化（review 记录）

- **预算耗尽路径顺序**：原代码先查预算（N>3 exit 0）再清残留 → 预算耗尽时残留 wrapper
  不杀；现在残留清理在预算前 → 预算耗尽前也会先清本 run 孤儿再报 ATTEMPT_BUDGET_EXHAUSTED
  （更安全：清孤儿再 fail，不留叠加事故）。
- 无 `ORCA_AGENT_RESOURCES` env 时 launch.sh 提前 abort（helper 不可达）——生产路径恒注入，
  可接受。

## 验证

- `bash -n` × 4（launch × 2 + helper × 2）通过；`tars validate workflows/nas-supernet.yaml` ✓ 0 error。
- 4 场景 smoke（临时 artifacts 目录 + 假 wrapper，train/retrain 双份）全过：
  ① FOREIGN run → abort 不杀不烧预算；② 本 run stale → 整组杀 + detach；③ 无关进程（PID 复用）
  → 不误杀；④ legacy（env 缺失）→ 旧行为 kill。
- `tests/workflows` 532 passed / 3 skipped（无回归）。

## 遗留

- 共享目录的 run 间 **liveness 判定**仍互相可见（status.sh 读同一 pid 文件会误认他人训练
  为自己的 → 误报 ALIVE → agent 走 Step 2 但 kill 已被归属门拦住，安全）——彻底隔离需
  run 级 artifacts 或引擎级并发守卫，另行决策。
- kd-nas `train-teacher`（旧 deferred-training-cron 模型，独立 artifacts 目录）的裸 kill
  未迁移——不同 workflow 目录隔离，无跨 run 共享，随该节点迁移时一并处理。
