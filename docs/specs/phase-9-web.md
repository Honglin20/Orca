# 阶段 9 SPEC —— iface/web Web 壳（索引页）

> **状态**：已拆分为 4 份子 SPEC（2026-06-30）。本文档保留为**索引 + 设计总览**，具体契约/验收见各子 SPEC。
> **依据**：[shells-design-draft.md](shells-design-draft.md) §4 · Conductor web/ 实际实现调研 · AgentHarness chartTheme.ts 调研
> **范围**：FastAPI 后端（单进程同引擎）+ WebSocket 单通道 + React SPA（ReactFlow DAG + Zustand 单 store + tape replay + render_chart）
> **前置**：phase 5（Orchestrator）+ phase 6（gates）实现完成
> **gate UX 主战场** + **tape replay UI（Orca 独有杀手锏）**

---

## 子 SPEC 索引（实现时以此为准）

| 子阶段 | SPEC | 范围 |
|---|---|---|
| **9a 后端** | [phase-9a-web-backend.md](phase-9a-web-backend.md) | FastAPI + WS + RunManager 真并发 + 懒加载 API |
| **9b 前端骨架** | [phase-9b-web-frontend-core.md](phase-9b-web-frontend-core.md) | 路由导航栈 + Zustand 单 store + 懒加载 + WS hook |
| **9c DAG+replay** | [phase-9c-web-dag-replay.md](phase-9c-web-dag-replay.md) | ReactFlow DAG + tape replay 单路径 fold + 增量 apply |
| **9d gate+chart** | [phase-9d-web-gate-chart.md](phase-9d-web-gate-chart.md) | gate 富交互弹窗 + render_chart 迁移 AgentHarness 5 种图 |

**总开发计划**：[`docs/plans/2026-06-30-phase9-web-plan.md`](../plans/2026-06-30-phase9-web-plan.md)

> ⚠️ 本文以下内容为**设计总览**（保留用于上下文），**不是契约**。具体接口/数据契约/验收标准以 4 份子 SPEC 为准。若本文与子 SPEC 冲突，**以子 SPEC 为准**。

---

## 0. 阶段目标

phase 9 回答唯一一个问题：**「用户在浏览器怎么跑/看/回放 workflow，并富交互地回答 gate？」**

| 模块 | 解决什么 | 核心交付 |
|---|---|---|
| FastAPI 后端 | 单进程同引擎，REST + WS | `/api/state` 全量 + `/ws` 单通道 + `/api/run` 启动 |
| WebSocket 单通道 | 事件推送（唯一通道，反双 WS）| sync emit + Queue 桥 + broadcaster（抄 Conductor）|
| React SPA | 前端应用 | React 19 + Vite + ReactFlow + Zustand + Tailwind |
| DAG 可视化 | workflow 进度图 | ReactFlow + dagre 自动布局 + 回环边处理 |
| Log Stream | 流式事件日志 | 订阅事件，按 session 分组滚动 |
| Gate 弹窗 | 富交互人工确认 | 选项/自由文本/上下文/取消 |
| tape replay UI | 时间旅行调试（独有） | 单路径 fold（apply_event），增量 apply 防卡 |
| render_chart | claude 产出的图表渲染 | 订阅 custom(chart) 事件 → recharts |

**核心铁律（最重要）**：tape 是唯一真相源，Web 壳无业务真相，UI 只是真相的推送（反 AgentHarness 多 store 灾难）。

---

## 1. 技术栈决策（2026-06-30 定稿，抄 Conductor 实际验证的栈）

### 1.1 后端
**FastAPI + uvicorn**（单进程，同引擎 asyncio 事件循环）。
- 理由（Conductor 设计决策 D4）：零 IPC、零序列化开销，引擎和 WS 共享事件循环。CLI 工具/编排框架的最优解。

### 1.2 前端
**React 19 + Vite + ReactFlow（DAG）+ dagre（布局）+ Zustand（单 store）+ Tailwind + recharts（chart）**。
- 理由：Conductor **实际用的就是这套**（不是它过时 design.md 说的「单 HTML+Cytoscape」）。
  - ReactFlow+dagre 是 DAG 可视化成熟方案，Conductor 还解决了回环边反向喂图（直接抄）。
  - Zustand 单 store + eventHandlers 表 = 事件溯源前端范式，契合唯一真相源。

### 1.3 不用 Go/Rust 后端
单个编排器事件量小（几百到几千），Python asyncio 足够。Go/Rust 要重写 exec + 跨语言 IPC + 失去 Python 生态。Orca 全栈 Python + 前端 React 独立是正确选择。

