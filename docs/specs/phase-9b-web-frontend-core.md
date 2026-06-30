# 阶段 9b SPEC —— iface/web 前端骨架（路由导航栈 + Zustand 单 store + 懒加载 + WS hook）

> **状态**：最终版（待分发实现）
> **依据**：[phase-9a-web-backend.md](phase-9a-web-backend.md)（API 契约）· [shells-design-draft.md](shells-design-draft.md) §4 · Conductor workflow-store.ts 调研
> **范围**：React SPA 骨架——路由导航栈（后退语义）+ Zustand 单 store（事件溯源 fold）+ 懒加载（按需加载 run）+ WS hook（按需订阅 + 重连全量重拉）
> **前置**：phase 9a（后端 API + WS）实现完成
> **commit 规范**：`feat(web):` 前缀，独立分支

---

## 0. 阶段目标 + 铁律

phase 9b 回答：**「前端骨架怎么搭，才能懒加载、后退正确、状态从唯一真相派生？」**

### 0.1 六条铁律（违反即返工）
1. **懒加载**：Runs 列表只加载元数据；点开 run 才 `GET /api/runs/<id>/events`；**切走 run 卸载其事件**（store 清除，不累积）。
2. **前端无业务真相**：store = tape 的 fold 派生物 + 少量 UI 交互态（selectedNode/replayPosition）。刷新页面 = 重新拉 = 状态一致。
3. **导航栈 = URL 路由**：用 react-router，后退 = 浏览器原生后退（`history.back()`），URL 驱动，绝不回错页（反 AgentHarness「后退回主页」）。
4. **单 store + 单 fold**：Zustand 一个 store + eventHandlers 表，live 和 replay 共用（反 AgentHarness 多 store + 多 sidecar）。
5. **WS 按需订阅 + 重连全量重拉**：subscribe(run_id)，断了重连先 `GET /api/runs/<id>/events` 全量 replay。
6. **依赖单向**：前端只调后端 API/WS，不含任何编排/gate 决策逻辑（纯渲染 + forward 输入）。

### 0.2 反模式（来自 AgentHarness，必须避免）
- ❌ 一次加载所有 run 全量数据（懒加载红线）
- ❌ 多 store（run/event/message 各一份真相）
- ❌ 后退回主页（无 URL 路由栈）
- ❌ 状态散落各 store 不一致（非幂等 reducer）
- ❌ live 和 replay 两套 fold

---

## 1. 技术栈

- **React 19 + Vite + TypeScript**（Conductor 实际栈）
- **react-router v6**（URL 路由 + browser history，后退语义）
- **Zustand**（单 store，事件溯源 handler 表）
- **Tailwind CSS v4**（样式，主题感知）
- **react-resizable-panels**（可调布局，Conductor 用）

---

## 2. 路由导航栈（后退语义，生产级）

### 2.1 URL 结构

```
/                          → Runs 列表（主页，只元数据）
/runs/new                  → 新建 Run 表单
/runs/:runId               → Run 详情（live 或 replay，懒加载事件）
/runs/:runId?tab=dag|log|output|yaml   → Run 详情某 tab
```

### 2.2 后退语义（反 AgentHarness）

**用 react-router 的 browser history**，URL 是页面状态的唯一来源：

```typescript
// 每次导航 push 新 URL（不 replace）
navigate(`/runs/${runId}`);       // 进入 run 详情
// 后退 = 浏览器后退按钮 / history.back()
// 从 /runs/A 后退 → 回到上一个页面（可能是 /runs 列表或 /runs/B）
```

**保证**：
- 后退**永远**回到上一个真实页面，不是「回主页」（URL 栈驱动）。
- URL 可分享/收藏（`/runs/nas-a3f2b1` 直接打开）。
- 刷新不丢状态（URL 重建页面 + 重新拉数据）。
- 浏览器前进/后退按钮可用。

### 2.3 页面卸载干净（配合懒加载）

```typescript
// Run 详情页 unmount 时清除该 run 的事件（懒加载红线）
function RunDetailPage() {
  const { runId } = useParams();
  useEffect(() => {
    store.loadRun(runId);        // 懒加载：拉该 run 事件
    return () => store.unloadRun(runId);  // 切走 → 卸载事件（不累积）
  }, [runId]);
}
```

---

## 3. Zustand 单 store（事件溯源 fold）

### 3.1 设计（抄 Conductor，单 store + handler 表）

