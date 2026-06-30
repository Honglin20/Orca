# Release Note —— 阶段 6：gates/ HMIL 层

> **日期**：2026-07-01
> **commit**：`<待回填>`
> **SPEC**：[`docs/specs/phase-6-gates.md`](../specs/phase-6-gates.md)
> **计划**：[`docs/plans/2026-06-30-phase6-gates.md`](../plans/2026-06-30-phase6-gates.md)
> **前置**：phase 5（Orchestrator + EventBus）

---

## 1. 做了什么

回答「workflow 需要人决策时怎么暂停 / 把决策广播给三壳 / 等任一壳答 / 恢复？」——
phase 6 实现 HMIL（Human-in-the-Loop）核心，**统一两个决策来源**（Claude 想调危险工具 /
agent 主动问用户）为 `HumanGate` 原语，走同一个暂停/竞速/广播/恢复机制。

### 交付（6 模块 + 5 测试文件）

```
orca/gates/
├── __init__.py           导出 HumanGate / HumanGateHandler / SessionContextRegistry / ask_user / register_gate_routes
├── types.py              G1 HumanGate frozen dataclass（id/prompt/options/context/source/run_id/node/session_id/timeout_hint）
├── handler.py            G2 HumanGateHandler（request/resolve）+ _broadcaster 协程 + start/stop 生命周期
├── context_registry.py   G3 SessionContextRegistry（claude session_id → (run_id, node)，threading.Lock 保护）
├── hook_script.py        G4 PreToolUse hook HTTP 桥（stdlib only，安全优先 exit 2 语义）
├── http_endpoint.py      G4 register_gate_routes：POST /gate + POST /gate/respond（FastAPI）
└── ask_user.py           G4 ask_user（agent 主动问，source=agent_ask）
tests/gates/  test_types / test_handler / test_context_registry / test_ask_user / test_hook_bridge / test_integration(@integration)
```

### 关键设计决策（SPEC §10）

1. **HumanGate 统一两个来源**（`tool_permission` / `agent_ask`），`source` 仅驱动壳渲染分支。
2. **gate 事件写 Tape**：`human_decision_requested` / `human_decision_resolved` 都 emit 到
   EventBus 写 Tape（唯一真相，三壳读同一份，无第二份 gate 状态存储）。
3. **request 是 async**（`await emit` + `await fut`），**resolve 同步非阻塞**（set_result +
   入 `_resolved_queue`，**不直接 emit**——广播由 `_broadcaster` 异步负责）。
4. **gate 无限等**：request 的 `await fut` 无 timeout（超时只在 hook 桥传输层，SPEC §2.2 决策 3）。
5. **hook 桥安全优先**：超时/不可达/响应非法/非 allow → exit 2（绝不放行，HMIL 底线）。
6. **三通道竞速 = 广播**：任一壳 resolve → `_broadcaster` emit resolved → 全壳收到（视觉同步）。
7. **晚到 resolve = fail loud**：未知/已 resolved → 返回 False + warning（不静默吞）。
8. **依赖单向**：gates → events（仅 TYPE_CHECKING 引用 EventBus），**不依赖 run/exec/iface**。
   `grep "orca\.run\|orca\.exec\|orca\.iface" orca/gates/` 零匹配。
9. **session_id 透传 event 顶层**：HumanGate 新增 `session_id` 字段，两处 `bus.emit(...)` 都
   透传 `session_id=`（phase 3 §3.3 身份模型——壳 reducer 据此分组关联到 claude 会话）。

### HTTP 栈 + hook 依赖决策（任务要求 justify）

- **HTTP 栈 = FastAPI**（`uv add fastapi httpx`）：与 phase 9 web server 同栈（shells-design-draft
  §4.1 定稿）；`register_gate_routes(app, handler, registry)` 框架中立地挂路由到提供的 app，
  phase 9 构建完整 app 后调用本函数复用。`httpx` 用于测试（`AsyncClient + ASGITransport` 单 loop
  驱动 FastAPI app，无跨线程 race）。
- **hook_script.py = stdlib only**（`urllib.request` / `json` / `os` / `sys`）：hook 跑在 claude
  spawn 的独立短命进程，**可能没有 Orca 的 venv**（claude 用系统 Python spawn）。不 import
  httpx/fastapi/orca 任何模块，保证它在任何 Python 3.10+ 环境都能跑。
- **端口/超时**：`ORCA_PORT`（默认 7421，与 phase 9 web 同站）+ `ORCA_GATE_TIMEOUT`（默认 60s）。

### broadcaster 生命周期设计（任务要求 justify）

- `start()` 创建 `_broadcaster` asyncio task（幂等）；`stop()` 投 `_STOP` 哨兵入队 + `await task`
  干净退出（5s 兜底 cancel，记 error），幂等。
- 用 `asyncio.Queue` + 私有单例 sentinel `_STOP`（而非 None）避免与 `(gate_id, answer, source)`
  元组形态冲突。
- **resolve 跨线程安全**：`resolve()` 用 `threading.Lock`（不是 asyncio.Lock）保护「get + done
  check + set_result + put_nowait」原子段——resolve 可能从 hook HTTP handler 线程或
  `asyncio.to_thread` 工作线程并发调用，必须跨线程串行（否则 race first-wins 失效）。与
  `context_registry.py` 的 `threading.Lock` 用法一致。

