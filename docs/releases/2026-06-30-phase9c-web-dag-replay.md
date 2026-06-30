# Release Note —— phase 9c：iface/web DAG 可视化 + tape replay

> **日期**：2026-06-30
> **分支**：`phase9-web`
> **SPEC**：[`docs/specs/phase-9c-web-dag-replay.md`](../specs/phase-9c-web-dag-replay.md)
> **计划**：[`docs/plans/2026-06-30-phase9c-web-dag-replay.md`](../plans/2026-06-30-phase9c-web-dag-replay.md)
> **commit 规范**：`feat(web):` 前缀

---

## 做了什么

phase 9c 回答「怎么把 workflow 的 DAG 实时画出来、并支持时间旅行回放，且 live 和 replay
数据完全一致、长 workflow 不卡？」。

### 1. 拓扑来源决策（跨阶段后端改动，surgical）

**决策**：在 `workflow_started` 事件 data 里新增紧凑 `topology` 摘要（option a），而非加到
`/api/runs/<id>` snapshot。理由（铁律「tape 是唯一真相源」）：

- live：第一个事件即拿到拓扑，DAG 立刻布局（无需等 `route_taken` 增量拼边）。
- 历史 run replay：同样从事件拿，单一数据源，无额外 endpoint。
- 摘要只含 node name+kind / routes(from→to, when?) / parallel(name+branches) —— 非完整 yaml，
  保持 payload 小。foreach body 不展开（动态并行，运行时按 `foreach_started` 渲染分支）。

**后端改动**（`orca/run/lifecycle.py`）：`make_workflow_started` 新增 `topology` 字段 +
`_topology_summary(wf)` helper。非破坏（旧消费者忽略未知字段；前端对 topology 缺失做兜底）。

**测试**：`tests/run/test_lifecycle.py::test_make_workflow_started_topology_summary` 覆盖
含 parallel 组 + 回环边的拓扑。

### 2. DAG 可视化（ReactFlow + dagre，C1）

- `components/graph/graph-layout.ts`：dagre TB 布局 + `findBackEdges`（DFS 三色识别回环边，
  抄 Conductor）+ `mergeNodeStatus`（增量更新，未变节点保持原引用）+ `markTakenEdges`。
- 5 种 node widget（`AgentNodeWidget`/`ScriptNodeWidget`/`SetNodeWidget`/
  `ForeachGroupWidget`/`EndNodeWidget`）+ 共享 `NodeShell` 外壳（DRY）。
- `WorkflowGraph.tsx`：3 effect 模式 —— Effect 1 拓扑首次出现全量 build + dagre 布局；
  Effect 2 节点状态变化只更新对应 node data（铁律 5）；Effect 3 route_taken 标记走过的边。
- `AnimatedEdge.tsx`：taken 边蓝色流动 + 回环边圆角弧形（渲染保持原方向）。
- `NODE_STATUS_HEX` 5 色（pending 灰 / running 蓝 / done 绿 / failed 红 / blocked 黄）。

### 3. tape replay（增量 apply + checkpoint，C2）

- `stores/replay-actions.ts`：`setReplayTarget` 前进 apply events(current, pos] / 后退从最近
  checkpoint restore 再 apply。每 `CHECKPOINT_INTERVAL`(20) 个事件存 snapshot。
  `enterReplay` 建一个 position=-1 空态 checkpoint，消除「后退到首个 checkpoint 前触发全量
  重置」分支（铁律 3：永远增量）。`exitReplay` 恢复 live 末态。
- **单路径 fold（铁律 1）**：replay 的 `applyOne` 复用 store 的 `foldEvent`（同一 handler 表），
  仅绕过 `processEvent` 外壳的 seq 去重 + events.push。无第二套 replay fold。
- `hooks/use-replay.ts`：定时器推进（speed 1×/5×/10×/20×），unmount/pause 自动清 timer（无 leak）。
- `components/layout/ReplayBar.tsx`：滑块 + 播放/暂停 + 速度下拉。

### 4. Log Stream + Node Detail（C3）

- `components/detail/LogStream.tsx`：react-window v2 `List` 虚拟滚动（1000 事件 < 50 DOM row）
  + 按 session_id 分组（连续相同 session 第一条标组头）+ replay 模式只显示 events[0..pos]。
- `components/detail/NodeDetail.tsx`：选中节点 status/output/progress + 该节点相关事件流。

### 5. RunDetailPage 集成（C4）

- 主区 DAG tab（WorkflowGraph）+ 右侧 NodeDetail + 底部 Log/Output/Yaml tab。
- run 完成（completed/failed）→ Header 出现「⏮ Replay」按钮；进 replay 显示 ReplayBar。

---

## 偏离计划