```typescript
// stores/workflow-store.ts
interface WorkflowState {
  // === 业务真相派生物（从 events fold）===
  events: WorkflowEvent[];            // 当前 run 的事件缓存（懒加载填，切走清）
  nodes: Record<string, NodeState>;   // 派生：节点状态
  gate: GateState | null;             // 派生：当前 gate
  workflowName: string;
  status: "idle" | "running" | "completed" | "failed";

  // === UI 交互态（非业务真相）===
  selectedNode: string | null;
  replayMode: boolean;
  replayPosition: number;
  activeRunId: string | null;

  // === actions ===
  loadRun: (runId: string) => Promise<void>;     // 懒加载
  unloadRun: (runId: string) => void;             // 卸载
  processEvent: (event: WorkflowEvent) => void;   // 统一 fold 入口
  replayState: (events: WorkflowEvent[]) => void; // 全量 replay
}

// 单一 handler 表（live 和 replay 共用，反双路径）
const eventHandlers: Record<string, (state, data, timestamp) => void> = {
  workflow_started: (s, d) => { s.status = "running"; s.workflowName = d.workflow_name; },
  node_started: (s, d) => { s.nodes[d.node] = { status: "running" }; },
  node_completed: (s, d) => { s.nodes[d.node] = { status: "done", output: d.output }; },
  node_failed: (s, d) => { s.nodes[d.node] = { status: "failed" }; },
  human_decision_requested: (s, d) => { s.gate = { ...d }; },
  human_decision_resolved: (s, d) => { s.gate = null; },
  // ... 覆盖所有 EventType
};

const processEvent = (state, event) => {
  const handler = eventHandlers[event.type];
  handler?.(state, event.data, event.timestamp);
  state.events.push(event);  // 缓存
};
```

### 3.2 关键约束

1. **handler 表是唯一状态计算路径**。live 和 replay 都调 `processEvent`，状态必然一致（反 AgentHarness 双路径）。
2. **events 是缓存不是真相**：真相在后端 tape，前端 events 只是当前 run 的懒加载缓存，切走就清。
3. **fold 幂等**：同一事件应用 N 次 = 1 次（节点状态 last-writer-wins，不拼接）。
4. **loadRun/unloadRun**：懒加载的加载/卸载点。

---

## 4. 懒加载 hook

### 4.1 useRunEvents（懒加载 + 卸载）

```typescript
// hooks/use-run-events.ts
function useRunEvents(runId: string | undefined) {
  const loadRun = useStore(s => s.loadRun);
  const unloadRun = useStore(s => s.unloadRun);
  useEffect(() => {
    if (!runId) return;
    loadRun(runId);            // GET /api/runs/<id>/events → replayState
    return () => unloadRun(runId);  // 切走 → store.events 清空
  }, [runId]);
}

// store.loadRun 实现
const loadRun = async (runId) => {
  const resp = await fetch(`/api/runs/${runId}/events`);
  const events = await resp.json();
  set({ activeRunId: runId, events: [], nodes: {}, gate: null });
  events.forEach(e => get().processEvent(e));  // 全量 fold
};
const unloadRun = (runId) => {
  set({ activeRunId: null, events: [], nodes: {}, gate: null });  // 清，不累积
};
```

### 4.2 useRunsList（元数据列表，懒加载）

```typescript
// hooks/use-runs-list.ts
function useRunsList() {
  const [metas, setMetas] = useState<RunMeta[]>([]);
  const refresh = () => fetch('/api/runs').then(r => r.json()).then(setMetas);
  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 2000);  // 轮询元数据（轻量，无事件）
    return () => clearInterval(interval);
  }, []);
  return metas;
}
```

**关键**：Runs 列表轮询 `/api/runs`（元数据，无事件），不轮询事件。事件只在点开 run 时拉。

---

## 5. WebSocket hook（按需订阅 + 重连全量重拉）

### 5.1 useWebSocket（抄 Conductor + 改进按需订阅）

```typescript
// hooks/use-websocket.ts
function useWebSocket(runId: string | undefined) {
  const processEvent = useStore(s => s.processEvent);
  const replayState = useStore(s => s.replayState);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!runId) return;

    // 1. 先全量重拉（初始 + 重连都走）
    fetch(`/api/runs/${runId}/events`).then(r => r.json()).then(events => {
      if (events.length) replayState(events);
    });

    // 2. 再开 WS 按需订阅该 run
    const ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onopen = () => ws.send(JSON.stringify({ type: "subscribe", run_id: runId }));
    ws.onmessage = e => {
      const event = JSON.parse(e.data);
      if (event.run_id === runId) processEvent(event);
    };
    ws.onclose = () => scheduleReconnect();  // 指数退避，重连又走全量重拉
    wsRef.current = ws;

    return () => ws.close();
  }, [runId]);

  const scheduleReconnect = () => {
    // 指数退避（1s/2s/4s...max 30s），重连后重新执行上面的全量重拉 + subscribe
  };
}
```

### 5.2 关键约束

1. **按需订阅**：WS `subscribe(run_id)`，只收该 run 事件（后端 9a 保证）。
2. **重连全量重拉**（Conductor 验证最简单正确）：断了先 `GET /api/runs/<id>/events` 全量 replay，再 subscribe。
3. **切换 run**：useEffect cleanup 关旧 WS + 开新 WS subscribe 新 run。

---

## 6. 页面组件（骨架，9c/9d 填充具体视图）

### 6.1 App（路由 + 布局壳）

