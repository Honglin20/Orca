# 阶段 10 SPEC —— iface/mcp 壳（外部 MCP 服务）

> **状态**：草稿（待监工确认后写实施计划）
> **依据**：[shells-design-draft.md](shells-design-draft.md) §5（MCP 协议约束 + HandleId pattern）· [phase-6-gates.md](phase-6-gates.md) §1 §4（HumanGate / handler.resolve）· [phase-9a-web-backend.md](phase-9a-web-backend.md)（RunManager 多 run 托管）· 2026-07-01 生态再调研（opencode flush bug #21516 / MCP stdio 是 canonical transport）
> **范围**：外部 MCP 服务（stdio JSON-RPC）+ HandleId 三件套工具 + 与 Web 壳同进程共存 + tape-only query path
> **前置**：phase 6（gates handler）+ phase 9a（RunManager）已完成并合并 master
> **commit 规范**：`feat(mcp):` 前缀，独立分支

---

## 0. 阶段目标 + 铁律

phase 10 回答：**「外部 MCP 客户端（Claude Code / opencode / Cursor）怎么把 Orca workflow 当工具调、长跑 gate 怎么不超时、MCP 和 Web 共存怎么不漂移？」**

### 0.1 七条铁律（违反即返工）

1. **单进程单 RunManager**：一个 Orca 实例 = 一个进程 = 一个 `RunManager` 实例。多壳（MCP / Web）在同进程内并排，共享 `_runs` / `_sem` / `_registry`。**禁止跨进程共享 in-memory state**（live run 的 owner 唯一，第二个进程只能读 tape 做 read-only replay，连 gate 都 resolve 不了）。
2. **tape-only query path**：所有对外查询的唯一数据源是 tape（`replay_state` / `pending_gates_from_tape` / `run_summary`）。`HumanGateHandler._pending` / `_gates_meta` 是 runtime await 状态，**禁止作为查询路径**（重启即丢，且与 tape 漂移）。
3. **HandleId pattern（Call-Now, Fetch-Later）**：每个 MCP tool 秒级返回，**禁止 server 端阻塞**等 gate / 等事件（CC 对 MCP tool call 有 60s 硬超时，[issue #52137](https://github.com/anthropics/claude-code/issues/52137)）。
4. **stdio 每消息 flush**：规避 opencode stdio 批量发不 flush 的 bug（[#21516](https://github.com/anomalyco/opencode/issues/21516)）。SDK 已 flush 也要自包一层兜底。
5. **source="mcp" 复用 handler.resolve**：MCP resolve 走 `HumanGateHandler.resolve` 同一入口，竞速 first-wins + `_broadcaster` 广播（与 Web 走同一机制，零新代码路径）。
6. **不依赖客户端特有能力**：禁用 elicitation（CC 不支持）、progress notification（渲染有限）、长轮询（60s 超时）。引导 Claude chain 调全部靠 tool description + `_hint` 字段。
7. **MCP 返回值文本摘要**：CC / opencode 渲染不了 DAG / chart（草稿 §5.6）。`get_task_status` 只返回**文本进度**（"3/7 nodes · 当前 deploy · ⏸ gate g_3"），富视图指路 Web。

### 0.2 反模式（必须避免）

- ❌ 跨进程共享 in-memory RunManager（live run owner 唯一，第二进程只能 read-only）
- ❌ 长轮询 tool（`wait_for_event` / 阻塞 `resolve_gate`）
- ❌ elicitation（CC 未支持）
- ❌ 查询路径读 `handler._pending` / `_gates_meta`
- ❌ 把 DAG / chart JSON 塞进 MCP 返回值（富视图走 Web）
- ❌ tool 内启动 Web server（生命周期与 MCP 适配器解耦，由启动命令控制）
- ❌ 重新实现 resolve / gate 分发逻辑（复用 `routes/gate.py` 同款 run_id / gate_id 反查）

---

## 1. 进程模型与生命周期

### 1.1 单进程多壳

```
┌─────────────────── 一个 Orca 进程（`orca mcp [--with-web]`）──────────────────┐
│                                                                                │
│   RunManager（唯一实例，进程级单例）                                             │
│   ─ _runs: {run_id: RunHandle}    ─ _sem / _lock / _registry（共享）            │
│                                                                                │
│           ↑ resolve（入）              ↓ events（出）                            │
│    ┌──────┴──────────┐            ┌──────────┴──────────┐                      │
│    │ HumanGateHandler │            │      EventBus        │                     │
│    │   (per-run)      │            │      (per-run)       │                     │
│    └──┬────────┬──────┘            └─────┬──────────┬─────┘                      │
│       │        │                         │          │                            │
│  ┌────┴───┐ ┌───┴────┐            ┌──────┴──┐  ┌─────┴──────┐                   │
│  │ MCP    │ │ Web HTTP│            │ MCP tool│  │ Web WS pump│                   │
│  │resolve │ │/gate/   │            │ 返回值   │  │ push events │                  │
│  │_gate   │ │respond  │            │（文本）  │  │             │                  │
│  │source= │ │source=  │            │         │  │             │                  │
│  │"mcp"   │ │"web"    │            │         │  │             │                  │
│  └───┬────┘ └────┬───┘            └─────────┘  └─────────────┘                   │
│      │          │                                                              │
└──────┼──────────┼──────────────────────────────────────────────────────────────┘
       │          │
   stdio         HTTP/WS
   JSON-RPC
   ↑↓            ↑↓
   CC /          浏览器
   opencode /
   Cursor
```

**核心不变量**：所有壳的 resolve 都进**同一个** `handler.resolve`；所有壳的 events 订阅都来自**同一个** `handle.bus`；状态查询都读**同一个** tape。三壳视觉必然同步（草稿 §4.3 / §6.3）。

### 1.2 启动模式

| 命令 | stdio MCP | Web UI | 主生命周期 | 场景 |
|---|---|---|---|---|
| `orca mcp` | ✅ | ❌ | stdio（CC 持有 stdin）| CC / opencode / Cursor 拉起，纯 MCP |
| `orca mcp --with-web[:port]` | ✅ | ✅ | stdio + Web 双活 | CC 拉起，浏览器同时监控 |
| `orca serve` | ❌ | ✅ | Web（已有，不变）| 纯 server 模式，无 stdio 客户端 |
| `orca run <yaml>` | ❌ | ❌ | 一次性（已有，不变）| CLI 跑单 run |

**`orca mcp --with-web` 是 phase 10 的主推模式**：CC 拉起 Orca 子进程用 stdio 调 MCP 工具，浏览器同进程访问 HTTP 看 DAG / 答 gate —— **一个进程一个 RunManager，无跨进程漂移**。

### 1.3 stdin EOF 生命周期

CC 关闭 / disconnect → Orca stdin 收到 EOF → stdio MCP 适配器收到 EOF。进程退不退？

- **`orca mcp`（无 `--with-web`）**：**退出**。drain in-flight tool call（最大 5s），cancel 后台 run task（草稿 §0.1 第一条说"一次性"的延伸——纯 stdio 模式随 CC session 生灭）。
- **`orca mcp --with-web`**：**不退出**，detach 成 daemon 继续 serve Web。退出条件二选一：
  - 用户显式 `SIGINT` / `SIGTERM`
  - 无活跃 run（`status in {"queued","running"}` 的 run 数 == 0）持续 N 分钟（默认 30，可配 `--idle-timeout`）

**理由**：`--with-web` 意味着用户想用浏览器监控，CC session 关了浏览器还要能看 / 答 gate；纯 stdio 模式无此需求，随 CC 生灭最干净。

### 1.4 单例 RunManager 启动契约

```python
# orca/iface/mcp/server.py 启动入口（伪代码，计划细化）
def run_mcp_server(*, with_web: bool, web_port: int, max_concurrent: int):
    manager = RunManager(max_concurrent=max_concurrent)  # 唯一实例
    assert_runmanager_singleton()  # 启动后 assert：进程内 RunManager 实例数 == 1
    if with_web:
        start_web_in_thread(manager, port=web_port)  # 同进程挂 Web
    run_stdio_mcp(manager)  # 阻塞，stdin EOF 退出
```

启动 assert：进程内 `RunManager` 实例数 == 1（防止后续 refactor 误造多实例）。**单测覆盖**。

---

## 2. MCP 工具面（四件套）

### 2.1 工具清单

| 工具 | 用途 | 优先级 | 返回核心字段 |
|---|---|---|---|
| `start_workflow` | 启动 run，立即返回 task_id | P0 | `task_id, status, _hint` |
| `get_task_status` | 查进度（含 gate 详情）| P0 | `status, current_node, progress, gate?, output?, error?, _hint` |
| `resolve_gate` | 答门 | P0 | `ok, status, _hint` |
| `cancel_task` | 取消 run | P1 | `ok, status, _hint` |

### 2.2 工具签名（契约）

```python
# orca/iface/mcp/server.py

@mcp.tool()
def start_workflow(
    yaml_path: str,
    inputs: dict | None = None,
    task: str | None = None,
    max_iter: int | None = None,
) -> dict:
    """启动一个 Orca workflow，立即返回 task_id（不阻塞）。

    **启动后必须调用 get_task_status(task_id=...) 轮询**，直到 status 为
    completed/failed/cancelled。needs_decision 时按返回的 gate 详情调
    resolve_gate。
    """

@mcp.tool()
def get_task_status(task_id: str) -> dict:
    """查询 task 当前状态。秒级返回，不阻塞。

    返回 status: running | needs_decision | completed | failed | cancelled。
    - needs_decision：含 gate 详情（gate_id / prompt / options / context），需调 resolve_gate。
    - completed：含 output（workflow outputs）。
    - failed：含 error。
    - running：含 progress（"3/7"）+ current_node + 最近 agent_message 摘要。
    """

@mcp.tool()
def resolve_gate(task_id: str, gate_id: str, decision: str) -> dict:
    """对 needs_decision 的 task 提交人的决策。

    decision 通常是 gate.options 之一或自由文本。返回 ok：赢家 True（answer 生效）/ 输家 False（已被别壳答）。
    """

@mcp.tool()
def cancel_task(task_id: str, reason: str | None = None) -> dict:
    """取消 run（P1）。已终态的 run 调它返回 ok=False。"""
```

### 2.3 `_hint` 策略（引导 Claude chain 调，跨客户端通用杠杆）

`_hint` 是返回值里的"傻瓜也能跟着调"的下一步指令。按 status 分支：

| status | `_hint` 内容 |
|---|---|
| running（start_workflow 返回）| "Workflow started in background. Call get_task_status(task_id=...) to poll progress." |
| running（get_task_status 返回）| "Workflow still running. End your turn; the user can ask 'how is it going?' or you can poll again later." |
| needs_decision | "Gate awaiting human decision. Ask the user, then call resolve_gate(task_id=..., gate_id=..., decision=...)." |
| completed | "Workflow completed. Output is in the `output` field." |
| failed | "Workflow failed. Error is in the `error` field." |
| cancelled | "Workflow cancelled." |
| resolve_gate ok=True | "Decision accepted. Call get_task_status to continue polling." |
| resolve_gate ok=False | "Gate already resolved by another channel. Call get_task_status to see current state." |

**关键**：`running` 状态的 hint **显式建议 Claude 结束 turn**，规避 CC 循环检测（草稿 §5.5）。

### 2.4 tool description 强指令（跨客户端唯一杠杆）

每个 tool 的 docstring 必须包含**显式 chain 调指令**，因为不同模型 / 客户端对 tool description 的遵循度不一样，但这是唯一通用机制：

```python
"""启动 Orca workflow，立即返回 task_id（不阻塞）。

**Always call get_task_status(task_id=...) after this returns**, and again
after each resolve_gate, until status is completed/failed/cancelled.

Long-running workflows: do NOT poll more than once per turn. End your turn
after polling and let the user ask for updates.
"""
```

---

## 3. 唯一真相源约束（tape-only query path）⭐ 核心

### 3.1 query path 规则

**任何"查询状态"的对外接口，唯一数据源是 tape**。三个派生函数（纯函数 / 单向依赖）：

| 函数 | 位置 | 输入 | 输出 |
|---|---|---|---|
| `replay_state(tape)` | `orca/events/replay.py`（已有）| tape | `RunState` |
| `pending_gates_from_tape(tape)` | `orca/gates/pending.py`（新）| tape | `list[HumanGate]` |
| `RunManager.run_summary(run_id)` | `orca/iface/web/run_manager.py`（新方法）| run_id | dict（merge 上面两个 + meta）|

依赖方向：`gates → events`（已有），`iface.web → gates + events`（已有）。**无反向依赖**。

### 3.2 `pending_gates_from_tape` 派生逻辑

```python
# orca/gates/pending.py（新文件）
def pending_gates_from_tape(tape: Tape) -> list[HumanGate]:
    """从 tape 派生当前未 resolved 的 gate 列表。

    扫所有事件：
    - 收集 human_decision_requested（gate_id → event）
    - 减去已 human_decision_resolved 的（gate_id set）
    - 剩下的 requested 事件重建 HumanGate（frozen dataclass）

    纯函数，无 runtime 状态依赖。重启进程后仍能查（tape 在磁盘）。
    """
    requested: dict[str, Event] = {}
    resolved: set[str] = set()
    for event in tape.replay():
        if event.type == "human_decision_requested":
            requested[event.data["gate_id"]] = event
        elif event.type == "human_decision_resolved":
            resolved.add(event.data["gate_id"])
    return [
        HumanGate(
            id=e.data["gate_id"],
            prompt=e.data["prompt"],
            options=e.data.get("options"),
            context=e.data.get("context", {}),
            source=e.data["source"],
            run_id=e.data["run_id"],
            node=e.node,
            session_id=e.session_id,
        )
        for gid, e in requested.items()
        if gid not in resolved
    ]
```

### 3.3 `RunManager.run_summary` 组装

```python
# RunManager 新方法（伪代码）
def run_summary(self, run_id: str) -> dict | None:
    """MCP / 其他程序化客户端友好的 run 摘要（不含 _hint，那是 MCP 层加）。"""
    handle = self._runs.get(run_id)
    if handle is None:
        return None
    meta = self._meta_from_handle(handle)
    state = replay_state(handle.tape)
    gates = pending_gates_from_tape(handle.tape)
    status = self._derive_mcp_status(meta.status, gates)  # running/needs_decision/...
    return {
        "task_id": run_id,
        "status": status,
        "current_node": state.current_node,
        "progress": meta.progress,
        "cost": meta.cost,
        "elapsed": meta.elapsed,
        "gate": _gate_to_dict(gates[0]) if gates and status == "needs_decision" else None,
        "output": state.context.get("outputs") if status == "completed" else None,
        "error": meta.error if status == "failed" else None,
    }

def _derive_mcp_status(self, run_status: str, pending_gates: list) -> str:
    """RunStatus → MCP status 映射。needs_decision 优先于 running。"""
    if run_status == "completed": return "completed"
    if run_status == "failed": return "failed"
    if run_status == "cancelled": return "cancelled"
    if pending_gates: return "needs_decision"
    return "running"
```

### 3.4 反例（SPEC 禁止）

```python
# ❌ 错误：读 handler 内存
def pending_gates(run_id) -> list[HumanGate]:
    h = manager.get_handle(run_id)
    return [h.gate_handler._gates_meta[gid]
            for gid, fut in h.gate_handler._pending.items()
            if not fut.done()]

# ❌ 错误：handler 单独暴露 pending 列表（哪怕封装了）
class HumanGateHandler:
    def list_pending(self) -> list[HumanGate]: ...  # 禁止新增
```

### 3.5 `handler._pending` / `_gates_meta` 的合法用途（白名单）

- `_pending[gate_id] = fut` + `await fut` —— `request()` 内部 await 机制（必需）
- `has_pending(gate_id)` —— race 内部判定（`routes/gate.py` 多 run 分发用，已存在）
- `_gates_meta` —— `_broadcaster` emit 时取 node / session_id（广播内部用）

**禁止扩展**：不再新增任何从这两个字段读出对外数据的 API。新增查询路径必须经 tape。

### 3.6 review 检查项

phase 10 实现完，code-reviewer 必须断言：
- `grep -r "_pending\|_gates_meta" orca/iface/mcp/ orca/iface/web/routes/` —— 命中 = 违规
- 仅 `orca/gates/handler.py` 内部 + `orca/iface/web/routes/gate.py` 的 `has_pending` 调用允许

---

## 4. 跨客户端兼容

### 4.1 stdio 协议（canonical transport）

[MCP spec 2025-03-26 §transports](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)：clients SHOULD support stdio whenever possible。Orca 用 `mcp` Python SDK（[modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk)）的 `FastMCP` + `stdio_server`。

**flush 兜底**（规避 [opencode #21516](https://github.com/anomalyco/opencode/issues/21516)）：自包一层 stdio writer，每条 JSON-RPC 消息写完显式 `flush()`。即使 SDK 未来某版本忘了 flush，Orca 不受影响。

```python
# orca/iface/mcp/transport.py（新文件）
class FlushingStdoutWriter:
    """每条消息 flush，规避 opencode stdio bug。"""
    def __init__(self, stream): self._stream = stream
    async def write(self, data: bytes) -> None:
        self._stream.write(data)
        self._stream.flush()
```

### 4.2 不依赖的客户端能力（黑名单）

| 能力 | CC | opencode | Cursor | Orca 处理 |
|---|---|---|---|---|
| elicitation | ❌ | 部分 | ? | **不用**，gate 全走 `resolve_gate` tool |
| progress notification | 有限 | 有限 | ? | **不用**，进度走 `get_task_status` 文本摘要 |
| 长轮询 tool call | ❌ 60s | ❌ | ❌ | HandleId pattern（每 tool 秒级返回）|
| push to client | ❌ | ❌ | ❌ | Claude 主动 poll（外置 cron / skill）|

### 4.3 通用杠杆：tool description + `_hint`

不同模型对 tool description 遵循度不同，但这是 MCP 标准定义的**唯一**引导机制。Orca 三件套工具 description 必须写得"傻瓜也能跟着调"（§2.4）。`_hint` 字段是返回值的补充引导。

### 4.4 客户端差异隔离

未来若某客户端需要特殊处理（如 opencode flush hack 已在适配层），全部隔离在 `orca/iface/mcp/transport.py`，不污染 `server.py` 的工具逻辑。

---

## 5. 缺口实现（前置 P0/P1）

### 5.1 `pending_gates_from_tape` —— P0（§3.2 已述）

新文件 `orca/gates/pending.py`。

### 5.2 `RunManager.run_summary` —— P0（§3.3 已述）

`orca/iface/web/run_manager.py` 新方法。**Web 路由暂不暴露**（仅程序化客户端用，Web 前端走已有的 `/api/runs/{id}` + 自己 fold events）。如未来需要 HTTP `GET /api/runs/{id}/summary`，再加。

### 5.3 `RunManager.cancel_run` —— P1（cancel_task 工具依赖）

```python
# RunManager 新方法（伪代码）
async def cancel_run(self, run_id: str, reason: str | None = None) -> bool:
    """取消 run：cancel asyncio task + emit workflow_cancelled + status 转 cancelled。

    已终态的 run 调它返回 False（无法 cancel）。
    """
    handle = self._runs.get(run_id)
    if handle is None or handle.status in ("completed", "failed", "cancelled"):
        return False
    if handle._task is not None and not handle._task.done():
        handle._task.cancel()
    # emit workflow_cancelled（schema 扩展，§5.4）
    await handle.bus.emit("workflow_cancelled", data={"reason": reason or "user_cancelled"})
    handle.status = "cancelled"
    await self._teardown_handle(handle)
    return True
```

### 5.4 schema 扩展 —— `workflow_cancelled` 事件类型

phase-1 schema 现有 `workflow_started/failed/completed`，**无 cancelled**。phase 10 提议加：

```python
# orca/schema/event.py 扩展 EventType Literal
EventType = Literal[
    ...,
    "workflow_failed",
    "workflow_cancelled",  # 新增：data={reason: str}
    ...,
]
```

`apply_event` 加分支：`workflow_cancelled → state.status = "cancelled"`。

`RunStatus` Literal 同步加 `"cancelled"`。

**理由**：cancel 不写 tape → 进程重启后 replay 仍显示 running（漂移）。写 tape 才符合"tape 是唯一真相"。

### 5.5 `orca mcp` 命令 —— `orca/iface/cli/commands.py` 加 subcommand

```python
@app.command()
def mcp(
    with_web: bool = typer.Option(False, "--with-web", help="同进程额外挂 Web UI"),
    web_port: int = typer.Option(7428, "--web-port"),
    max_concurrent: int = typer.Option(3, "--max-concurrent"),
    idle_timeout: int = typer.Option(30, "--idle-timeout", help="无活跃 run N 分钟后退出（仅 --with-web 模式）"),
) -> None:
    """启动 MCP server（stdio JSON-RPC），供 CC / opencode / Cursor 接入。"""
    from orca.iface.mcp import run_mcp_server
    asyncio.run(run_mcp_server(
        with_web=with_web, web_port=web_port,
        max_concurrent=max_concurrent, idle_timeout=idle_timeout,
    ))
```

---

## 6. 验收标准

### 6.1 工具契约（D3 单元测试覆盖）

- [ ] `start_workflow(demo_linear.yaml)` → 返回 `task_id`（== run_id）+ `status="running"` + `_hint` 引导 poll
- [ ] `get_task_status` 五种 status 各一例（running / needs_decision / completed / failed / cancelled），字段齐全
- [ ] `needs_decision` 时 gate 详情含 `gate_id / prompt / options / context`
- [ ] `resolve_gate` 赢家 → `ok=True`，handler.request 返回的 `(answer, source)` 中 `source="mcp"`
- [ ] `resolve_gate` 晚到（已被别壳答）→ `ok=False` + `_hint` "Gate already resolved"
- [ ] `cancel_task` 已终态 → `ok=False`；running → `ok=True` + status 转 cancelled

### 6.2 七条铁律（§0.1）

- [ ] **tape-only query path**：`grep -r "_pending\|_gates_meta" orca/iface/mcp/ orca/iface/web/run_manager.py` 命中 = 违规（仅 handler.py + routes/gate.py 的 has_pending 允许）
- [ ] **单进程单 RunManager**：启动 assert + 单测覆盖（构造两个 RunManager 应被检测）
- [ ] **HandleId pattern**：每个 tool 单测 mock 慢 manager（sleep 5s）证明 tool 秒级返回（不阻塞）
- [ ] **stdio flush**：transport 单测 mock stdout，断言每条消息后 flush 被调
- [ ] **source="mcp" resolve**：D3 单测断言 `handler.resolve` 收到 `source="mcp"`
- [ ] **不依赖客户端能力**：server 不引用 elicitation / progress notification API
- [ ] **返回值文本摘要**：`get_task_status` 返回 dict 无 `dag` / `chart_json` 等富字段

### 6.3 E2E（D5，用户强调"必须有端到端"）

- [ ] **E2E-1（CI 可跑，无 API key）**：`demo_linear.yaml`（纯 script）→ start_workflow → poll get_task_status 到 completed → assert output.stdout 含 "step_c"
- [ ] **E2E-2（CI 可跑）**：合成 gate workflow → start → poll needs_decision → resolve_gate → poll completed（gate 触发用测试 fixture 直接调 `handler.request`，不依赖 claude）
- [ ] **E2E-3（CI 可跑）**：跨壳 race —— 同进程 MCP + Web test client 同时连，Web 答门，MCP poll 跳过 needs_decision 直接到 running/completed
- [ ] **E2E-4（@pytest.mark.integration）**：`demo_task.yaml`（真 claude call）→ start → poll → completed，assert output 非空
- [ ] **E2E-5（CI 可跑）**：opencode flush 兼容 —— mock stdio 客户端连续发 5 个 tool call 不等 reply，server 仍能逐条 flush 应答（不批量 / 不丢）

### 6.4 启动模式

- [ ] `orca mcp`（无 --with-web）：stdin EOF → 进程 5s 内退出（单测用 subprocess + close stdin）
- [ ] `orca mcp --with-web`：stdin EOF → 进程不退出，HTTP 仍能访问；`--idle-timeout 0` + 无活跃 run → 退出
- [ ] 启动 assert：进程内 RunManager 实例数 == 1

### 6.5 构建

- [ ] `pyproject.toml` 加 `mcp>=1.0` 依赖
- [ ] `orca mcp --help` 显示子命令 + 参数
- [ ] 无前端改动（MCP 是后端 + 工具层）

---

## 7. 给后续阶段的契约

| 后续 | phase 10 提供 |
|---|---|
| 路径 A（CC agent + skill 编排，本次不做）| MCP server 是 skill 的驾驶对象（skill 调 start_workflow / get_task_status / resolve_gate）|
| phase 8（vendor-neutral 跨工具）| MCP 标准入口已就位，opencode / Cursor 等任意 MCP 客户端可接入 |
| `render_chart` / `ask_user` MCP 工具 | server 骨架 + tool 注册机制就位，新工具挂上去即可 |
| cron / skill 定时汇报（外置）| `get_task_status` 是 cron 拉取的稳定接口 |

---

## 8. 不做的事（边界）

- ❌ **路径 A（CC agent + skill 编排）**—— 后续讨论（已对齐：A 是 B 之上的 UX 层）
- ❌ SSE / HTTP MCP transport —— 先 stdio，跨进程场景另议
- ❌ cron / 定时汇报内置 —— 外置 skill 负责（用户已确认）
- ❌ `render_chart` / `ask_user` MCP 工具 —— 独立 SPEC（依赖 phase 10 server 骨架）
- ❌ 把 DAG / chart JSON 塞进 MCP 返回值 —— 富视图走 Web
- ❌ Web 路由暴露 `run_summary` HTTP 端点 —— Web 前端继续用已有 `/api/runs/{id}` + fold events，不增端点（YAGNI）
- ❌ 多 Orca 进程间 run 共享 —— 一进程一 owner，明确不支持

---

## 9. 关键决策备忘（防 drift）

1. **单进程单 RunManager**（§0.1 第一条 / §1.4）—— 防跨进程漂移，启动 assert 保护。
2. **tape-only query path**（§3）—— 防 AgentHarness 多真相源重蹈。`pending_gates_from_tape` 是 P0 新增。
3. **HandleId pattern**（§0.1 第三条 / 草稿 §5.2）—— 60s 超时的唯一规避。
4. **stdio 每消息 flush**（§4.1）—— opencode 兼容兜底。
5. **source="mcp" 复用 handler.resolve**（§0.1 第五条）—— 零新 resolve 路径，复用 phase-6 + phase-9a 的多 run 分发。
6. **`_hint` 按 status 分支**（§2.3）—— 跨客户端通用引导；running 显式建议结束 turn 防循环检测。
7. **`orca mcp --with-web` 是主推模式**（§1.2）—— 一进程两壳，CC 拉起 + 浏览器监控。
8. **stdin EOF 双行为**（§1.3）—— 纯 MCP 随 CC 生灭，`--with-web` 转 daemon。
9. **`workflow_cancelled` 新事件类型**（§5.4）—— cancel 写 tape 才是唯一真相。
10. **`run_summary` 不含 `_hint`**（§3.3 / §2.3）—— summary 是通用 dict，`_hint` 是 MCP 层加的引导字段。
11. **Web 路由不增 `run_summary` 端点**（§8）—— Web 前端继续 fold events，YAGNI。
12. **客户端差异隔离在 transport.py**（§4.4）—— 不污染 server 工具逻辑。
