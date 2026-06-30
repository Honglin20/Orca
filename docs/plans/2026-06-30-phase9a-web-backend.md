# 开发计划 —— 阶段 9a：iface/web 后端

> **状态**：待执行（**phase 5 + 6 实现完成后开工**）
> **SPEC**：[`docs/specs/phase-9a-web-backend.md`](../specs/phase-9a-web-backend.md)
> **前置**：phase 5（Orchestrator + EventBus）+ phase 6（HumanGateHandler + register_gate_routes）
> **commit 规范**：`feat(web):` 前缀，独立分支 `phase9-web`
> **测试原则**：pytest + httpx AsyncClient（单元/集成）+ playwright（端到端 API/WS）

---

## 0. 产出与执行顺序

```
orca/iface/web/
├── __init__.py          A4
├── server.py            A3（create_app + lifespan + 路由注册）
├── run_manager.py       A1（RunManager 真并发 + RunHandle + RunMeta）
├── ws_handler.py        A2（WS 单通道 + 按需订阅 + gate_response）
├── routes/
│   ├── __init__.py      A3
│   ├── runs.py          A3（懒加载 REST）
│   ├── run.py           A3
│   └── gate.py          A3（复用 phase 6）
└── static/              （phase 9b 前端构建产物）
+ tests/iface/web/ × 5
```

执行顺序：A1 RunManager → A2 WS → A3 server+routes → A4 导出+集成+playwright

---

## A1. RunManager（真并发 + 懒加载元数据）

### A1.1 `orca/iface/web/run_manager.py`
- `RunHandle`（run_id/wf/bus/tape/gate_handler/status/error/started_at）
- `RunMeta`（run_id/workflow_name/status/progress/cost/elapsed/error，**不含事件**）
- `RunManager(max_concurrent=3)`：
  - `start_run(yaml_path, inputs, task, max_iter) -> run_id`（后台 task，不阻塞）
  - `_run_with_sem(handle, ...)`：sem 内 Orchestrator.run()
  - `list_runs() -> list[RunMeta]`（元数据从 RunHandle + replay_state 算）
  - `get_run_events(run_id) -> list[Event]`（`tape.replay()`，懒加载）
  - `get_handle(run_id) -> RunHandle | None`

### A1.2 验收（A1）— `tests/iface/web/test_run_manager.py`
- [ ] `start_run` 返回 run_id，不阻塞（await 后 run 已在 queued/running）
- [ ] **真并发**：start 3 个慢 run（mock Orchestrator sleep），断言 3 个同时 running（`asyncio.gather` 并发）
- [ ] **max_concurrent 排队**：max_concurrent=2，start 4 个，断言同时 running ≤ 2，其余 queued
- [ ] `list_runs` 返回元数据，**断言无 events 字段**（`assert not hasattr(meta, "events")`）
- [ ] status 转换：queued → running → completed（mock run 成功）
- [ ] failed：mock run raise → status=failed + error 记录
- [ ] **元数据与 tape 一致**：`list_runs()[0].progress == replay_state(tape).node_status` 算的 done 数（断言）

### A1.3 测试骨架
```python
async def test_real_concurrency():
    manager = RunManager(max_concurrent=3)
    # 3 个 mock 慢 run
    with patch("orca.run.Orchestrator.run", new=async_sleep(0.1)):
        ids = await asyncio.gather(*[manager.start_run(...) for _ in range(3)])
    await asyncio.sleep(0.05)
    metas = manager.list_runs()
    running = [m for m in metas if m.status == "running"]
    assert len(running) == 3  # 真并发

async def test_list_runs_no_events():
    manager = RunManager()
    rid = await manager.start_run(...)
    await manager._wait_done(rid)
    metas = manager.list_runs()
    assert all("events" not in m.__dict__ for m in metas)  # 懒加载红线

async def test_metadata_from_tape():
    manager = RunManager()
    rid = await manager.start_run(...)
    await manager._wait_done(rid)
    meta = [m for m in manager.list_runs() if m.run_id == rid][0]
    handle = manager.get_handle(rid)
    state = replay_state(handle.tape)
    done = sum(1 for s in state.node_status.values() if s == "done")
    assert meta.progress.endswith(f"/{done}") or str(done) in meta.progress  # 与 tape 一致
```

---

## A2. WebSocket（单通道 + 按需订阅）

### A2.1 `orca/iface/web/ws_handler.py`
- `WebServer(manager)`：`_subs: dict[WebSocket, RunSubscription]`
- `ws_endpoint(ws)`：accept → 循环 receive_json：
  - `subscribe` → 订阅 handle.bus，create_task `_pump`
  - `unsubscribe` → cancel 旧 pump
  - `gate_response` → handle.gate_handler.resolve
- `_pump(ws, sub, run_id)`：bus 事件 → ws.send_json（带 run_id）
- 连接断开 → 清理订阅

### A2.2 验收（A2）— `tests/iface/web/test_ws.py`
- [ ] WS 连接 + `subscribe(A)` → A 的 emit 收到（带 run_id）
- [ ] **不推未订阅 run**：subscribe(A)，B 的 emit 收不到（断言）
- [ ] 切 run：subscribe(A) → unsubscribe → subscribe(B) → 只收 B
- [ ] `gate_response` → handler.resolve 被调（断言 resolve 返回 True）
- [ ] 连接断开 → 订阅清理（断言 _subs 清空）

