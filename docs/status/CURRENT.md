# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**phase 1-9 全部完成并合并 master**。最近一次提交是 phase-1 critical infra 卫生修复：

- **状态**：✅ Tape 写句柄惰性打开（消除 ResourceWarning）完成。`Tape.__init__` 不再 eager-open
  append handle —— 只读构造（replay/inspect）不再泄漏未关闭句柄；首次 `append()` 在锁内惰性打开；
  `__del__` leak 安全网兜底忘 close 的调用方。
- **release note**：[`docs/releases/2026-07-01-tape-lazy-open.md`](../releases/2026-07-01-tape-lazy-open.md)
- **验收**：`-W "error::ResourceWarning"` 全绿（30→0）、RuntimeWarning 全绿、599 passed 零回归、vitest 84 passed

## 下一步

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