---

## 2. 唯一真相源铁律（2026-06-30 定稿，最重要）

**Tape 是唯一真相源。Web 壳无自己的业务真相，UI 只是真相的推送。** 根治 AgentHarness 多 store 投影分裂灾难。

### 2.1 五条具体铁律（必须逐条验收）
1. **Tape 唯一真相**：所有 UI 状态都是 tape 的 fold 派生物（Conductor 验证：前端 handler 表，派生状态随时可重建）。
2. **前端无业务真相**：前端只有事件 handler 表（fold）+ 临时 UI 交互态（selectedNode/replayPosition，不算业务真相）。
3. **重连 = 全量重放**（Conductor 验证最简单正确）：WS 断了重连，`GET /api/state` 拿全量事件 replay，状态必然一致。
4. **gate 状态写 tape**：requested/resolved 都是事件，三壳从同一份 tape 读。
5. **WS 单通道**（反 AgentHarness 双 WS）：所有事件/gate/决策走一条 WS；反向通道同 WS 收 gate_response。

### 2.2 反模式清单（必须避免，来自 AgentHarness 教训）
- ❌ 多 store（run store / event store / message store 各一份真相）
- ❌ 非幂等 reducer（同事件重放结果不一致）
- ❌ 多 sidecar（每个 store 配一个 sidecar 文件，多真相源漂移）
- ❌ 双 WS / 双激活管线
- ❌ live 和 replay 两套渲染代码

---

## 3. tape replay UI（单路径，不是双路径）

### 3.1 是什么
tape replay = **时间旅行调试**：选历史 run → 拖时间轴 → 回到「第 N 个事件发生时」的状态看 DAG/输出/日志。

### 3.2 单路径原理（避免 AgentHarness 双路径灾难）
**replay 不是新路径，是同一个 fold 的两种数据注入时机**：
- **live**：WS 实时推事件 → `apply_event` fold
- **replay**：HTTP 拿历史事件，按时间戳推进 → `apply_event` fold
- **fold 函数只有一份**（phase 3 `replay.py` 的 `apply_event`），live 和 replay 状态计算永远一致。

**对比 AgentHarness 错误**：它用两套渲染代码（live 一套 + replay 一套）→ 漂移。Orca 用同一份 `apply_event`（phase 3 SPEC §6.0 铁律 3「一条读路径：streaming = replay = 同一个 apply_event」已锁定）。

### 3.3 性能优化（避免 Conductor 的缺点）
Conductor 的 replay 每次拖滑块全量重置+全量重放，长 workflow 卡。Orca 用**增量 apply**：
- 前进（N→M）：apply events[N+1..M]
- 后退（M→N）：rollback（每类事件记录逆操作）或 checkpoint 快照（每 K 个事件存一次 state，回退到最近 checkpoint 再 apply）

---

## 4. 后端设计（FastAPI）

### 4.1 文件结构

```
orca/iface/web/
├── __init__.py          # 导出 create_app / run_server
├── server.py            # FastAPI app + lifespan（启动 broadcaster）+ 路由
├── ws_handler.py        # WebSocket 单通道 + sync emit→Queue 桥 + broadcaster
├── replay_server.py     # 只读 replay server（无 WS，加载历史 tape）
└── static/              # Vite 构建产物（frontend/dist → static/）
```

### 4.2 路由

```python
# GET /              → static/index.html
# GET /api/state     → 全量事件（tape.replay()）—— 重连/late-joiner 用
# GET /api/runs      → 历史 run 列表（ls runs/）
# GET /api/runs/<id> → 某历史 run 的全量事件（replay 用）
# POST /api/run      → 启动新 run（body: yaml_path, inputs, task）
# POST /gate         → hook 桥（phase 6 已实现，复用）
# POST /gate/respond → 壳 resolve（body: gate_id, answer, source）
# WS /ws             → 单通道（事件推送 + gate_response 反向）
```

### 4.3 WebSocket 单通道（抄 Conductor）

**核心机制**：引擎同步 emit + asyncio.Queue 桥 + 独立 broadcaster task。

