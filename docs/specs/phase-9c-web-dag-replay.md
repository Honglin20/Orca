# 阶段 9c SPEC —— iface/web DAG 可视化 + tape replay（单路径 fold + 增量 apply）

> **状态**：最终版（待分发实现）
> **依据**：[phase-9b-web-frontend-core.md](phase-9b-web-frontend-core.md)（store 契约）· [phase-3-events.md](phase-3-events.md) §3.4（apply_event 幂等）· Conductor graph/ 调研
> **范围**：ReactFlow DAG 渲染（含回环边）+ tape replay UI（时间旅行，单路径 fold + 增量 apply）+ Log Stream
> **前置**：phase 9b（store + hooks + RunDetailPage 容器）
> **commit 规范**：`feat(web):` 前缀，独立分支

---

## 0. 阶段目标 + 铁律

phase 9c 回答：**「怎么把 workflow 的 DAG 实时画出来、并支持时间旅行回放，且 live 和 replay 数据完全一致、长 workflow 不卡？」**

### 0.1 五条铁律（违反即返工）
1. **live 和 replay 同一 fold**：DAG 状态都从 `apply_event`（9b store 的 handler 表）算，**绝不写第二套渲染逻辑**（反 AgentHarness 双路径）。
2. **live 状态 == replay 状态**：run 完成后切 replay，拖到末尾的状态必须与 live 最后状态 byte-identical（有断言）。
3. **增量 apply（反全量重放卡顿）**：replay 拖滑块用前进 apply N..M / 后退 checkpoint rollback，不全量重置（反 Conductor setReplayPosition 全量重放缺点）。
4. **回环边正确处理**：DAG 有循环路由时（nas.yaml reviewer→optimizer），dagre 布局不乱翻边（抄 Conductor findBackEdges）。
5. **DAG 增量更新**：节点状态变化只更新该节点 data，不全量重建 ReactFlow elements（抄 Conductor，反全量 rebuild 性能问题）。

### 0.2 反模式（必须避免）
- ❌ live 和 replay 两套 fold 代码（AgentHarness 灾难根源）
- ❌ replay 每次拖滑块全量重置+全量重放（Conductor 卡顿）
- ❌ DAG 状态变化全量 rebuild elements（性能差）
- ❌ 回环边让 dagre 乱排（视觉错乱）

---

## 1. DAG 可视化（ReactFlow + dagre，抄 Conductor）

### 1.1 技术栈
- **@xyflow/react（ReactFlow 12）** + **@dagrejs/dagre**（自动布局）
- 节点类型按 kind 分派（AgentNode/ScriptNode/SetNode/ForeachGroup/ParallelGroup）

### 1.2 组件结构

```
components/graph/
├── WorkflowGraph.tsx       # 主组件：ReactFlow + dagre 布局 + 增量更新
├── graph-layout.ts         # dagre TB 布局 + findBackEdges（回环边，抄 Conductor）
├── AnimatedEdge.tsx        # 边（route_taken 高亮）
└── nodes/
    ├── AgentNodeWidget.tsx     # agent 节点（状态色 + token + spinner）
    ├── ScriptNodeWidget.tsx
    ├── SetNodeWidget.tsx
    ├── ForeachGroupWidget.tsx # foreach/parallel 组（父+子+进度计数）
    └── constants.ts           # NODE_STATUS_HEX（状态颜色）
```

### 1.3 WorkflowGraph 主组件（增量更新，抄 Conductor）

```typescript
// components/graph/WorkflowGraph.tsx
function WorkflowGraph() {
  const nodes = useStore(s => s.nodes);        // store 派生
  const wf = useStore(s => s.workflowDef);     // 静态拓扑（从 yaml）
  const [flowNodes, setFlowNodes] = useState([]);
  const [flowEdges, setFlowEdges] = useState([]);

  // Effect 1: 拓扑首次出现 / workflow 变化 → 全量 build + dagre 布局
  useEffect(() => {
    const { nodes: laid, edges } = applyDagreLayout(wf);  // 含 findBackEdges
    setFlowNodes(laid);
    setFlowEdges(edges);
  }, [wf]);

  // Effect 2: 节点状态变化 → 只更新 data（不全量 rebuild，抄 Conductor）
  useEffect(() => {
    setFlowNodes(nds => nds.map(n => {
      const s = nodes[n.id];
      if (!s) return n;
      return { ...n, data: { ...n.data, status: s.status, output: s.output } };
    }));
  }, [nodes]);

  return <ReactFlow nodes={flowNodes} edges={flowEdges} nodeTypes={NODE_TYPES} />;
}
```

