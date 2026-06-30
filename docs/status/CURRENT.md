# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**phase 9b（iface/web 前端骨架）已完成** —— 在 **`phase9-web` 分支** 上。
phase 9 全部子阶段（9a/9b/9c/9d）在此分支开发，**勿切回 master**。

- **状态**：✅ 9b 前端骨架完成（vitest 22 passed：store 13 + hooks 9；npm run build 成功输出
  static/；6 playwright integration collect；594 Python 全绿零回归 0 RuntimeWarning；六条铁律
  grep 全过；review 全修复 M3/Minor4/Nit2，n4 双轮询 deferred 9c）
- **release note**：[`docs/releases/2026-06-30-phase9b-web-frontend-core.md`](../releases/2026-06-30-phase9b-web-frontend-core.md)
- **CHANGELOG**：[`docs/status/CHANGELOG.md`](CHANGELOG.md)
- **commit 规范**：`feat(web):` 前缀（web 是纯渲染/转发层）

## 下一步（phase 9c dag-replay）

phase 9c：DAG 可视化 + replay UI（RunDetailPage 的 dag tab 内容 + 节点详情）。
参考 [`docs/specs/phase-9c-web-dag-replay.md`](../specs/phase-9c-web-dag-replay.md) +
[`docs/plans/2026-06-30-phase9c-web-dag-replay.md`](../plans/2026-06-30-phase9c-web-dag-replay.md)。

phase 9b 提供给 9c 的契约（SPEC §8）：
- `RunDetailPage` 容器 + 四 tab 占位（dag/log/output/yaml，dag 待 9c 填）
- `useWorkflowStore`：`nodes`（每节点 status/output）、`gate`、`events`（全量缓存）、
  `selectedNode`/`replayMode`/`replayPosition` UI 交互态 + actions
- `useRunEvents(runId)` / `useWebSocket(runId)` 已就位（懒加载 + 按需订阅 + 重连全量重拉）
- `types/events.ts`：WorkflowEvent（含 data payload 字段注释）/ NodeState / GateState

### 9c 待办（review deferred）
- **n4 双轮询根治**：`RunsListPage` + `RunsSidebar` 各自 `useRunsList`（2×/2s 元数据轮询）。
  根治需 React Context 提升 `useRunsList` 单实例（9c 引入 `RunsProvider`）。

## 必读文件（9c 开工前）

1. [`docs/specs/phase-9c-web-dag-replay.md`](../specs/phase-9c-web-dag-replay.md)
2. [`docs/plans/2026-06-30-phase9c-web-dag-replay.md`](../plans/2026-06-30-phase9c-web-dag-replay.md)
3. [`docs/releases/2026-06-30-phase9b-web-frontend-core.md`](../releases/2026-06-30-phase9b-web-frontend-core.md)（9b store/hook 契约）
4. [`orca/iface/web/frontend/src/stores/workflow-store.ts`](../../orca/iface/web/frontend/src/stores/workflow-store.ts)（store + eventHandlers 实态）