```python
# orca/iface/web/ws_handler.py
class WebServer:
    def __init__(self, engine, tape):
        self._connections: set[WebSocket] = set()
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._tape = tape

    def attach_to_bus(self, bus: EventBus):
        """订阅 bus，事件入 queue（不阻塞引擎）。"""
        sub = bus.subscribe()
        asyncio.create_task(self._pump(sub))

    async def _pump(self, sub):
        async for event in sub.events():
            await self._queue.put(event)  # 桥 sync→async

    async def _broadcaster(self):
        """独立 task：从 queue 取事件，广播给所有 WS 连接。"""
        while True:
            event = await self._queue.get()
            for ws in list(self._connections):
                try: await ws.send_json(event.model_dump())
                except: self._connections.discard(ws)

    async def ws_endpoint(self, ws: WebSocket):
        await ws.accept()
        self._connections.add(ws)
        try:
            while True:
                msg = await ws.receive_json()  # 反向通道
                if msg["type"] == "gate_response":
                    self.gate_handler.resolve(msg["gate_id"], msg["answer"], "web")
        except WebSocketDisconnect:
            self._connections.discard(ws)
```

**关键约束**：
- **单通道**：所有事件类型复用一条 WS。
- **重连全量重拉**（Conductor 验证）：WS 断了重连，前端先 `GET /api/state` 全量 replay 再开 WS。
- **反向通道**：同 WS 收 `gate_response`（壳 resolve 路径）。

### 4.4 后端单进程（同引擎事件循环）

```python
# orca/iface/web/server.py
async def run_server(engine, host, port):
    app = create_app(engine)
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()  # 与 orchestrator 同事件循环
```

orchestrator 和 uvicorn 在**同一个 asyncio 事件循环**里跑（CLI 启动时 `asyncio.gather(orchestrator.run(), run_server(...))`）。

---

## 5. 前端设计（React SPA）

### 5.1 文件结构

```
orca/iface/web/frontend/
├── package.json          # react/vite/reactflow/zustand/tailwind/recharts
├── vite.config.ts        # outDir: ../static
├── index.html
└── src/
    ├── main.tsx          # createRoot
    ├── App.tsx           # LiveMode / ReplayMode 切换
    ├── stores/
    │   └── workflow-store.ts  # Zustand 单 store + eventHandlers 表（抄 Conductor）
    ├── hooks/
    │   ├── use-websocket.ts   # WS 连接 + 重连 + 全量重拉
    │   └── use-replay.ts      # replay 模式（定时器推进 + setReplayPosition）
    ├── components/
    │   ├── layout/
    │   │   ├── Header.tsx         # run_id/wf/进度/awaiting
    │   │   ├── Sidebar.tsx        # Runs 列表（live + history）
    │   │   ├── ReplayBar.tsx      # 时间轴滑块 + 播放/速度
    │   │   └── OutputPane.tsx     # 最终输出
    │   ├── graph/
    │   │   ├── WorkflowGraph.tsx  # ReactFlow DAG（主组件）
    │   │   ├── graph-layout.ts    # dagre 布局 + 回环边处理（抄 Conductor）
    │   │   └── nodes/             # AgentNode/ScriptNode/SetNode/GroupNode/...
    │   ├── detail/
    │   │   ├── LogStream.tsx      # 流式日志（按 session 分组）
    │   │   └── NodeDetail.tsx     # 选中节点详情
    │   ├── gate/
    │   │   └── GateDialog.tsx     # gate 弹窗（按 source 渲染）
    │   └── chart/
    │       └── ChartRenderer.tsx  # render_chart（订阅 custom(chart) → recharts）
    └── types/
        └── events.ts      # 事件类型定义（对齐 phase 1 EventType）
```

### 5.2 Zustand 单 store（事件溯源范式，抄 Conductor）

```typescript
// stores/workflow-store.ts
interface WorkflowState {
  events: WorkflowEvent[];      // 事件日志（真相源在 tape，前端缓存用于 replay）
  nodes: Record<string, NodeState>;  // 派生：节点状态
  routes: EdgeState[];
  selectedNode: string | null;  // UI 交互态（非业务真相）
  gate: GateState | null;
  replayMode: boolean;
  replayPosition: number;       // UI 交互态
  // ...
  processEvent: (event: WorkflowEvent) => void;  // 统一 fold 入口
  replayState: (events: WorkflowEvent[]) => void;
  setReplayPosition: (pos: number) => void;
}

// 事件 handler 表（抄 Conductor，单一 fold）
const eventHandlers: Record<string, (state, data, timestamp) => void> = {
  workflow_started: (s, d, t) => { ... },
  node_started: (s, d, t) => { s.nodes[d.node].status = "running"; ... },
  node_completed: (s, d, t) => { ... },
  human_decision_requested: (s, d, t) => { s.gate = {...}; },
  human_decision_resolved: (s, d, t) => { s.gate = null; },
  // ... 每个 EventType 一个 handler
};
```

