# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**phase 9c（iface/web DAG 可视化 + tape replay）已完成** —— 在 **`phase9-web` 分支** 上。
phase 9 全部子阶段（9a/9b/9c/9d）在此分支开发，**勿切回 master**。

- **状态**：✅ 9c 完成（vitest 58 passed：store 13 + graph 15 + replay 12 + hooks 9 +
  log-detail 9；npm run build 成功输出 static/；5 playwright integration collect；
  595 Python 全绿零回归 0 RuntimeWarning；五条铁律全过；review 全修复 3 Must-fix +
  5 Minor + Nit）
- **release note**：[`docs/releases/2026-06-30-phase9c-web-dag-replay.md`](../releases/2026-06-30-phase9c-web-dag-replay.md)
- **CHANGELOG**：[`docs/status/CHANGELOG.md`](CHANGELOG.md)
- **commit 规范**：`feat(web):` 前缀（web 是纯渲染/转发层）

## 下一步（phase 9d gate-chart）

phase 9d：gate 弹窗 + chart 渲染（RunDetailPage 的 output tab 内容 + gate modal）。
参考 [`docs/specs/phase-9d-web-gate-chart.md`](../specs/phase-9d-web-gate-chart.md)（待确认）
+ [`docs/plans/2026-06-30-phase9d-web-gate-chart.md`](../plans/2026-06-30-phase9d-web-gate-chart.md)（待确认）。

phase 9c 提供给 9d 的契约（SPEC §6）：
- `store.gate`（gate 弹窗读，human_decision_requested 派生）
- `events` filter custom（chart 渲染读，按 data.kind 分发）
- Detail Panel 容器（chart 挂在节点详情里）
- DAG graph + NodeDetail + LogStream 已就位（9d 在此基础上加 gate modal + chart widget）

### 9c deferred（review 无 deferred 项；9b n4 双轮询仍待 9d 根治）
- **n4 双轮询根治**：`RunsListPage` + `RunsSidebar` 各自 `useRunsList`（2×/2s 元数据轮询）。
  根治需 React Context 提升 `useRunsList` 单实例（9d 引入 `RunsProvider`）。

## 必读文件（9d 开工前）

1. [`docs/specs/phase-9d-web-gate-chart.md`](../specs/phase-9d-web-gate-chart.md)（如已存在）
2. [`docs/releases/2026-06-30-phase9c-web-dag-replay.md`](../releases/2026-06-30-phase9c-web-dag-replay.md)（9c store/graph 契约）
3. [`orca/iface/web/frontend/src/stores/workflow-store.ts`](../../orca/iface/web/frontend/src/stores/workflow-store.ts)（store + eventHandlers + workflowDef + replay 实态）
4. [`orca/iface/web/frontend/src/components/detail/NodeDetail.tsx`](../../orca/iface/web/frontend/src/components/detail/NodeDetail.tsx)（9d chart 挂载点）