```typescript
// App.tsx
function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Layout><RunsListPage /></Layout>} />
        <Route path="/runs/new" element={<Layout><NewRunPage /></Layout>} />
        <Route path="/runs/:runId" element={<Layout><RunDetailPage /></Layout>} />
      </Routes>
    </BrowserRouter>
  );
}

function Layout({ children }) {
  return (
    <div className="flex flex-col h-screen">
      <TopBar />
      <div className="flex flex-1">
        <RunsSidebar />   {/* 常驻左侧，元数据列表 */}
        <main className="flex-1">{children}</main>
      </div>
      <StatusBar />
    </div>
  );
}
```

### 6.2 三个页面（骨架）

- **RunsListPage**（`/`）：元数据列表表格 + 点击 → navigate(`/runs/<id>`)
- **NewRunPage**（`/runs/new`）：表单（yaml/inputs/task/max_iter）→ POST /api/run → navigate(`/runs/<new_id>`)
- **RunDetailPage**（`/runs/:runId`）：`useRunEvents(runId)` + `useWebSocket(runId)` + tab 切换（dag/log/output/yaml，9c/9d 实现 tab 内容）

### 6.3 RunsSidebar（常驻，元数据）

```typescript
function RunsSidebar() {
  const metas = useRunsList();  // 元数据轮询，无事件
  const navigate = useNavigate();
  return (
    <aside className="w-64">
      <button onClick={() => navigate('/runs/new')}>+ New Run</button>
      {metas.map(m => (
        <div key={m.run_id} onClick={() => navigate(`/runs/${m.run_id}`)}>
          <StatusIcon status={m.status} /> {m.run_id.slice(0,8)}
          <ProgressBar progress={m.progress} />
        </div>
      ))}
    </aside>
  );
}
```

---

## 7. 验收标准

### 7.1 路由 + 后退语义
- [ ] `/` 显示 Runs 列表（元数据）
- [ ] 点 run → navigate `/runs/<id>`，显示详情
- [ ] **后退 = 浏览器后退**，回到上一个页面（不是主页，有 playwright 断言）
- [ ] `/runs/<id>` 可直接访问（刷新不丢）
- [ ] URL 可分享

### 7.2 懒加载
- [ ] Runs 列表加载**只调 `/api/runs`**（元数据），**不调 `/events`**（playwright 抓网络请求断言）
- [ ] 点开 run 才调 `/api/runs/<id>/events`
- [ ] 切走 run → store.events 清空（断言 `getActiveRunId() === null`）

### 7.3 Zustand 单 store
- [ ] 一个 store（grep 无第二个 create()）
- [ ] eventHandlers 覆盖所有 EventType
- [ ] processEvent / replayState 共用 handler（反双路径）
- [ ] fold 幂等（同事件应用两次状态一致）

### 7.4 WS hook
- [ ] subscribe(run_id) 后收到该 run 事件
- [ ] 切 run → 旧 WS 关 + 新 WS subscribe
- [ ] 重连：WS 断 → 全量重拉 + 重新 subscribe（playwright 模拟断连）

### 7.5 测试（vitest 单元 + playwright 端到端）
- [ ] `frontend/test/store.test.ts`：handler 表 + fold 幂等 + loadRun/unloadRun
- [ ] `frontend/test/hooks.test.tsx`：useRunEvents（懒加载/卸载）+ useRunsList
- [ ] playwright：路由 + 后退 + 懒加载（抓网络请求）+ WS

### 7.6 playwright 验收（AI 自动测，关键）
> 安装 `playwright-mcp`（`github.com/microsoft/playwright-mcp`）+ playwright python。
- [ ] **后退语义**：playwright 导航 A → B → `page.goBack()` → 断言回到 A（不是主页）
- [ ] **懒加载网络**：playwright `page.on('request')` 抓 `/api/runs` 被调，但首页加载**不调** `/events`（断言）
- [ ] **点开才加载**：点 run 后断言 `/events` 被调
- [ ] **URL 可访问**：`page.goto('/runs/<id>')` 直接打开
- [ ] **WS 工作**：playwright evaluate WS 客户端，subscribe 后收到事件

---

## 8. 给后续阶段的契约

| 后续 | phase 9b 提供 |
|---|---|
| phase 9c dag-replay | RunDetailPage 容器 + store（nodes/gate）+ useRunEvents/useWebSocket |
| phase 9d gate-chart | store.gate（gate 弹窗读）+ events filter custom（chart 渲染读）|

---

## 9. 不做的事

- ❌ **DAG 可视化 / replay UI**（RunDetailPage 的 dag tab 内容）—— phase 9c
- ❌ **gate 弹窗 / chart 渲染**—— phase 9d
- ❌ **后端 API/WS**（9a 已做）
- ❌ **编排/gate 决策**——纯渲染

---

## 10. 关键决策备忘（防 drift）

1. **react-router URL 路由**：后退 = 浏览器原生，绝不回错页
2. **懒加载**：列表只元数据，点开才 events，切走卸载
3. **Zustand 单 store + handler 表**：唯一 fold，live/replay 共用
4. **WS 按需订阅 + 重连全量重拉**
5. **events 是缓存非真相**：真相在后端 tape
6. **前端纯渲染**：不含编排/gate 逻辑
7. **commit `feat(web):` 前缀 + 独立分支**
