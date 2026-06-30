# 阶段 9a SPEC —— iface/web 后端（FastAPI + WebSocket + RunManager 真并发 + 懒加载 API）

> **状态**：最终版（待分发实现）
> **依据**：[shells-design-draft.md](shells-design-draft.md) §4 · [phase-5-run.md](phase-5-run.md) §4 · [phase-6-gates.md](phase-6-gates.md) §3 §4 · Conductor web/server.py 调研
> **范围**：FastAPI 后端（单进程同引擎 asyncio）+ WebSocket 单通道 + RunManager（多 run 真并发）+ 懒加载 REST API
> **前置**：phase 5（Orchestrator + EventBus）+ phase 6（HumanGateHandler）实现完成
> **commit 规范**：web 相关 commit 须 `feat(web):` 前缀，**独立分支开发**（web 是纯渲染层，与引擎改动分离）

---

## 0. 阶段目标 + 铁律

phase 9a 回答：**「后端怎么托管多个并发 run、把事件流按需推给前端、让前端懒加载？」**

### 0.1 五条铁律（违反即返工）
1. **后端唯一真相源**：每个 run 的 tape 是唯一真相。后端**不维护并行内存事件 list**（反 Conductor `_event_history` list 与 tape 并存）。需要全量事件时 `tape.replay()`。
2. **懒加载契约**：`GET /api/runs` 只返回**元数据**（不含事件）；事件只在 `GET /api/runs/<id>/events` 按需返回。前端持有一个 run 的事件，不累积。
3. **WS 单通道 + 按需订阅**：所有事件/gate 走一条 WS。前端订阅某 run 时，后端只推**该 run** 的事件（不是所有 run 洪流）。
4. **真并发**：RunManager 用 `asyncio.gather` 真并发跑多个 run；`max_concurrent` 配置（默认 3）超过排队（排队≠单活跃，本质并发）。
5. **依赖单向**：iface/web → run + gates + events + schema + compile，**不被任何模块 import**，不含编排/gate 决策逻辑。

### 0.2 反模式（来自 AgentHarness 教训，必须避免）
- ❌ 后端内存 list 与 tape 并存（Conductor `_event_history` + `EventLogSubscriber` 两份）
- ❌ 一次返回所有 run 的全量事件（懒加载红线）
- ❌ WS 推所有 run 的事件洪流
- ❌ 后端做编排/gate 决策（那是 run/gates 的职责，web 只是转发）

---

## 1. 架构设计

### 1.1 文件结构

```
orca/iface/web/
├── __init__.py              # 导出 create_app, run_server, RunManager
├── server.py                # create_app(app factory) + lifespan（启动 broadcaster）+ 路由注册
├── run_manager.py           # RunManager：多 run 真并发 + max_concurrent 排队 + run 元数据缓存
├── ws_handler.py            # WebSocket 单通道 + sync emit→Queue 桥 + 按需订阅 broadcaster
├── routes/
│   ├── __init__.py
│   ├── runs.py              # GET /api/runs（元数据列表）+ GET /api/runs/<id>/events（懒加载）
│   ├── run.py               # POST /api/run（启动新 run）
│   └── gate.py              # POST /gate（hook 桥，复用 phase 6）+ POST /gate/respond（壳 resolve）
└── static/                  # Vite 构建产物（phase 9b 前端 build → static/）
```

### 1.2 单进程同引擎（抄 Conductor D4）

orchestrator 和 uvicorn 在**同一个 asyncio 事件循环**：

```python
# 启动入口（CLI orca serve）
async def serve(host, port, max_concurrent):
    manager = RunManager(max_concurrent)
    app = create_app(manager)
    config = uvicorn.Config(app, host=host, port=port)
    server = uvicorn.Server(config)
    await asyncio.gather(
        server.serve(),
        manager._scheduler(),  # 排队调度（若有）
    )
```

**理由**：零 IPC、零序列化开销，引擎和 WS 共享事件循环（Conductor 设计决策 D4 验证）。

---

## 2. RunManager（多 run 真并发）

### 2.1 职责