**关键**：handler 表是**唯一的状态计算路径**。live 和 replay 都调 `processEvent`/`replayState`，状态必然一致。

### 5.3 use-websocket hook（重连全量重拉，抄 Conductor）

```typescript
// hooks/use-websocket.ts
function useWebSocket() {
  useEffect(() => {
    // 1. 先全量重拉（初始 + 重连都走这条）
    fetch('/api/state').then(r => r.json()).then(events => {
      if (events.length) replayState(events);  // 全量 replay
    });
    // 2. 再开 WS 收增量
    const ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onmessage = e => processEvent(JSON.parse(e.data));
    ws.onclose = () => scheduleReconnect();  // 指数退避 + 重连又走全量重拉
  }, []);
}
```

### 5.4 DAG 可视化（ReactFlow + dagre，抄 Conductor）

- `WorkflowGraph.tsx`：ReactFlow 渲染，node 类型按 kind 分派（AgentNode/ScriptNode/SetNode/GroupNode）
- `graph-layout.ts`：dagre TB 自动布局 + **回环边处理**（DFS 找 back edges，反向喂 dagre 让正向 DAG 排名正确，渲染保持原方向——直接抄 Conductor）
- 状态颜色：✓ done / ✽ running / ⏸ blocked / ! failed / ○ pending（NODE_STATUS_HEX）
- 实时更新：监听 store.nodes 状态变化，增量更新 ReactFlow node data（不全量重建）

### 5.5 tape replay UI（单路径 fold + 增量 apply）

- `ReplayBar.tsx`：底部时间轴滑块（Event N/M）+ 播放/暂停 + 速度（1×/5×/10×）
- `use-replay.ts`：定时器按事件时间戳间隔推进
- `setReplayPosition(pos)`：**增量 apply**（前进 apply N..M / 后退 rollback 或 checkpoint），不全量重置（避免 Conductor 卡顿缺点）
- **切换历史 run**：Sidebar 选历史 run → `GET /api/runs/<id>` 全量事件 → replay 模式

### 5.6 Gate 弹窗（gate UX 主战场）

```typescript
// components/gate/GateDialog.tsx
function GateDialog({ gate }) {
  if (gate.source === "tool_permission") {
    return <PermissionGate gate={gate} />;  // 工具+参数+批准/拒绝/编辑
  }
  return <AskGate gate={gate} />;  // 问题+选项/输入框
}
```
- 收到 `human_decision_requested` → 弹窗
- 用户答 → POST /gate/respond → handler.resolve
- 收到 `human_decision_resolved`（别壳先答）→ 自动关闭 + 显示「已被 [source] 答」

### 5.7 render_chart（独立 feature）

```typescript
// components/chart/ChartRenderer.tsx
function ChartRenderer() {
  const customEvents = useStore(s => s.events.filter(e => e.type === "custom" && e.data.kind === "chart"));
  return customEvents.map(ev => <Recharts spec={ev.data.spec} />);
}
```
- 订阅 `custom` 事件，按 `data.kind` 分发（chart → recharts / table → 表格 / image → img）
- `render_chart` MCP 工具本身在 phase 10；phase 9 **只做前端渲染**
- phase 9 验收：能渲染一个测试用的 `custom(chart)` 事件

---

## 6. Live / Replay 模式切换

```typescript
// App.tsx
function App() {
  const isReplay = useIsReplayMode();  // GET /api/replay/info 探活
  return isReplay ? <ReplayMode /> : <LiveMode />;
}
function LiveMode() { useWebSocket(); return <Dashboard />; }
function ReplayMode() { useReplay(); return <Dashboard />; }
```
- **Live**：WS 实时推 + `/api/state` 全量
- **Replay**：只读 server（无 WS），`/api/runs/<id>` 全量 + 定时器推进
- 两者共用同一 `<Dashboard />` + 同一 handler 表（单路径）

> **改进点**（vs Conductor）：Conductor 在挂载时永久二选一。Orca 允许 live run 跑完后切到 replay（看自己的历史），通过 `/api/runs/<current_id>` 复用 replay 路径。

---

## 7. 验收标准

### 7.0 验收总则（5 条铁律）
1. **Tape 唯一真相**：所有 UI 状态是 tape fold 派生物（grep 前端无自己存业务状态）。
2. **前端无业务真相**：只有 handler 表 + UI 交互态。
3. **重连全量重放**：WS 断重连，`/api/state` replay，状态一致（有测试）。
4. **gate 写 tape**：requested/resolved 都在 tape。
5. **WS 单通道**：grep 后端无第二个 WS 端点。