---

## 2. 偏离计划

- **HumanGate 新增 `session_id` 字段**（SPEC §1.1 未列）：code-review 发现原实现未把 claude
  session_id 透传到 event 顶层（只放进 `context` dict），违背 phase 3 §3.3 身份模型（壳 reducer
  按 session 分组）。修复：HumanGate 加 `session_id` 字段，两处 `bus.emit(...)` 补 `session_id=`。
  这是 schema 小步演进，未破坏 SPEC §1.1 其余字段。
- **`resolve()` 加 `threading.Lock`**（SPEC §2.1 内联了 `async with self._lock`，但 resolve 是
  同步方法不能用 asyncio.Lock）：code-review 发现原「无锁靠 GIL」论断在多线程 to_thread 路径
  有 TOCTOU 风险。改为 `threading.Lock` 保护原子段，注释修正。
- **空 stdin hook 行为**：code-review 建议测「空 stdin → exit 2」，但实测 `sys.stdin.read()` 对
  空 stdin 返回 `""`（不抛），hook 会 POST 空 body，安全语义由 server 响应决定。测试改为验证
  「空 stdin + server allow → 0 / server deny → 2」（server 仍是安全决策点），符合实际协议。

---

## 3. 验证

### 测试统计
- **gates 单元测试**：36 passed（不含 integration），4 integration passed（`@pytest.mark.integration`，CI skip）
- **全量回归**：478 passed, 8 deselected（478 = 442 phase-5 基线 + 36 gates 净增），**phase 1-5 零回归**

### SPEC §7 验收标准 file:line 证据

| 验收项 | 文件:行 |
|---|---|
| §7.0 铁律 1（gate 事件写 tape） | `orca/gates/handler.py:134`（request emit）+ `orca/gates/handler.py:217`（broadcaster emit） |
| §7.0 铁律 3（依赖单向） | `grep` 零匹配；handler 仅 `TYPE_CHECKING` 引 `orca.events.bus`（`handler.py:42`） |
| §7.0 铁律 4（hook 安全优先） | `orca/gates/hook_script.py:97,99,109,114`（4 处 exit 2） |
| §7.0 铁律 5（广播语义） | `orca/gates/handler.py:200-235`（`_broadcaster`） |
| §7.1 HumanGate frozen | `orca/gates/types.py:36`（`@dataclass(frozen=True)`） |
| §7.2 request emit + await | `orca/gates/handler.py:134,150` |
| §7.2 resolve 返回赢家 | `orca/gates/handler.py:162-200`（`threading.Lock` 保护） |
| §7.2 已 resolved → False + warning | `orca/gates/handler.py:176-182` |
| §7.2 gate 无限等（无 timeout） | `orca/gates/handler.py:150`（`return await fut` 无 timeout） |
| §7.3 race first-wins | `tests/gates/test_handler.py:137`（`to_thread` gather + count(True)==1） |
| §7.3 resolved 广播 emit | `tests/gates/test_handler.py:168`（订阅断言 requested+resolved） |
| §7.4 hook 安全语义（4 路径） | `tests/gates/test_hook_bridge.py:130,139,148,158`（allow→0/deny→2/unreachable→2/timeout→2） |
| §7.4 session_id 映射注入 | `orca/gates/http_endpoint.py:81,103`（registry.lookup → gate.session_id） |
| §7.5 ask_user 触发 agent_ask | `orca/gates/ask_user.py:48` + `tests/gates/test_ask_user.py:13` |
| §7.6 端到端 mock（@integration） | `tests/gates/test_integration.py`（4 用例） |

### code-review 发现 + 修复

| 发现 | 严重度 | 处理 |
|---|---|---|
| session_id 未透传 event 顶层 | major（架构） | 已修：HumanGate 加字段 + 两处 emit 补 `session_id=` + 测试 `test_session_id_propagated_to_event` |
| resolve() 缺锁，多线程 TOCTOU | major（bug） | 已修：`threading.Lock` 保护原子段，注释修正 |
| 空 stdin hook 测试假设错误 | nit | 已修：改为 server 响应决定语义的测试 |
| broadcaster emit 失败容错无测试 | minor | 已加：`test_broadcaster_survives_emit_failure` |
| race 断言可读性 | nit | 已改：`(r1, r2).count(True) == 1` |
| `except ... pass` 吞 Exception | nit | 已改：仅 catch `CancelledError` |
| `__init__.py` 文档「events+schema」不准 | nit | 已改：文档改为「events」 |
| hook_script 路径硬编码 `parents[2]` | nit | 已改：从 `orca.gates` 包定位 |

---

## 4. 给后续阶段的契约

| 后续 | phase 6 提供 |
|---|---|
| phase 7 cli | `HumanGateHandler`（CLI 壳调 resolve）+ gate 事件订阅 |
| phase 9 web | `register_gate_routes(app, handler, registry)`（web server 复用）+ HumanGateHandler |
| phase 10 mcp | `ask_user(handler, ...)` + `resolve_gate` 接入 handler（MCP 工具注册归 phase 10）|

---

## 5. 不做（边界，SPEC §9）

壳的具体渲染（CLI ModalScreen / Web 弹窗 / MCP tool 注册）· 真 spawn claude（用 mock）·
elicitation · gate 持久化恢复 · 三壳并发真跑竞速（单壳能 resolve 即可）。