```python
# orca/iface/web/run_manager.py
class RunManager:
    """托管多个并发 run。每个 run 一个 orchestrator task + 独立 tape。
    真并发（asyncio.gather），max_concurrent 超过排队。"""

    def __init__(self, max_concurrent: int = 3):
        self._max_concurrent = max_concurrent
        self._runs: dict[str, RunHandle] = {}   # run_id → RunHandle
        self._sem = asyncio.Semaphore(max_concurrent)
        self._lock = asyncio.Lock()

    async def start_run(self, yaml_path: str, inputs: dict, task: str | None,
                        max_iter: int | None) -> str:
        """启动一个 run，返回 run_id。不阻塞（后台 task）。"""
        run_id = gen_run_id(...)
        wf = load_workflow(yaml_path)
        tape = Tape(f"runs/{run_id}/events.jsonl", run_id)
        bus = EventBus(tape)
        gate_handler = HumanGateHandler(bus)
        handle = RunHandle(run_id, wf, bus, tape, gate_handler, status="queued")
        async with self._lock:
            self._runs[run_id] = handle
        asyncio.create_task(self._run_with_sem(handle, inputs, task, max_iter))
        return run_id

    async def _run_with_sem(self, handle, inputs, task, max_iter):
        async with self._sem:  # 真并发 + max_concurrent 排队
            handle.status = "running"
            orch = Orchestrator(handle.wf, handle.bus, inputs, task, max_iter, handle.gate_handler)
            try:
                await orch.run()
                handle.status = "completed"
            except Exception as e:
                handle.status = "failed"
                handle.error = str(e)

    def list_runs(self) -> list[RunMeta]:
        """返回所有 run 的元数据（不含事件，懒加载）。从 RunHandle 算摘要。"""
        # run_id, workflow_name, status, progress(done/total), cost, elapsed, error

    def get_run_events(self, run_id: str) -> list[Event]:
        """懒加载：返回某 run 的全量事件（tape.replay）。"""

    def get_handle(self, run_id: str) -> RunHandle | None: ...
```

### 2.2 RunHandle / RunMeta

```python
@dataclass
class RunHandle:
    run_id: str
    wf: Workflow
    bus: EventBus
    tape: Tape
    gate_handler: HumanGateHandler
    status: Literal["queued", "running", "completed", "failed"]
    error: str | None = None
    started_at: float = field(default_factory=time.time)

@dataclass
class RunMeta:
    """懒加载列表项：只有元数据，不含事件。"""
    run_id: str
    workflow_name: str
    status: str
    progress: str         # "3/7"
    cost: float
    elapsed: float
    error: str | None
```

### 2.3 关键约束

1. **每个 run 独立 bus + tape + gate_handler**（隔离，多 run 不串事件/gate）。
2. **真并发**：`_sem` 限制同时跑的 run 数（默认 3），超过的 queued。`asyncio.gather` 在 sem 内并发。
3. **status 实时**：RunHandle.status 在 start/complete/fail 时更新，`list_runs` 反映最新。
4. **元数据从 tape 派生**：progress/cost 不单独存，从 `replay_state(tape)` 算（保证与真相源一致）。

---

## 3. REST API（懒加载）

### 3.1 路由

| 方法 | 路径 | 用途 | 返回 |
|---|---|---|---|
| GET | `/api/runs` | run 列表（**元数据，不含事件**）| `list[RunMeta]` |
| GET | `/api/runs/<run_id>/events` | 某 run 全量事件（**懒加载**）| `list[Event]` |
| GET | `/api/runs/<run_id>` | 某 run 元数据 + 当前状态 | `RunMeta + RunState snapshot` |
| POST | `/api/run` | 启动新 run | `{run_id, status: "queued"}` |
| POST | `/gate` | hook 桥（**复用 phase 6**）| `{decision, resolved_by}` |
| POST | `/gate/respond` | 壳 resolve gate | `{ok, status}` |
| GET | `/` + `/assets/*` | 静态前端（phase 9b 构建产物）| HTML/JS |

### 3.2 懒加载契约（红线）

- `GET /api/runs` **绝不**返回事件。只 `RunMeta`（run_id/status/progress/cost/elapsed/error）。
- 事件只在 `GET /api/runs/<id>/events` 返回，前端**点开某 run 才调**。
- `GET /api/runs/<id>` 返回元数据 + 当前 RunState 快照（`replay_state(tape)`），**不返回全量事件**（除非显式 events 端点）。

### 3.3 启动 run

```python
@router.post("/api/run")
async def start_run(body: RunRequest, manager: RunManager):
    run_id = await manager.start_run(body.yaml_path, body.inputs, body.task, body.max_iter)
    return {"run_id": run_id, "status": "queued"}
```

