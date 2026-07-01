# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**phase 1-9 全部完成并合并 master**。最近一次提交是 phase 9 浏览器 E2E 修复：

- **状态**：✅ phase 9 playwright E2E 全绿（20 passed）。修了一个真实后端 bug（SPA 深链 404 ——
  `server.py` 加 catch-all GET → index.html fallback，让 `/runs/<id>` 刷新/直链不再返回 404）
  + 4 个测试文件的测试代码 bug（live_server fixture / WS race / run_id URL 模式 / playwright
  snake_case API / 9d async 包装 / chart 测试挂载点）。
- **release note**：[`docs/releases/2026-07-01-phase9-browser-e2e-fix.md`](../releases/2026-07-01-phase9-browser-e2e-fix.md)
- **验收**：playwright E2E 20 passed（test_playwright 3 + 9b 6 + 9c 5 + 9d 6）、默认套件 599 passed 0 warnings、vitest 84 passed

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
