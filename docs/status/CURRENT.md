# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前状态：phase-13 render_chart 接入完成（S1-S5 全过），无进行中任务

**phase-13 script-side render_chart 接入收官**：env 身份路由 + per-run Unix socket + 大数据三道关 + executor-agnostic（ClaudeExecutor + ScriptExecutor 双闭环）。
- **SPEC**：[`phase-13-render-chart.md`](../specs/phase-13-render-chart.md)（对抗审闭环 v1，16 处修订）｜**计划**：[`2026-07-03-phase13-render-chart.md`](../plans/2026-07-03-phase13-render-chart.md)｜**release**：[`2026-07-03-phase13-render-chart.md`](../releases/2026-07-03-phase13-render-chart.md)
- **验证**：**1224 passed 0 回归**（baseline 1208→1224，新增 16 测试：10 单测 + 6 e2e）。
- **S5 真跑通过**：E2E-1～5 全过（含 E2E-5 压测 3 run × 10 chart 无丢失/串扰）；E2E-6 **opencode + deepseek-v4-flash** 真跑 + 4 验证点（agent_message 完整 / TUI 各面板合理 / render_chart 推送 / 图表排布）逐条通过 + TUI snapshot 留档。
- **S5 顺带修 2 实施 gap**：ScriptExecutor 漏 chart env（违反 SPEC §11 #9 executor-agnostic）+ OrcaApp CLI shell 漏起 ingestor（与 RunManager 不对称）—— e2e 真跑发现的设计盲点。
- **CLAUDE.md 已记录**：测试后端固定使用 **opencode + deepseek-v4-flash**，不再用 claude 作后端测试。
- 分支：`phase13-render-chart`（未 merge）。

## 待办（等用户指示方向）

1. **phase-13 分支 merge / PR**（等用户决定）。
2. **phase-12 分支 merge / PR**（仍待，等用户决定）。
3. **前序 4-bug-fix 的 TUI 端到端验证**（仍待 manual）。

## 必读文件（下一任务开工前按需）

- [`docs/releases/2026-07-03-phase13-render-chart.md`](../releases/2026-07-03-phase13-render-chart.md)（phase-13 全貌 + S5 + 偏差 + 已知 gap）
- [`docs/specs/phase-13-render-chart.md`](../specs/phase-13-render-chart.md) §0.1 铁律 + §6 chart 事件契约 + §11 关键决策备忘
- [`docs/releases/2026-07-03-phase12-tui-redesign.md`](../releases/2026-07-03-phase12-tui-redesign.md)（phase-12 全貌 + S10，render_chart 渲染侧已就绪）
- [`orca/chart/_limits.py`](../../orca/chart/_limits.py)（大数据 / socket 限制常量同源 source of truth）
