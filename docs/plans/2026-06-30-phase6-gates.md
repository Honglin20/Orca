# 开发计划 —— 阶段 6：gates/ HMIL 层

> **状态**：待执行（**phase 5 实现完成后开工**）
> **SPEC**：[`docs/specs/phase-6-gates.md`](../specs/phase-6-gates.md)
> **前置**：phase 5（Orchestrator + EventBus）实现完成
> **测试原则**：纯逻辑单元测试为主（不真 spawn claude），hook 桥用 TestClient，集成测试标 `@pytest.mark.integration`。

---

## 0. 产出与执行顺序

### 产出
```
orca/gates/
├── __init__.py              G5（导出）
├── types.py                 G1（HumanGate 原语）
├── handler.py               G2（HumanGateHandler + broadcaster）
├── context_registry.py      G3（session_id 映射）
├── hook_script.py           G4（hook 脚本）
├── http_endpoint.py         G4（/gate 端点）
└── ask_user.py              G4（agent 主动问）
+ tests/gates/ × 5
```

### 执行顺序（依赖链）
```
G1 HumanGate 原语（纯数据，零依赖）
   ↓
G2 HumanGateHandler（依赖 G1 + events）
   ↓
G3 session_id 映射（独立，可并行 G2 收尾）
   ↓
G4 hook 桥 + ask_user（依赖 G2 + G3）
   ↓
G5 导出 + 集成测试
```

---

## G1. HumanGate 原语（纯数据）

### G1.1 `orca/gates/types.py`
- `HumanGate` frozen dataclass（SPEC §1.1）：id/prompt/options/context/source/run_id/node/timeout_hint
- source 类型 `Literal["tool_permission", "agent_ask"]`

### G1.2 验收（G1）— `tests/gates/test_types.py`
- [ ] `HumanGate(id="g1", prompt="p", options=["a","b"], context={}, source="tool_permission", run_id="r", node="n")` 构造成功
- [ ] frozen：`gate.prompt = "x"` → FrozenInstanceError
- [ ] source 非 Literal 值 → TypeError（dataclass 不强制，但类型检查工具能抓；测试用 isinstance）
- [ ] options=None 合法（自由文本）
- [ ] timeout_hint 默认 None

---

## G2. HumanGateHandler（暂停/竞速/广播）

### G2.1 `orca/gates/handler.py`
- `HumanGateHandler(bus)`：`_pending` dict + `_lock` + `_resolved_queue` + `_gates_meta` 缓存
- `async request(gate) -> (answer, source)`：注册 future + emit requested + await
- `resolve(gate_id, answer, source) -> bool`：set_result + 入 resolved_queue + 返回赢家
- `async _broadcaster()`：从 resolved_queue 取 → emit resolved（广播）
- `async start()`/`async stop()`：启动/停止 broadcaster 协程

### G2.2 关键实现点
- emit 用 phase 3 真实签名：`await self._bus.emit("human_decision_requested", data={...}, node=gate.node)`
- `_gates_meta[gate_id]` 缓存 gate 元信息（广播时 emit 需要 node；request 时不 emit run_id 到事件 data，但 handler 内部记 run_id 供日志/广播定位）
- resolve 用 `_lock` 保护并发（多个壳同时 resolve）

### G2.3 验收（G2）— `tests/gates/test_handler.py`
- [ ] `request(gate)` emit `human_decision_requested` 事件（tape 有该行）
- [ ] `request` 在 resolve 前 await 不返回（用 asyncio.wait_for 测超时证明它在等）
- [ ] `resolve(gate_id, "allow", "cli")` 后 `request` 返回 `("allow", "cli")`
- [ ] resolve 返回 True（赢家）
- [ ] 已 resolved 再 resolve → 返回 False + 记 warning（capsys/log 抓）
- [ ] 未知 gate_id 的 resolve → False + warning
- [ ] **竞速**：两个 task 同时 resolve 同一 gate，只有一个返回 True（用 asyncio.gather + 略微 delay 模拟）
- [ ] **广播**：resolve 后 emit `human_decision_resolved`（含 answer + resolved_by）
- [ ] gate 无限等（request 无 timeout，测 await 不超时）
- [ ] **不依赖壳**：纯逻辑，测试用 fake resolve 调用