无重大偏离。计划 C2.1 写「replay 扩展 store」（`replay-actions.ts`），实际抽成独立模块
（不进 store state，模块级单例 buffer）—— 原因：replay buffer 是 replay 内部缓存，非业务
真相，放进 store 反而每次 set 触发重渲染。store 通过依赖注入（`get/set/applyOne`）调用，
未反向耦合。

---

## 五条铁律验收（SPEC §0.1）

| 铁律 | 验证 | 证据 |
|------|------|------|
| 1. live/replay 同 fold | ✓ grep 唯一 `eventHandlers` 表（workflow-store.ts:110） | replay-actions.ts 的 applyOne 调 foldEvent（同一表），无第二套 fold |
| 2. live==replay byte-identical | ✓ replay.test.ts 显式断言（含 cost/gate/foreach 富流） | snapshotState 比对 nodes/gate/cost/workflowName/status/workflowDef |
| 3. 增量 apply（反全量重放） | ✓ setReplayTarget 前进/后退 + checkpoint | replay.test.ts 断言前进只 apply 增量、后退用 checkpoint、checkpoint 内容正确 |
| 4. 回环边正确处理 | ✓ findBackEdges DFS + 反向喂 dagre | graph.test.tsx 覆盖自环/三角回环；playwright 断言回环节点 y 坐标不乱 |
| 5. DAG 增量更新 | ✓ mergeNodeStatus 保持未变节点原引用 | graph.test.tsx 断言未变节点引用不变 + progress 透传 |

---

## 验证结果

- **vitest**：58 passed（store 13 + graph 15 + replay 12 + hooks 9 + log-detail 9）
- **npm run build**：成功（225 modules，输出 static/）
- **uv run pytest -q -m "not integration"**：595 passed, 0 RuntimeWarnings，phase 1-9b 零回归
- **playwright 9c**：5 tests collect（`@pytest.mark.integration`，无浏览器自动 skip）

---

## Review findings + 修复

`code-reviewer` 发现 3 Must-fix + 5 Minor + 3 Nit，全部修复：

- **[M5] foreach/parallel progress 不透传到 DAG widget**：`mergeNodeStatus` 签名 + 透传加
  `progress` 字段（`graph-layout.ts`），未变判定纳入 progress 比较。补端到端测试
  （`graph.test.tsx` "透传 progress" + "progress 变化触发更新"）。
- **[M1/2] live==replay 测试 vacuous-pass**：`snapshotState` 纳入 `workflowDef`；新增
  `buildRichStream`（覆盖 agent_usage/foreach/gate 派生），加「富流 live==replay」断言。
- **[M3] 后退到首个 checkpoint 前触发全量重置**：`enterReplay` 建 `checkpoints.set(-1, empty)`，
  消除 `setReplayTarget` 的无 checkpoint 全量重置分支。
- **[Minor] resetDerivedState 重复 4 处**：抽 `resetDerived(set)` helper（replay-actions.ts），
  replayState/unloadRun/enterReplay 复用（DRY）。
- **[Minor] mergeNodeStatus 不覆盖 output 变化**：未变判定加 output 比较。
- **[Minor] WorkflowNodeData 类型漏 progress**：加显式 `progress?: string` 字段。
- **[Minor] playwright 回环边断言过弱**：改为断言「回环节点 y 坐标不大于前驱 y」。
- **[Minor] checkpoint 内容正确性无回归保护**：加「checkpoint[19].nodes == 从头 fold 到 19」断言。
- **[Nit] 未知 event type 静默**：`foldEvent` 未知 type 加 `console.warn`（fail loud）。

---

## Commit SHA

待填入（见 CHANGELOG 索引）。

## 关键文件

**后端**（surgical topology change）：
- `orca/run/lifecycle.py`：`_topology_summary()` + `make_workflow_started` 加 topology
- `tests/run/test_lifecycle.py`：拓扑摘要测试

**前端**（NEW）：
- `orca/iface/web/frontend/src/components/graph/{graph-layout,WorkflowGraph,AnimatedEdge,constants}.ts(x)` + `nodes/`
- `orca/iface/web/frontend/src/components/detail/{LogStream,NodeDetail}.tsx`
- `orca/iface/web/frontend/src/components/layout/ReplayBar.tsx`
- `orca/iface/web/frontend/src/stores/replay-actions.ts`
- `orca/iface/web/frontend/src/hooks/use-replay.ts`
- `orca/iface/web/frontend/src/types/topology.ts`

**前端**（MODIFIED）：
- `orca/iface/web/frontend/src/stores/workflow-store.ts`：workflowDef + foreach progress 派生 + replay 接线 + foldEvent 提取
- `orca/iface/web/frontend/src/types/events.ts`：NodeState.progress
- `orca/iface/web/frontend/src/components/pages/RunDetailPage.tsx`：集成 graph/detail/log/replay

**测试**：
- `orca/iface/web/frontend/test/{graph,replay,log-detail}.test.tsx` + `fixtures/events.ts`
- `tests/iface/web/test_playwright_9c.py`
