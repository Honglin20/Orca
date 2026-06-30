# phase 9a —— iface/web 后端（FastAPI + WebSocket + RunManager 真并发 + 懒加载 REST）

> **分支**：`phase9-web`（从 master 分出，phase 9 全部子阶段在此分支）
> **commit 规范**：`feat(web):` 前缀（web 是纯渲染/转发层，与引擎改动隔离）
> **SPEC**：[`docs/specs/phase-9a-web-backend.md`](../specs/phase-9a-web-backend.md)
> **计划**：[`docs/plans/2026-06-30-phase9a-web-backend.md`](../plans/2026-06-30-phase9a-web-backend.md)

---

## 做了什么

phase 9a 回答「后端怎么托管多个并发 run、把事件流按需推给前端、让前端懒加载？」。实现
FastAPI 后端（单进程同引擎 asyncio）+ WebSocket 单通道 + RunManager 真并发 + 懒加载 REST。

### 交付物

```
orca/iface/web/
├── __init__.py        导出 create_app, run_server, RunManager, RunHandle, RunMeta
├── server.py          create_app + lifespan + run_server（uvicorn 同事件循环）
├── run_manager.py     RunManager（真并发 Semaphore）+ RunHandle + RunMeta（懒加载）
├── ws_handler.py      WebServer：单通道 WS + 按 run 订阅 + gate_response 反向
├── routes/{runs,run,gate}.py   懒加载 REST + 多 run gate 分发
└── static/.gitkeep    （phase 9b 前端构建产物占位）
tests/iface/web/{conftest,test_run_manager,test_ws,test_routes,test_gate_routes,
                  test_integration,test_playwright}.py
```

### 关键设计

1. **RunManager 真并发**：`asyncio.Semaphore(max_concurrent)`（默认 3），sem 内自然并发，
   超过的 queued（不是单活跃）。每个 run 独立 bus + tape + gate_handler（隔离）。
2. **懒加载契约**：`GET /api/runs` 只返回 `RunMeta`（7 字段，无 events）；事件只在
   `GET /api/runs/<id>/events`（`tape.replay()`）。元数据从 `replay_state(tape)` 派生
   （progress/cost 与真相源一致）。
3. **WS 单通道 + 按需订阅**：单条 `/ws`，client 发 `subscribe(run_id)` → 起 pump 推**该 run**
   的事件（带 run_id 标签）；切 run / 断开 cancel 旧 pump（无 leaked task）。反向通道同 WS
   收 `gate_response`。
4. **多 run gate 分发**：phase-6 `register_gate_routes` 单 handler 无法表达多 run。web 层加
   薄分发器：session_id → 共享 registry → run_id → 该 run 的 `handle.gate_handler`。
   HumanGate 构造 + session_id 反查逻辑提取为 phase-6 共享 helper（`resolve_session_context` /
   `build_gate_from_hook_payload`），web 与 CLI 共用（DRY）。
5. **单进程同引擎**：uvicorn + orchestrator 共享同一 asyncio loop（lifespan 管 shutdown）。

### 五条铁律（SPEC §0.1）逐条验证

| 铁律 | 验证 | 证据 |
|---|---|---|
| 1. tape 唯一真相 | PASS | `grep "events.*=.*\[\]" orca/iface/web/` 空；全量事件唯一来源 `tape.replay()`（`run_manager.py:176`）|
| 2. 懒加载 | PASS | RunMeta 7 字段无 events（`run_manager.py:71-84`）；`/api/runs` body 无 events（测试 `test_routes.py:53` 等三处断言）|
| 3. WS 单通道 + 按需订阅 | PASS | 单条 `/ws`（`server.py:75`）；subscribe(A) 不推 B（测试 `test_ws.py:155`）|
| 4. 真并发 | PASS | Semaphore（`run_manager.py:108`）；3 个同时 running 断言（`test_run_manager.py:48`）|
| 5. 依赖单向 | PASS | `grep "orca\.iface\.web" orca/`（非 web 内）空；iface/web 只 import run/gates/events/schema/compile |

---

## 偏离计划处

- **gate 分发不直接复用 `register_gate_routes`**：计划 §5 写「复用 phase 6 gate 端点」，
  但 phase-6 是单 handler 绑定，多 run 场景每个 run 独立 handler（隔离铁律）。改为：
  HumanGate 构造 + session_id 反查提取为 phase-6 共享 helper（DRY），web 层加薄分发器选 handler。
  在 `routes/gate.py` 文件头 docstring 记录决策。
- **`websockets` 依赖**：uvicorn 真 server WS 后端需要。引入后触发 websockets.legacy 弃用警告
  （第三方库噪音），pyproject `filterwarnings` 过滤（仅压第三方 DeprecationWarning，不压
  RuntimeWarning/ResourceWarning）。

## review findings 修复

dispatch `code-reviewer` 后修复全部 blocker + major：
- 加 `routes/gate.py` 多 run 分发测试（8 个，此前零覆盖）。
- `shutdown` 加超时兜底（run 卡 gate 时不再 hang，cancel task）。
- DRY：提取 `resolve_session_context` / `build_gate_from_hook_payload` 共享 helper（phase-6 + web）。
- `EventBus.close` 加幂等 guard（与 Tape 对齐）。
- `HumanGateHandler.has_pending` 公开方法（避免 web 访问私有 `_pending`）。
- `runs.py` 单 run 端点用 `get_run_meta`（避免 N+1 replay 全部 run）。
- `routes/gate.py` fallback 收窄：仅当恰好一个活跃 run 才兜底（多 run 时 400，fail loud）。
- `_cancel_sub` except 收窄为 `CancelledError`。
- `test_integration.py` 名实不符测试改名（诚实反映只验 events 端点）。

## 验证结果

- `uv run pytest -q -m "not integration" tests/iface/web`：37 passed，0 warnings。
- `uv run pytest -q -m "not integration" tests/iface/web -W "error::RuntimeWarning"
  -W "error::ResourceWarning"`：37 passed，0 warnings（web 套件全清）。
- 全量 `uv run pytest -q -m "not integration"`：594 passed，0 RuntimeWarning
  （`tests/run/` 有预存 ResourceWarning，phase 5 遗留，非 phase 9a 引入，stash 验证确认）。
- phase 1-7 零回归（phase-6 `http_endpoint.py` 重构后 gate 测试仍绿）。
- 5 条铁律 grep 全过。

## commit SHA

`b34c87d`

## 下一步

phase 9b：前端骨架（路由导航栈 + Zustand 单 store + 懒加载 + WS hook）。