### 1.4 回环边处理（抄 Conductor findBackEdges）

```typescript
// graph-layout.ts
function applyDagreLayout(wf) {
  const backEdges = findBackEdges(wf);  // DFS 找 reviewer→optimizer 这类回环
  // 把 backEdges 反向喂给 dagre（让它正确排名正向 DAG）
  // 但渲染时保持原方向（回环边画成弧形）
}
```

**理由**：dagre 默认会把回环边当普通边，导致节点排名混乱。Conductor 的解法是识别 back edges 反向喂图、渲染保持原方向——直接抄。

### 1.5 节点状态颜色（NODE_STATUS_HEX）

```typescript
// constants.ts
export const NODE_STATUS_HEX = {
  pending:  "#9CA3AF",  // 灰
  running:  "#3B82F6",  // 蓝
  done:     "#22C55E",  // 绿
  failed:   "#EF4444",  // 红
  blocked:  "#F59E0B",  // 黄（gate）
};
```

---

## 2. tape replay UI（时间旅行，单路径 fold + 增量 apply）

### 2.1 是什么

run 完成（或 live 进行中切 replay）后，底部出现 ReplayBar，可拖时间轴回到「第 N 个事件发生时」的状态。DAG/Log/Detail **全部回到该时刻**。

### 2.2 单路径原理（反 AgentHarness 双路径）

**replay 不是新路径，是同一个 fold 的两种数据注入时机**：
- live：WS 事件 → `processEvent`（9b handler 表）
- replay：定时器按时间戳推进 → `setReplayPosition` → **同一个 handler 表**重放到 N

**fold 函数只有一份**（9b 的 eventHandlers），live 和 replay 状态计算必然一致。

### 2.3 增量 apply（反 Conductor 全量重放卡顿）

```typescript
// stores/replay-actions.ts（store 扩展）
// checkpoint：每 K 个事件存一次 state snapshot
const CHECKPOINT_INTERVAL = 20;
const checkpoints: Map<number, WorkflowState> = new Map();

function setReplayPosition(pos: number) {
  const current = get().replayPosition;
  if (pos > current) {
    // 前进：apply events[current+1 .. pos]
    for (let i = current + 1; i <= pos; i++) {
      processEvent(events[i]);
      if (i % CHECKPOINT_INTERVAL === 0) checkpoints.set(i, snapshot());
    }
  } else {
    // 后退：找最近 checkpoint (< pos) → 从 checkpoint 恢复 → apply 到 pos
    const cp = nearestCheckpoint(pos);
    restore(checkpoints.get(cp));
    for (let i = cp + 1; i <= pos; i++) processEvent(events[i]);
  }
  set({ replayPosition: pos });
}
```

**性能**：拖滑块来回，最坏 apply CHECKPOINT_INTERVAL(20) 个事件，不全量重放几百个。

### 2.4 ReplayBar 组件

```
┌─────────────────────────────────────────────────────────────────┐
│ ◄◄  ▶  ████████░░░░░░░░░░░░░░  Event 23/47   速度 [1× ▼]        │
│     ────────────────────────────────────                        │
│     14:02:11 ────────────── 14:02:25 ────── 14:02:47            │
└─────────────────────────────────────────────────────────────────┘
```

- `◄◄ ▶`：播放/暂停（按事件时间戳间隔推进，调速 1×/5×/10×/20×）
- 滑块：拖到 Event N → setReplayPosition(N)
- 时间刻度：真实时间戳

### 2.5 live → replay 切换

```typescript
// run 完成后，Run Header 出现「⏮ Replay」按钮
function ReplayToggle() {
  const status = useStore(s => s.status);
  const setReplayMode = useStore(s => s.setReplayMode);
  if (status === "completed" || status === "failed") {
    return <button onClick={() => setReplayMode(true)}>⏮ Replay</button>;
  }
}
// 切到 replay：WS 停（已 completed），ReplayBar 显示，可拖时间轴
// 切回 live：ReplayBar 隐藏，状态恢复到最新（events 末尾）
```

### 2.6 历史 run replay

```typescript
// Sidebar 点 Done 的 run → navigate('/runs/<id>') → useRunEvents 加载全量事件
// → 默认进 replay 模式（因为已完成，无 live WS）→ 可拖时间轴
```

---

## 3. Log Stream（流式日志，按 session 分组）

### 3.1 组件

```typescript
// components/detail/LogStream.tsx
function LogStream() {
  const events = useStore(s => s.events);
  const replayPos = useStore(s => s.replayPosition);
  const replayMode = useStore(s => s.replayMode);
  // replay 模式只显示 events[0..replayPos]，live 显示全部
  const visible = replayMode ? events.slice(0, replayPos + 1) : events;
  return <VirtualList items={visible.map(formatLog)} />;  // react-window 虚拟滚动
}
```

