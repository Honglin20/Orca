# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## ⚠ 并行协调协议（两 loop 必读，2026-08-27 生效）

**workflows/ 目录写权归「目录隔离改造」loop 直到其批 H 完成**（预计顺序：批 B kd 删除 → C 加载层 → D 大迁移 → E install → F skill → G web → H 验证）。
**prof-opt v5 loop**：spec 评审环（只读 workflows/）可并行；**Phase 3 实现必须等 `workflows/prof-opt/` 目录已存在**（批 D 落地标志），届时 SPEC 内所有 `workflows/prof-opt.yaml` / `workflows/agents/_po_scripts` / `workflows/subagents/prof-opt` 路径按新布局（`workflows/prof-opt/workflow.yaml` / `workflows/prof-opt/agents/_po_scripts` / `workflows/prof-opt/subagents`）换算——plan 级调整，fail loud 上报。迁移 loop 批 D 前须 `git log --oneline -3` 核对无 v5 写入 workflows/ 的提交；有 → fail loud 停。

---

## Workflows per-workflow 目录隔离改造 —— SDD loop 进行中

**任务**：workflows/ 平铺 → per-wf 自包含目录（`<wf>/workflow.yaml + agents/ + subagents/ + knowledge_base/ + scripts/`）；双形态加载兼容；kd-nas 净删除；create-workflow skill 同步；web 显示 sub-agents + 脚本资产。
**SPEC**：`C:\Users\mozzie\.claude\plans\crystalline-chasing-dewdrop.md`（PASS）｜**计划**：`docs/plans/2026-08-27-workflow-per-dir-layout-plan.md`（READY）
**Phase**：Phase 3 实现——**批 A 完成**（commit `a379375`，v2 固化 + 基线 `.layout_baseline_list.txt` 源态口径 15 wf 含 kd-nas；注意 `orca list` CLI 混扫安装态多 po-probe 尸体，diff 一律用源态口径）；**批 B 完成**（kd-nas 净删除：58 文件删 + 混合测试改判 test_struct_kd_p7/test_receiver_variants 保留 kd 外用例 + e2e_redesign 契约 kd 条目清零 + 注释死例换 _po_scripts；源态 catalog 14 wf；deferred 待批 D/H 裁决：knowledge_base kd 专属卡死链 / workflows 内 KD-NAS 大写死例文本 / examples/kd-nas-demo README 死链）
**基线 diff 口径**：源态直扫（load_workflow 逐 yaml），勿直接 diff `orca list` 输出
**无人值守**：计划外问题 fail loud 停下写 `LAYOUT_MIGRATION_REPORT.md`；pytest/tars 走 WSL .venv；不 push

---

## Prof-opt v5 —— SDD loop 进行中（并行，见顶部协调协议）

**SPEC**：`docs/specs/prof-opt-v5-spec.md`（依据已终审 design-draft）｜**Phase**：Phase 1 spec 评审环
**实现门槛**：见协调协议——等 `workflows/prof-opt/` 存在后按新布局换算路径再开工

---

## 已完成（勿重复）

- create-workflow skill v2 固化（commit `a379375`，2026-08-27）
- prof-opt v4 重构完成（2026-08-26，13 commits，CHANGELOG [2026-08-26]）

## 工作区遗留（非本任务，不动）
- `.e2e_po/`、`.e2e_spe2e/` scratch；`docs/specs/prof-opt-v5-spec.md`（v5 loop 资产，其自行处置）；`.layout_baseline_list.txt`（本任务基线，不提交）