### 7.1 后端
- [ ] FastAPI app + lifespan 启动 broadcaster
- [ ] `/api/state` 返回全量事件
- [ ] `/api/runs` + `/api/runs/<id>` 历史 run
- [ ] `/api/run` 启动新 run
- [ ] `/ws` 单通道 + 反向 gate_response
- [ ] sync emit + Queue 桥 + broadcaster（不阻塞引擎）
- [ ] 单进程 uvicorn（同引擎事件循环）

### 7.2 前端 store
- [ ] Zustand 单 store + eventHandlers 表
- [ ] processEvent / replayState 统一 fold
- [ ] handler 覆盖所有 EventType
- [ ] live 和 replay 共用 handler（单路径）

### 7.3 WebSocket
- [ ] use-websocket：连接 + 重连（指数退避）+ 全量重拉
- [ ] 事件正确推送到 store

### 7.4 DAG 可视化
- [ ] ReactFlow 渲染所有 node（按 kind）
- [ ] dagre 自动布局
- [ ] **回环边正确处理**（不乱翻边）
- [ ] 状态颜色实时更新（增量，不全量重建）
- [ ] parallel 组渲染（父+子+进度）

### 7.5 tape replay UI
- [ ] ReplayBar 时间轴滑块 + 播放/速度
- [ ] 拖滑块 → DAG 回到该事件时状态
- [ ] **增量 apply**（不全量重置，长 workflow 不卡）
- [ ] 切换历史 run → replay 该 run
- [ ] **live 和 replay 状态一致**（同一 handler 表，有断言）

### 7.6 Gate 弹窗
- [ ] human_decision_requested → 弹窗
- [ ] tool_permission / agent_ask 两种渲染
- [ ] 用户答 → POST /gate/respond
- [ ] human_decision_resolved（别壳先答）→ 自动关闭

### 7.7 render_chart
- [ ] 订阅 custom(chart) 事件 → recharts 渲染
- [ ] 测试用 custom(chart) 事件能渲染

### 7.8 端到端（真 claude demo）
- [ ] 浏览器开 localhost:7428 → 启动 run → 看 DAG 进度
- [ ] gate 弹窗 → 答 → 继续
- [ ] run 完成 → 切 replay → 时间旅行
- [ ] render_chart 显示（demo 产出 custom 事件）

### 7.9 测试
- [ ] 后端 `tests/iface/web/test_server.py`：路由 + WS + broadcaster（TestClient）
- [ ] 前端：store handler 单元测试（vitest）
- [ ] 真集成 `@pytest.mark.integration`：浏览器跑 demo（playwright 可选）

---

## 8. 给后续阶段的契约

| 后续 | phase 9 提供 |
|---|---|
| phase 10 mcp | Web 壳是 gate 主交互面；MCP 壳的 gate 走 Web（用户开浏览器）|
| render_chart 工具 | phase 9 前端渲染就位；phase 10 补 MCP 工具让 claude 能调 |

---

## 9. 不做的事

- ❌ **render_chart MCP 工具**（让 claude 能调）—— phase 10（phase 9 只做前端渲染）
- ❌ **MCP 壳** —— phase 10
- ❌ **多用户认证**（本地工具，无 auth）—— 后续
- ❌ **真三通道竞速端到端**（CLI+Web 同时跑）—— 集成测试覆盖即可
- ❌ **复杂图表**（只做 recharts 基础图）—— 后续按需

---

## 10. 关键决策备忘（防 drift）

1. **技术栈抄 Conductor 实际栈**：FastAPI + React + Vite + ReactFlow + dagre + Zustand + Tailwind + recharts
2. **Tape 唯一真相源**：前端无业务真相，UI 只是真相推送（5 条铁律 §2.1）
3. **WS 单通道**（反双 WS）：sync emit + Queue 桥 + broadcaster
4. **重连全量重放**（Conductor 验证最简单正确）
5. **tape replay 单路径**（同一 apply_event，反双路径）
6. **增量 apply**（反 Conductor 全量重放卡顿）
7. **回环边处理**（抄 Conductor graph-layout.ts）
8. **单进程 uvicorn**（同引擎事件循环，零 IPC）
9. **render_chart 独立 feature**（前端渲染 phase 9，MCP 工具 phase 10）
10. **不用 Go/Rust**（Python 足够，全栈 Python + React 前端）
11. **依赖单向**：iface/web → run + gates + events + schema，不被 import