### 3.2 关键约束
- **虚拟滚动**（react-window）：几千条日志不卡
- **按 session 分组**：agent 事件按 session_id 分组显示
- **replay 同步**：replay 模式 Log 也只显示到 replayPos（同一真相）

---

## 4. Detail Panel（选中节点详情）

```typescript
// components/detail/NodeDetail.tsx
function NodeDetail() {
  const selected = useStore(s => s.selectedNode);
  const node = useStore(s => s.nodes[s.selectedNode]);
  const replayMode = useStore(s => s.replayMode);
  // replay 模式显示该节点在 replayPos 时的快照
  return <div>状态: {node.status} / tokens / output / tools / thinking</div>;
}
```

---

## 5. 验收标准

### 5.1 DAG 可视化
- [ ] ReactFlow 渲染所有 node（按 kind 分派 widget）
- [ ] dagre TB 自动布局
- [ ] **回环边正确**（nas.yaml reviewer→optimizer 不乱排，playwright 截图断言布局合理）
- [ ] 状态颜色实时更新（done 绿/running 蓝/blocked 黄/failed 红）
- [ ] **增量更新**（不全量 rebuild，devtools profiler 验证只重渲染变化节点）
- [ ] parallel/foreach 组渲染（父+子+进度计数）

### 5.2 tape replay（核心）
- [ ] ReplayBar 时间轴 + 播放/速度
- [ ] 拖滑块 → DAG/Log/Detail 全部回到该时刻（playwright 拖动断言）
- [ ] **live 状态 == replay 末尾状态**（断言：run 完成后 replay 拖到末尾，nodes 状态 == live 最后状态）
- [ ] **增量 apply**（不卡）：100+ 事件的 run，拖滑块流畅（playwright 测响应时间 < 100ms）
- [ ] 历史 run 切 replay（点 Done run → 进 replay）
- [ ] live → replay 切换按钮

### 5.3 Log Stream
- [ ] 流式滚动（live）
- [ ] 虚拟滚动（1000+ 条不卡）
- [ ] 按 session 分组
- [ ] replay 同步（只显示到 replayPos）

### 5.4 单路径 fold（最重要）
- [ ] grep 前端只有一份 handler 表（无第二套 replay 渲染逻辑）
- [ ] live 和 replay 共用 processEvent（断言）

### 5.5 测试
- [ ] `frontend/test/graph.test.tsx`：dagre 布局 + 回环边 + 增量更新
- [ ] `frontend/test/replay.test.ts`：增量 apply + checkpoint + live==replay
- [ ] playwright：DAG 渲染 + replay 拖动 + 截图

### 5.6 playwright 验收（AI 自动测）
- [ ] **DAG 渲染**：playwright 截图，断言节点数 == workflow node 数
- [ ] **回环边**：截图断言 nas.yaml 的 DAG 没有乱翻边（节点位置合理）
- [ ] **replay 拖动**：playwright 拖滑块到中间，断言某节点状态变化（如从 done 变 running）
- [ ] **live==replay**：run 完成 → replay 拖到末尾 → 断言 nodes 状态 == live 末尾
- [ ] **增量不卡**：100 事件 run，拖滑块响应 < 100ms（playwright measure）
- [ ] **历史 replay**：点 Done run → 断言进 replay 模式 + ReplayBar 显示

---

## 6. 给后续阶段的契约

| 后续 | phase 9c 提供 |
|---|---|
| phase 9d gate-chart | Detail Panel 容器（chart 渲染挂在节点详情里）+ store.gate（gate 弹窗读）|

---

## 7. 不做的事

- ❌ gate 弹窗 / chart 渲染 —— phase 9d
- ❌ 后端 / store 骨架 / 路由 —— 9a/9b
- ❌ 第二套 replay fold —— 红线

---

## 8. 关键决策备忘（防 drift）

1. **ReactFlow + dagre**（抄 Conductor 实际栈）
2. **回环边 findBackEdges**（抄 Conductor，反 dagre 乱排）
3. **DAG 增量更新**（不全量 rebuild，抄 Conductor）
4. **replay 单路径 fold**（同一 handler 表，反双路径）
5. **增量 apply + checkpoint**（反 Conductor 全量重放卡顿）
6. **live == replay 末尾**（断言保证一致）
7. **Log 虚拟滚动 + session 分组**
8. **历史 run 默认进 replay**（已完成无 live WS）