---

## 4. WebSocket（单通道 + 按需订阅）

### 4.1 设计

**抄 Conductor：sync emit + asyncio.Queue 桥 + 独立 broadcaster task**。但 Orca 改进：**按需订阅某 run**（不推所有 run 洪流）。

```python
# orca/iface/web/ws_handler.py
class WebServer:
    def __init__(self, manager: RunManager):
        self._manager = manager
        self._subs: dict[WebSocket, RunSubscription] = {}  # WS → 订阅的 run

    async def ws_endpoint(self, ws: WebSocket):
        await ws.accept()
        active_run_id: str | None = None
        try:
            while True:
                msg = await ws.receive_json()
                if msg["type"] == "subscribe":
                    # 前端切到某 run → 订阅该 run 的 bus
                    active_run_id = msg["run_id"]
                    handle = self._manager.get_handle(active_run_id)
                    if handle:
                        self._subs[ws] = RunSubscription(handle.bus.subscribe())
                        asyncio.create_task(self._pump(ws, self._subs[ws], active_run_id))
                elif msg["type"] == "gate_response":
                    # 反向通道：壳 resolve
                    handle = self._manager.get_handle(active_run_id)
                    handle.gate_handler.resolve(msg["gate_id"], msg["answer"], "web")
        except WebSocketDisconnect:
            ...

    async def _pump(self, ws, sub, run_id):
        """把某 run 的 bus 事件推给该 WS。"""
        async for event in sub.events():
            await ws.send_json({**event.model_dump(), "run_id": run_id})
```

### 4.2 关键约束

1. **单通道**：所有事件/gate/决策走一条 `/ws`。
2. **按需订阅**：前端 `subscribe(run_id)` 后才推该 run 事件。切 run → 旧订阅 cancel + 新订阅。
3. **反向通道**：同 WS 收 `gate_response`（壳 resolve）。
4. **不推所有 run 洪流**：每个 WS 只收它订阅的 run 的事件。
5. **重连全量重拉**（phase 9b 前端职责）：WS 断了，前端 `GET /api/runs/<id>/events` 全量 replay 再开 WS。

---

## 5. 复用 phase 6 gate 端点

`POST /gate`（hook 桥）和 `POST /gate/respond`（壳 resolve）**直接复用 phase 6 的 `register_gate_routes`**（phase-6 SPEC §3.5）。phase 9a 只是把它们挂到 FastAPI app：

```python
# orca/iface/web/server.py
def create_app(manager: RunManager) -> FastAPI:
    app = FastAPI(lifespan=...)
    app.state.manager = manager
    app.include_router(runs.router)
    app.include_router(run.router)
    from orca.gates import register_gate_routes
    register_gate_routes(app, manager._current_gate_handler, manager._registry)
    # WS
    web_server = WebServer(manager)
    app.websocket("/ws")(web_server.ws_endpoint)
    return app
```

> **注**：phase 6 的 `/gate` 端点是「当前 run」语义（hook 是单 run 场景）。phase 9a 多 run 时，`/gate` 需要从 hook payload 的 session_id 经 registry 查 run_id（phase 6 §6 已设计 session 映射）。多 run 的 gate 路由由 manager 分发到对应 handle.gate_handler。

---

## 6. 验收标准

### 6.1 结构
- [ ] `orca/iface/web/` 下 server/run_manager/ws_handler/routes × 4
- [ ] `from orca.iface.web import create_app, run_server, RunManager`

### 6.2 RunManager（真并发 + 懒加载）
- [ ] `start_run` 返回 run_id，后台 task 跑（不阻塞）
- [ ] **真并发**：同时 start 3 个 run，全跑（asyncio.gather，有测试断言并发）
- [ ] **max_concurrent 排队**：start 5 个，max_concurrent=3，2 个 queued 等前面完成
- [ ] `list_runs` 返回元数据（**不含事件**，断言返回体无 events 字段）
- [ ] status 实时更新（queued→running→completed/failed）
- [ ] **元数据从 tape 派生**：progress/cost == `replay_state(tape)` 算的值（断言一致）