### G2.4 测试用例骨架
```python
async def test_request_resolve_basic(bus, handler):
    gate = _make_gate("g1")
    task = asyncio.create_task(handler.request(gate))
    await asyncio.sleep(0.01)  # 让 request 跑到 await
    ok = handler.resolve("g1", "allow", "cli")
    assert ok is True
    answer, source = await task
    assert (answer, source) == ("allow", "cli")

async def test_race_first_wins(bus, handler):
    gate = _make_gate("g2")
    task = asyncio.create_task(handler.request(gate))
    await asyncio.sleep(0.01)
    r1, r2 = await asyncio.gather(
        asyncio.to_thread(handler.resolve, "g2", "allow", "cli"),
        asyncio.to_thread(handler.resolve, "g2", "deny", "web"),
    )
    assert sum([r1, r2]) == 1  # 只有一个赢家
    await task

async def test_double_resolve_fail_loud(bus, handler, caplog):
    gate = _make_gate("g3")
    t = asyncio.create_task(handler.request(gate))
    await asyncio.sleep(0.01)
    handler.resolve("g3", "allow", "cli")
    ok = handler.resolve("g3", "deny", "web")
    assert ok is False
    assert "已 resolved" in caplog.text
    await t

async def test_broadcast_emits_resolved(bus, handler):
    gate = _make_gate("g4")
    sub = bus.subscribe()
    t = asyncio.create_task(handler.request(gate))
    await asyncio.sleep(0.01)
    handler.resolve("g4", "allow", "web")
    await t
    events = [e async for e in _drain(sub)]
    types = [e.type for e in events]
    assert "human_decision_requested" in types
    assert "human_decision_resolved" in types
```

---

## G3. session_id 映射（hook 桥定位）

### G3.1 `orca/gates/context_registry.py`
- `SessionContextRegistry`：`register(session_id, run_id, node)` / `lookup(session_id) -> (run_id, node) | None` / `unregister(session_id)`
- 线程安全（用 lock 或 dict 原子操作）

### G3.2 验收（G3）— `tests/gates/test_context_registry.py`
- [ ] register + lookup 正确
- [ ] lookup 未注册的 session_id → None
- [ ] unregister 后 lookup → None
- [ ] 同一 session_id 重复 register → 覆盖（last-writer-wins）

---

## G4. hook 桥 + ask_user

### G4.1 `orca/gates/hook_script.py`（hook 脚本，用户配置到 .claude）
- 读 stdin JSON
- POST `http://localhost:${ORCA_PORT}/gate`，body = stdin JSON，timeout = `ORCA_GATE_TIMEOUT`（默认 60）
- 响应 decision=="allow" → exit 0；否则 exit 2
- 任何异常（连接失败/超时/响应非法）→ exit 2（安全优先）
- 用 stdlib `urllib` 或 `httpx`（若已在依赖）

### G4.2 `orca/gates/http_endpoint.py`（Orca server 端点）
- `register_gate_routes(app, handler, registry)`：注册 POST /gate + POST /gate/respond
- POST /gate：构造 HumanGate(source=tool_permission) → 从 hook stdin 的 session_id 经 registry 查 run_id/node → handler.request → 返回 {decision, resolved_by}
- POST /gate/respond：壳 resolve 路径（{gate_id, answer, source}）→ handler.resolve

### G4.3 `orca/gates/ask_user.py`
- `ask_user(handler, prompt, options, context, run_id, node) -> str`（SPEC §5.2）
- 触发 HumanGate(source=agent_ask)

