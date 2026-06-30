# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**phase 9 全部完成** —— 9a（后端）/ 9b（前端骨架）/ 9c（DAG + replay）/ 9d（gate 弹窗 + render_chart）
四子阶段全部交付，在 **`phase9-web` 分支** 上。**分支可合并 master**。

- **状态**：✅ phase 9 完成（Web 壳全栈可用：列表 / 详情 / DAG 可视化 / 流式日志 / tape replay /
  gate 弹窗 / chart 渲染 / Output 视图）
- **9d release note**：[`docs/releases/2026-06-30-phase9d-web-gate-chart.md`](../releases/2026-06-30-phase9d-web-gate-chart.md)
- **9d commit**：`6d0c5e1`（`feat(web):` 前缀）
- **验收**：vitest 84 passed（gate 10 + chart 16 + 既有 58 零回归）；npm run build 成功；
  pytest 595 通过 0 RuntimeWarning；playwright 9d 6 场景 collected；五铁律 + §1.6 全过

## 下一步

**phase 9 收尾**：`phase9-web` 分支已就绪，可发起合并到 master（4 个子阶段 commit：
`b34c87d` 9a / `0347a66` 9b / `adc856c` 9c / `6d0c5e1` 9d）。

**phase 10（MCP）**：让 claude 能调 `render_chart` + `ask_user` 工具（9d 已就位前端渲染，
phase 10 补 MCP 工具实现让 claude 实际产出 chart/ask 事件）。SPEC 待写。

### 9c deferred（仍待根治，非阻塞）
- **n4 双轮询根治**：`RunsListPage` + `RunsSidebar` 各自 `useRunsList`（2×/2s 元数据轮询）。
  根治需 React Context 提升 `useRunsList` 单实例。

## 必读文件（phase 10 开工前）

1. [`docs/specs/shells-design-draft.md`](../specs/shells-design-draft.md)（三壳共同契约 + MCP 约束）
2. [`docs/releases/2026-06-30-phase9d-web-gate-chart.md`](../releases/2026-06-30-phase9d-web-gate-chart.md)（chart 渲染契约，phase 10 MCP 补工具）
3. [`orca/iface/web/frontend/src/components/chart/types.ts`](../../orca/iface/web/frontend/src/components/chart/types.ts)（ChartPayload 契约）
4. [`orca/gates/types.py`](../../orca/gates/types.py)（HumanGate，phase 10 ask_user 工具复用）
