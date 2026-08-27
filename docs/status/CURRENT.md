# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## Prof-opt v5 —— SDD loop 进行中（2026-08-27 启动，与下述迁移任务并行）

**任务**：时延先行顺序门控重设计（D-V5-1~8：inputs 14→8 / 顺序门控 / 轮帽默认100 无早退 / origin 双锚恒定 / round_state 单一来源 / 精度规则沉淀）
**SPEC**：`docs/specs/prof-opt-v5-spec.md`（依据用户已终审的 `docs/specs/prof-opt-v5-design-draft.md`）
**模式**：无人值守（用户 2026-08-27 授权「直接开始执行」）
**Phase**：Phase 1 spec 评审环（轮 0；全循环回退 0/2）
**协调检查点**：与目录迁移任务同一工作树并行——coder 开工前必 `git log` 核对布局：`workflows/prof-opt/` 已存在 → SPEC 路径按新布局换算（plan 级调整，fail loud 上报）
**环境**：pytest/tars 走 WSL .venv；不 push

---

## Workflows per-workflow 目录隔离改造 —— SDD loop 进行中（2026-08-27 启动）

**任务**：workflows/ 从平铺（根 yaml + 共享 agents/ + subagents/<wf>/ + 根 knowledge_base/）迁移为 per-wf 自包含目录（`<wf>/workflow.yaml + agents/ + subagents/ + knowledge_base/ + scripts/`）；catalog/subagents/KB/install 双形态兼容；kd-nas 净删除；create-workflow skill 同步 per-wf 产出；web 显示 sub-agents + 脚本资产。

**SPEC**（=用户已批准计划，8 项决策拍板）：`C:\Users\mozzie\.claude\plans\crystalline-chasing-dewdrop.md`
**模式**：无人值守（用户授权跳过全部用户 gate；计划外问题 fail loud 停下写 LAYOUT_MIGRATION_REPORT.md）
**Phase**：Phase 3 实现（批 A 进行中；spec PASS 2 轮 / plan READY 3 轮；计划 `docs/plans/2026-08-27-workflow-per-dir-layout-plan.md`；全循环回退 0/2）
**实施分批**（每批一 coder-agent，commit A..I）：0 提交v2+基线 → 1 kd删除 → 2 加载层 → 3 大迁移 → 4 install → 5 skill同步 → 6 web(先Plan agent设计) → 7 review+全链验证 → 8 收尾
**必读**：SPEC 计划文件 + `orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`（迁移零改 prompt 铁律）
**环境**：pytest/tars 走 WSL .venv；双层 shell 引号写临时 .sh；不 push

---

## 已完成（勿重复）

- prof-opt v4 重构完成（2026-08-26，13 commits，详见 CHANGELOG [2026-08-26] + `docs/releases/2026-08-26-prof-opt-v4-refactor.md`）
- create-workflow skill v2：用户 2026-08-26 确认已改完（工作区未提交改动即 v2 成果，步骤 0 提交）

## 工作区遗留（非本任务，不动）
- puzzle-universal 前任务 WIP（冻结）/ .e2e_po、.e2e_spe2e E2E scratch；详见 git status