### 6.3 REST API
- [ ] `GET /api/runs` 返回 `list[RunMeta]`，**无事件**（断言 body 不含 event 字段）
- [ ] `GET /api/runs/<id>/events` 返回该 run 全量事件（懒加载）
- [ ] `GET /api/runs/<id>` 返回元数据 + RunState 快照
- [ ] `POST /api/run` 启动，返回 run_id
- [ ] 启动不存在的 yaml → 400 + 错误信息

### 6.4 WebSocket
- [ ] 单通道 `/ws`
- [ ] `subscribe(run_id)` 后推该 run 事件
- [ ] 切 run → 旧订阅停 + 新订阅起
- [ ] **不推未订阅 run 的事件**（断言：subscribe A 后，B 的 emit 收不到）
- [ ] 反向 `gate_response` → handler.resolve 被调
- [ ] 连接断开 → 清理订阅

### 6.5 单进程同引擎
- [ ] orchestrator 和 uvicorn 同事件循环（lifespan 启动 manager task）

### 6.6 唯一真相源
- [ ] 后端**无并行内存事件 list**（`grep "events.*=.*\[\]" orca/iface/web/` 无持久化事件 list，只有 tape.replay 临时返回）
- [ ] 全量事件来源唯一：`tape.replay()`

### 6.7 测试（pytest + httpx AsyncClient + 真 tape）
- [ ] `tests/iface/web/test_run_manager.py`：真并发 / max_concurrent / 懒加载 / status 实时
- [ ] `tests/iface/web/test_routes.py`：REST 路由 + 懒加载契约（断言无事件泄露）
- [ ] `tests/iface/web/test_ws.py`：WS 订阅 / 切换 / gate_response / 不推未订阅
- [ ] 集成：`@pytest.mark.integration` 启动真 server + 跑 demo workflow（mock claude 或真 claude）

### 6.8 playwright 验收（端到端，AI 自动测）
> **phase 9a 的 playwright 验收**：后端没有 UI，但 API 可用 playwright 的 `page.request`（fetch）或 `evaluate` 测。phase 9b 前端就位后，playwright 主要验 UI。phase 9a 验收：
- [ ] playwright 脚本：`fetch('/api/runs')` 返回非空元数据列表（启动 demo run 后）
- [ ] playwright 脚本：`fetch('/api/runs/<id>/events')` 返回事件数组
- [ ] playwright 脚本：WS 连接 + subscribe + 收到事件（用 `evaluate` 跑 WS 客户端）
- [ ] 这些断言写进 `tests/iface/web/test_playwright.py`，`@pytest.mark.integration`

---

## 7. 给后续阶段的契约

| 后续 | phase 9a 提供 |
|---|---|
| phase 9b 前端 | `GET /api/runs`（元数据列表）+ `GET /api/runs/<id>/events`（懒加载）+ `/ws`（按需订阅）+ `POST /api/run` |
| phase 9c dag-replay | `GET /api/runs/<id>/events`（replay 数据源，全量事件）|
| phase 9d gate-chart | `/gate` + `/gate/respond`（gate）+ tape 的 custom 事件（chart 渲染源）|

---

## 8. 不做的事

- ❌ **前端**（任何 HTML/JS/React）—— phase 9b
- ❌ **DAG 渲染 / replay UI** —— phase 9c
- ❌ **gate 弹窗 / chart 渲染** —— phase 9d
- ❌ **编排/gate 决策逻辑**（那是 run/gates 职责）—— web 只转发
- ❌ **持久化事件内存 list**（懒加载 + tape 唯一）—— 红线
- ❌ **多用户认证**（本地工具）—— 后续

---

## 9. 关键决策备忘（防 drift）

1. **单进程同引擎**（uvicorn + orchestrator 同 asyncio 循环，抄 Conductor D4）
2. **真并发 + max_concurrent 排队**（不是单活跃）
3. **懒加载**：`/api/runs` 只元数据，事件 `/api/runs/<id>/events` 按需
4. **WS 单通道 + 按需订阅**（subscribe(run_id)，不推所有 run 洪流）
5. **每个 run 独立 bus+tape+gate_handler**（隔离）
6. **元数据从 tape 派生**（progress/cost == replay_state(tape)，不另存）
7. **后端无并行内存事件 list**（唯一真相 = tape）
8. **复用 phase 6 gate 端点**（register_gate_routes）
9. **依赖单向**：iface/web → run+gates+events+schema+compile
10. **commit 独立 + feat(web): 前缀**（web 是纯渲染/转发层）