### G4.4 验收（G4）— `tests/gates/test_hook_bridge.py` + `test_ask_user.py`
- [ ] hook 脚本：mock server 返回 allow → exit 0
- [ ] hook 脚本：mock server 返回 deny → exit 2
- [ ] **hook 脚本安全语义**：server 不可达 → exit 2；超时 → exit 2（用 subprocess 跑脚本 + mock server 延迟）
- [ ] /gate 端点：POST hook payload → handler.request 被调 → 返回 decision
- [ ] /gate/respond：POST → handler.resolve 被调
- [ ] session_id 映射：/gate 从 registry 查到 run_id/node 注入 gate
- [ ] ask_user：调 handler.request(source=agent_ask) → resolve 返回 answer

### G4.5 测试用例骨架
```python
async def test_hook_exit_codes(tmp_path):
    """跑真 hook_script.py 子进程，mock server 返回不同响应，断言 exit code。"""
    # mock server 返回 {"decision": "allow"} → exit 0
    # mock server 返回 {"decision": "deny"} → exit 2
    # server 不启动 → exit 2
    # server 延迟超过 timeout → exit 2

async def test_gate_endpoint_resolves(handler, registry, client):
    registry.register("sess-1", "run-1", "node-1")
    # fake 壳 resolve
    asyncio.create_task(_delayed_resolve(handler, "g-x", "allow", "test"))
    resp = await client.post("/gate", json={"session_id": "sess-1", "tool_name": "Bash", "tool_input": {}})
    assert resp.json()["decision"] == "allow"

async def test_ask_user(handler):
    t = asyncio.create_task(ask_user(handler, "需要连接串?", options=["a","b"], run_id="r", node="n"))
    await asyncio.sleep(0.01)
    gate_id = next(iter(handler._pending))
    handler.resolve(gate_id, "a", "test")
    assert await t == "a"
```

---

## G5. 导出 + 集成

### G5.1 `orca/gates/__init__.py`
- 导出 `HumanGate, HumanGateHandler, SessionContextRegistry, ask_user, register_gate_routes`

### G5.2 集成测试（`tests/gates/test_integration.py`，@pytest.mark.integration）
- [ ] **端到端 mock**：构造 EventBus + HumanGateHandler + fake「claude 想调工具」(POST /gate) + fake 壳 resolve → claude resume 信号正确
- [ ] **ask_user 端到端**：agent_ask gate → fake 壳 resolve → 返回
- [ ] **竞速端到端**：两个 fake 壳同时 resolve，赢家 + 广播 + 输家 fail loud
- [ ] **tape 完整性**：整个流程后 replay_state 能重建（gate 事件在 tape 里）

### G5.3 验收（G5）
- [ ] `from orca.gates import HumanGateHandler` 等导出
- [ ] 集成测试全过（标 integration，CI skip，本地跑）
- [ ] 单元测试全过（CI 跑）

---

## 6. 总验收（Definition of Done）

### 6.1 单元测试（CI 跑）
- [ ] G1 HumanGate 原语
- [ ] G2 HumanGateHandler（request/resolve/竞速/广播/fail loud）
- [ ] G3 session_id 映射
- [ ] G4 hook 桥（exit code 安全语义）+ ask_user

### 6.2 集成测试（本地跑）
- [ ] G5 端到端 mock 流程

### 6.3 5 条铁律（SPEC §7.0）
- [ ] gate 事件写 tape（grep handler.py 有 bus.emit）
- [ ] 三壳读同一份 tape（无第二份 gate 状态存储）
- [ ] 依赖单向（`grep "from orca.run\|from orca.exec\|from orca.iface" orca/gates/` 零匹配）
- [ ] 安全优先（hook 桥超时/不可达 = exit 2，有测试）
- [ ] 广播语义（resolved 事件 emit，有测试）

### 6.4 全量回归
- [ ] `uv run pytest -q`（不含 integration）全绿
- [ ] phase 1-5 测试零回归

### 6.5 交付物
- [ ] orca/gates/ 6 文件
- [ ] tests/gates/ 5 文件
- [ ] release note（phase6-gates）
- [ ] CURRENT.md + CHANGELOG 更新

---

## 7. 不做（边界，SPEC §9）

壳的具体渲染（CLI/Web/MCP）· 真 spawn claude（用 mock）· elicitation · gate 持久化恢复 · 三壳并发真跑竞速（单壳能 resolve 即可）