### A2.3 测试骨架
```python
async def test_subscribe_only_pushes_that_run(manager):
    server = WebServer(manager)
    rid_a = await manager.start_run(...)  # 会 emit 事件
    rid_b = await manager.start_run(...)
    client = TestClient(server.app)  # 或 httpx websocket
    async with client.websocket_connect("/ws") as ws:
        await ws.send_json({"type": "subscribe", "run_id": rid_a})
        # 触发 B 的 emit
        manager.get_handle(rid_b).bus.emit("node_started", {...})
        # 只应收到 A 的事件
        msg = await asyncio.wait_for(ws.receive_json(), timeout=0.5)
        assert msg["run_id"] == rid_a
```

---

## A3. server + routes（懒加载 REST）

### A3.1 `orca/iface/web/server.py`
- `create_app(manager) -> FastAPI`：lifespan（无需额外 task，manager task 在 start_run 时起）+ 注册路由 + WS + 复用 phase 6 gate
- `run_server(manager, host, port)`：uvicorn 同事件循环

### A3.2 `routes/runs.py`
- `GET /api/runs` → `manager.list_runs()`（元数据列表）
- `GET /api/runs/<run_id>/events` → `manager.get_run_events()`（懒加载全量）
- `GET /api/runs/<run_id>` → `RunMeta + replay_state(tape)` 快照

### A3.3 `routes/run.py`
- `POST /api/run`（body: yaml_path, inputs, task, max_iter）→ `manager.start_run()` → `{run_id, status}`

### A3.4 `routes/gate.py`
- 复用 `orca.gates.register_gate_routes`（phase 6）
- 多 run 适配：从 hook session_id → run_id（registry）→ handle.gate_handler

### A3.5 验收（A3）— `tests/iface/web/test_routes.py`
- [ ] `GET /api/runs` 返回 `list[RunMeta]`，**断言 body 无 event 字段**（`assert "events" not in response.json()[0]`）
- [ ] `GET /api/runs/<id>/events` 返回事件数组（懒加载，run 完成后有事件）
- [ ] `GET /api/runs/<id>` 返回 meta + state 快照
- [ ] `POST /api/run` 返回 run_id + status=queued
- [ ] 不存在 run_id → 404
- [ ] 不存在 yaml_path → 400

### A3.6 测试骨架
```python
async def test_runs_list_no_events(client, manager):
    await manager.start_run("examples/demo_linear.yaml", {}, None, None)
    resp = await client.get("/api/runs")
    data = resp.json()
    assert resp.status_code == 200
    assert len(data) >= 1
    for item in data:
        assert "events" not in item  # 懒加载红线
        assert "run_id" in item and "status" in item
```

---

## A4. 导出 + 集成 + playwright 验收

### A4.1 `orca/iface/web/__init__.py` + `orca/iface/__init__.py`
- 导出 `create_app, run_server, RunManager, RunHandle, RunMeta`

### A4.2 pyproject.toml 依赖
- `fastapi, uvicorn, websockets`（新增）

### A4.3 集成测试 `tests/iface/web/test_integration.py`（@pytest.mark.integration）
- [ ] 启动真 server（uvicorn）+ start_run demo workflow（mock claude，避免 API key）
- [ ] 全流程：start → list 有该 run → events 端点有事件 → WS subscribe 收事件
- [ ] tape 完整性：events 端点返回 == tape.replay()

### A4.4 playwright 验收 `tests/iface/web/test_playwright.py`（@pytest.mark.integration）
> **安装 playwright-mcp**（`github.com/microsoft/playwright-mcp`）供 AI 自动测。本测试用 playwright python 库驱动浏览器跑 API/WS 断言。
- [ ] playwright 启动浏览器 → `page.request.fetch('/api/runs')` 返回 200 + 非空元数据列表
- [ ] `page.request.fetch('/api/runs/<id>/events')` 返回事件数组
- [ ] `page.evaluate(WS 客户端代码)` → subscribe + 收到事件（断言收到带 run_id 的事件）
- [ ] **懒加载断言**：playwright 抓 `/api/runs` 响应，断言无 event 字段

### A4.5 测试骨架
```python
@pytest.mark.integration
async def test_playwright_runs_api(live_server):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(live_server.url)
        resp = await page.request.fetch(f"{live_server.url}/api/runs")
        data = await resp.json()
        assert resp.status == 200
        for item in data:
            assert "events" not in item  # playwright 验懒加载
        await browser.close()
```

---

## 5. 总验收（Definition of Done）

### 5.1 单元/集成测试（CI 跑，mock claude）
- [ ] A1 RunManager（真并发/max_concurrent/懒加载/元数据一致）
- [ ] A2 WS（按需订阅/不推未订阅/gate_response）
- [ ] A3 REST（懒加载契约）
- [ ] A4 集成（全流程）

### 5.2 playwright 验收（本地 + CI 有浏览器时跑）
- [ ] A4.4 playwright API/WS 断言全过

### 5.3 5 条铁律（SPEC §0.1）
- [ ] 后端无并行内存事件 list（grep 无持久化 events list，只有 tape.replay）
- [ ] 懒加载（/api/runs 无事件，有测试）
- [ ] WS 单通道 + 按需订阅（不推未订阅，有测试）
- [ ] 真并发（asyncio.gather，有测试）
- [ ] 依赖单向（grep iface/web 只 import run/gates/events/schema/compile）

### 5.4 全量回归
- [ ] `uv run pytest -q`（不含 integration）全绿
- [ ] phase 1-6 测试零回归

### 5.5 交付物
- [ ] orca/iface/web/ 7 文件
- [ ] tests/iface/web/ 5 文件
- [ ] pyproject.toml 依赖
- [ ] **commit 全部 `feat(web):` 前缀，独立分支**
- [ ] release note + CHANGELOG

---

## 6. 不做（边界，SPEC §8）

前端（9b）· DAG/replay UI（9c）· gate 弹窗/chart 渲染（9d）· 编排/gate 决策逻辑 · 持久化事件内存 list · 多用户认证
