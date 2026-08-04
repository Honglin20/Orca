# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前：无活跃任务（2026-08-04 收尾）

最近入库：
- `76073ef` fix(web): 看板 KPI 运行计数+过滤补 live-pending（test-agent 实测发现2闭环）+ `b6d5034` SPEC v1.2/blocked draft
- `ca5c07a` feat(web): 看板卡片网格重设计（横向列→KPI概览带+section+卡片网格，SPEC v1.1，详见 CHANGELOG）
- `99db8aa` fix(kd-nas): gpu_probe device-only + flatten artifacts 根 + 删 baseline bar
  — A gpu_probe device-only + B flatten 目录/baseline-bar，两轮 code-review 闭环（🔴0 / 🟡5 / 🟢3）。
  详见 `docs/releases/2026-08-04-kd-nas-{gpu-probe-teacher-cache-optional,flatten-artifacts-dir-and-drop-baseline-bar}.md`
- `da0f724` refactor(kd-train-script): 占位符模板 → 强制特化（前序，已入库； fidelity_check.py 同 commit）

工作区遗留（**其他任务**，本 session 未碰）：`tests/e2e_phase13/_artifacts/`、多个 untracked
plans/specs（in-session-failure-sentinel / orca-home-directory-layout 等）。
**web 看板重设计已入库**（`ca5c07a` + `76073ef` KPI fix + `b6d5034` SPEC v1.2/draft）；blocked 死 UI
作为架构裂缝 follow-up（见 `docs/specs/run-blocked-status-design-draft.md`，方案 A 后端 fold 待立项）。

## 待办
- 无 kd-nas 活跃任务。
