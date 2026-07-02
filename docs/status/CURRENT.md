# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前状态：phase-12 TUI 重设计完成（S0–S10 全过），无进行中任务

**phase-12 CLI TUI 重设计收官**：DagGraph 拓扑图 + NodeDetail（6 kind 永不空）+ 终端图表渲染（plotext braille）+ ChartBrowser。
- **SPEC**：[`phase-12-cli-tui-redesign.md`](../specs/phase-12-cli-tui-redesign.md)（v2 对抗审闭环）｜**计划**：[`2026-07-03-phase12-tui-redesign.md`](../plans/2026-07-03-phase12-tui-redesign.md)｜**release**：[`2026-07-03-phase12-tui-redesign.md`](../releases/2026-07-03-phase12-tui-redesign.md)
- **验证**：1133 passed 0 回归；**S10 = opencode 后端真跑 e2e 通过**（glm-4.6v，SPEC §6 逐项 + 断言证据；图表走解耦注入真路径，braille + 多图分组规整）。
- **e2e 顺带修真 bug**：`ClaudeExecutor` 无条件注 `--allowed-tools`/`--mcp-config` → opencode spawn 失败，gate 到 `capabilities.mcp_tools` 修复。
- 分支：`phase12-tui-redesign`（未 merge）。

## 待办（等用户指示方向）

1. **`render_chart` MCP 生产者（phase-10，外部）**：未实现；TUI 渲染侧已就绪 + 验证。落地后可补「agent 真调 render_chart → 图自动出」回归（无需改 TUI）。
2. **phase-12 分支 merge / PR**（等用户决定）。
3. **`orca executor` 真实端到端 manual 验证（待 ccr/claude + key 环境）**。
4. **前序 4-bug-fix 的 TUI 端到端验证**（仍待 manual）。

## 必读文件（下一任务开工前按需）

- [`docs/releases/2026-07-03-phase12-tui-redesign.md`](../releases/2026-07-03-phase12-tui-redesign.md)（phase-12 全貌 + S10 + 偏差 + 已知 gap）
- [`docs/specs/phase-9d-web-gate-chart.md`](../specs/phase-9d-web-gate-chart.md) §2 + `orca/iface/web/frontend/src/components/chart/types.ts`（图表契约 source of truth）
