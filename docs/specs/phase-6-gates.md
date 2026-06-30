# 阶段 6 SPEC —— gates/ HMIL 层（HumanGate 统一原语 + hook 桥 + 三通道竞速）

> **状态**：最终版（待分发实现）
> **依据**：[TASK.md](../TASK.md) §4 · [shells-design-draft.md](shells-design-draft.md) §1 §6 · [phase-3-events.md](phase-3-events.md) §3.3 · [phase-5-run.md](phase-5-run.md) §4
> **范围**：① HumanGate 原语 + HumanGateHandler；② PreToolUse hook HTTP 桥；③ 三通道竞速（广播语义）；④ ask_user MCP 工具占位（壳的 resolve 路径）
> **前置**：phase 5（Orchestrator + EventBus）实现完成
> **不是**：任何壳的具体渲染（CLI/Web/MCP 各自的 gate UI 归 phase 7/9/10）

---

## 0. 阶段目标

phase 6 回答唯一一个问题：**「当 workflow 需要人决策时（工具权限 / agent 主动问），怎么暂停引擎、把决策广播给三壳、等任一壳回答、广播结果、恢复引擎？」**

这是 HMIL（Human-in-the-Loop）的核心。两个决策来源（Claude 想调危险工具 / agent 主动问用户）**本质同源**（暂停 + 等人决策 + 继续），统一为 `HumanGate` 原语。

| 模块 | 解决什么 | 核心交付 |
|---|---|---|
| `HumanGate` 原语 | 统一两个决策来源的数据模型 | frozen dataclass，含 source 区分渲染 |
| `HumanGateHandler` | 暂停 / 竞速 / 广播 / 恢复 | `request() -> str` + `resolve() -> bool` |
| PreToolUse hook 桥 | claude `-p` 的工具权限拦截 | HTTP 桥：hook → POST /gate → 阻塞 → exit code |
| 三通道竞速 | 三壳抢答，无冲突 | FIRST_COMPLETED + 广播 resolved |
| ask_user MCP 工具 | agent 主动问的第二个来源 | MCP 工具（壳的 resolve 路径，占位实现） |

**核心铁律**：gate 事件（`human_decision_requested` / `human_decision_resolved`）写进 tape，是唯一真相。三壳从同一份 tape 读 gate 状态，无投影分裂（反 AgentHarness）。

---

## 1. HumanGate 原语（统一两个决策来源）

### 1.1 数据模型

```python
# orca/gates/types.py
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True)
class HumanGate:
    """统一的人机决策原语。两个来源（工具权限 / agent 主动问）共用此模型，
    用 source 字段区分渲染（权限弹窗 vs 问答弹窗）。"""
    id: str                                  # uuid4 hex，gate 唯一标识
    prompt: str                              # 给人看的问题
    options: list[str] | None                # 固定选项（None = 自由文本输入）
    context: dict                            # 哪个 node / 哪个工具 / 什么参数
    source: Literal["tool_permission", "agent_ask"]  # 渲染分支依据
    run_id: str                              # 哪个 run 触发的（广播定位用）
    node: str | None                         # 哪个 node（None = workflow 级）
    timeout_hint: float | None = None        # 给壳的 UI 提示（非强制；gate 本身无限等）
```

### 1.2 两个 source 的语义差异（仅渲染，不分裂数据流）

| source | 触发方 | 默认渲染 | context 示例 |
|---|---|---|---|
| `tool_permission` | PreToolUse hook（Claude 想调工具）| 权限弹窗：显示「工具名 + 参数 + 批准/拒绝/编辑」 | `{tool: "Bash", tool_input: {command: "rm -rf ..."}, tool_use_id: "..."}` |
| `agent_ask` | ask_user MCP 工具（agent 主动问）| 问答弹窗：显示「问题 + 选项/输入框」 | `{question: "需要数据库连接串", suggested: [...]}` |

**统一点**：都是 HumanGate，都走三通道竞速，都写进同一个 tape。**分化点**只在壳的 UI 渲染（壳读 `gate.source` 决定弹窗样式），不在数据流。这避免「两套机制」。

---

## 2. HumanGateHandler（暂停 / 竞速 / 广播 / 恢复）

### 2.1 接口

```python
# orca/gates/handler.py
class HumanGateHandler:
    def __init__(self, bus: EventBus):
        self._bus = bus
        self._pending: dict[str, asyncio.Future[tuple[str, str]]] = {}
        # _pending[gate_id] = Future，resolve 时 set_result((answer, source))
        self._lock = asyncio.Lock()  # 保护 _pending 的并发 resolve

    async def request(self, gate: HumanGate) -> tuple[str, str]:
        """emit human_decision_requested + 暂停 + 等任一壳 resolve。
        返回 (answer, source)——source 是哪个壳答的（cli/web/mcp）。"""
        fut = asyncio.get_event_loop().create_future()
        async with self._lock:
            self._pending[gate.id] = fut
        # emit 第一动作 = 写 tape（唯一真相），见 phase-3 EventBus
        await self._bus.emit(
            "human_decision_requested",
            data={
                "gate_id": gate.id, "prompt": gate.prompt,
                "options": gate.options, "source": gate.source,
                "context": gate.context, "node": gate.node,
            },
            node=gate.node,
        )
        return await fut  # 任一壳 resolve 唤醒（广播语义见 §4）

    def resolve(self, gate_id: str, answer: str, source: str) -> bool:
        """任一壳调它喂答案。返回是否是赢家（FIRST_COMPLETED）。
        已 resolved 的 gate：返回 False，输入静默丢弃 + 记 warning（fail loud）。"""
        fut = self._pending.get(gate_id)
        if fut is None or fut.done():
            logger.warning("gate %s 已 resolved，source=%s 的输入被丢弃", gate_id, source)
            return False
        fut.set_result((answer, source))  # 唤醒 request() 的 await
        return True
```

### 2.2 关键约束

1. **request 是 async**：内部 `await self._bus.emit`（emit 是 async，phase 3 §3.3）+ `await fut`（等人）。Orchestrator 在 node 执行前后 `await gate_handler.request(gate)` 暂停。
2. **resolve 是同步非阻塞**：壳调它喂答案，立即返回（是否赢家）。resolve **不 emit 事件**——广播由 `_broadcaster` 协程负责（见 §4），避免 resolve 阻塞。
3. **gate 无限等**：request 的 `await fut` 没有 timeout。人可能要想很久/不在场。**超时只在 hook 桥那一侧**（§3，传输层超时，不是 gate 语义层超时）。
4. **fail loud**：未知 gate_id / 已 resolved 的 resolve → 记 warning，不静默吞。
5. **依赖单向**：gates 依赖 events（EventBus）+ schema（Event 类型），**不依赖 run/exec**。Orchestrator 调 gates，gates 不知道 orchestrator 存在。

### 2.3 与 Orchestrator 的对接（phase 5 扩展点）

phase 5 的 Orchestrator 主循环里，gate 插入点是 **node 执行前后**：

```python
# phase 5 orchestrator（phase 6 扩展：加 gate_handler）
async def _run_node(self, node, ctx):
    # phase 6 扩展：node 执行前可插 gate（如 agent 想调工具被 hook 拦）
    # ...
    output = await execute_and_emit(executor, node, ctx, self.bus)
    return output
```

**实际上 gate 触发不在 orchestrator 主循环**，而在 **hook 桥**（§3）：claude 子进程想调工具 → hook 拦 → POST /gate → HumanGateHandler.request → 广播 → resolve → hook 返回 exit code → claude 继续。orchestrator 主循环对 gate 透明（它只是等 node 执行完，而 node 内部的 claude 被 gate 卡住了）。

> **关键认知**：gate 不打断编排拓扑，只打断**单个 node 内部的 claude 工具调用循环**。orchestrator 视角下 node 在「执行中」（只是执行变慢了，因为 claude 在等 gate）。

---

## 3. PreToolUse hook HTTP 桥（工具权限拦截）

### 3.1 hook 机制（Claude Code 客观事实）

Claude Code 的 PreToolUse hook：claude 想调工具 → spawn 一个独立短命进程（hook）→ hook 收 stdin JSON（含 `{session_id, tool_name, tool_input, tool_use_id, permission_mode}`）→ hook exit code 决定 claude 行为：
- `exit 0` = 允许
- `exit 2` = 拒绝（claude 收到拒绝反馈）
- 其他 = 阻塞（实测：hook sleep 时 claude 整整等，什么都不做）

**实测铁证（2026-06-29）**：hook 在 `claude -p` headless 下完全能同步阻塞。

### 3.2 桥架构

hook 是 claude spawn 的独立短命进程，与 Orca server **不同进程**。用 **HTTP** 把「需要决策」送到 Orca + 阻塞等答案：

```
claude -p 想调 Bash
   │
   ▼
spawn hook（独立短命进程，Orca 提供的脚本）
   │ stdin: {session_id, tool_name: "Bash", tool_input: {...}, tool_use_id, ...}
   ▼
hook → HTTP POST /gate（body: hook stdin JSON）→ Orca server
   │                                          │
   │                                          ▼
   │                          HumanGateHandler.request(gate)
   │                          → emit human_decision_requested（写 tape + 广播）
   │                          → 暂停 await fut（等任一壳答）
   │                                          │
   ▼                                          ▼
hook 阻塞等 HTTP 响应 ◀────────── 任一壳 resolve(gate_id, answer) ──── CLI/Web/MCP
   │
   ▼
hook 根据响应 exit 0/2
   │
   ▼
claude 收到允许/拒绝，继续或调整
```

### 3.3 安全语义（2026-06-30 定稿：安全优先）

**决策**：hook HTTP 桥 **超时/不可达 = 拒绝（exit 2）**，绝不放行。

| 场景 | 行为 | 理由 |
|---|---|---|
| Orca server 正常 + 人答了 | exit 0（批准）/ exit 2（拒绝）| 正常路径 |
| Orca server 不可达（连接失败）| **立即 exit 2**（拒绝）| HMIL 底线：桥断了放行 = HMIL 失效 |
| 超时（默认 60s，`ORCA_GATE_TIMEOUT` 可配）| **exit 2**（拒绝）| 宁可 workflow 卡住等用户重试，不误放行危险工具 |
| HTTP 响应是拒绝 | exit 2 | — |
| HTTP 响应是批准 | exit 0 | — |

**权衡记录**：可用性 vs 安全。HMIL 本质是「危险操作要人确认」，桥断了放行 = 最坏情况（误删文件不可逆）。workflow 卡住是可接受的（用户能重试）。代价：Orca 挂了 workflow 会卡，但这是可接受的安全代价。

### 3.4 hook 脚本（Orca 提供）

Orca 提供一个 hook 脚本（`orca/gates/hook_script.py` 或 shell），用户配置到 `.claude/settings.json` 的 PreToolUse。脚本逻辑：
1. 读 stdin JSON（hook 的标准输入）。
2. `POST http://localhost:<ORCA_PORT>/gate`，body = stdin JSON，timeout = `ORCA_GATE_TIMEOUT`（默认 60s）。
3. 根据响应 `decision`（"allow" / "deny"）exit 0 / 2。
4. 任何异常（连接失败 / 超时 / 响应非法）→ exit 2（安全优先）。

### 3.5 Orca server 的 /gate 端点（phase 6 提供，壳共用）

```python
# orca/gates/http_endpoint.py（被 phase 9 web server 复用）
@app.post("/gate")
async def gate_endpoint(hook_payload: dict):
    """hook → POST /gate。构造 HumanGate → handler.request → 返回决策。"""
    gate = HumanGate(
        id=uuid4().hex,
        prompt=f"批准 {hook_payload['tool_name']} 调用？",
        options=["allow", "deny"],  # 固定选项（hook 语义只有允许/拒绝）
        context={"tool": hook_payload["tool_name"], "tool_input": hook_payload["tool_input"], ...},
        source="tool_permission",
        run_id=_current_run_id(),
        node=_current_node(),
    )
    answer, source = await gate_handler.request(gate)
    return {"decision": "allow" if answer == "allow" else "deny", "resolved_by": source}
```

> **注意**：`/gate` 端点的「当前 run_id / node」从哪来？hook 是 claude spawn 的独立进程，不知道 Orca 的 run 上下文。解决方案：hook stdin 的 `session_id` 关联到 Orca 的 session_id（phase 4 executor 生成的）。Orca 维护 `session_id → (run_id, node)` 映射（orchestrator 在 spawn claude 时记录）。详见 §6 映射表。

---

## 4. 三通道竞速（广播语义，2026-06-30 定稿）

### 4.1 广播机制

任一壳 resolve → gate 立即变 resolved → **广播**给所有订阅该 gate 的壳。

```python
# orca/gates/handler.py（续 §2.1）
async def _broadcaster(self):
    """后台协程：监听 resolved 的 gate，emit human_decision_resolved（广播）。
    resolve() 只唤醒 request()；广播由本协程统一 emit（避免 resolve 阻塞）。"""
    while True:
        gate_id, answer, source = await self._resolved_queue.get()
        gate = self._gates_meta[gate_id]  # 缓存的 gate 元信息（run_id/node）
        await self._bus.emit(
            "human_decision_resolved",
            data={"gate_id": gate_id, "answer": answer, "resolved_by": source},
            node=gate.node,
        )
```

**流程**：
1. `request(gate)` → emit `human_decision_requested`（写 tape + 三壳订阅者收到）。
2. 三壳各自渲染 gate（CLI ModalScreen / Web 弹窗 / MCP status=needs_decision）。
3. 任一壳用户答 → 调 `resolve(gate_id, answer, source)`。
4. `resolve` set_result 唤醒 `request` 的 await + 把 `(gate_id, answer, source)` 入 `_resolved_queue`。
5. `_broadcaster` 取出 → emit `human_decision_resolved`（写 tape + 三壳订阅者收到广播）。
6. 三壳收到 `human_decision_resolved` → 关闭自己的 gate UI，显示「已被 [source] 答：[answer]」。
7. orchestrator 的 `request` 返回 `(answer, source)`，node 内的 claude resume。

### 4.2 为什么是广播（不是纯取消）

- **视觉同步**：不会出现「Web 已答了 CLI 还在傻等」。所有壳收到 resolved 事件后同步关闭。
- **唯一真相**：resolved 状态写 tape，所有壳从同一份 `human_decision_resolved` 读，必然一致。
- **晚到输入 fail loud**：resolved 后的 resolve → 返回 False + warning（可见，不静默）。

### 4.3 各壳的 resolve 路径（壳实现，phase 7/9/10）

| 壳 | resolve 来源 | 调用 |
|---|---|---|
| CLI | Textual ModalScreen 用户答 | `gate_handler.resolve(gate_id, answer, "cli")` |
| Web | 浏览器点按钮 → HTTP POST /gate/respond | HTTP handler → `gate_handler.resolve(...)` |
| MCP | Claude 调 `resolve_gate` tool | MCP handler → `gate_handler.resolve(...)` |

**广播接收**（各壳订阅 `human_decision_resolved` 事件）：
- CLI：ModalScreen 收到事件 → 自动 dismiss + 显示「已被 [source] 答」。
- Web：弹窗收到事件 → 自动关闭 + 显示。
- MCP：`resolve_gate` 返回 `{ok: false, _hint: "already resolved by [source]"}`。

---

## 5. ask_user MCP 工具（agent 主动问，第二个来源）

### 5.1 定位

agent 在执行中可能需要主动问用户（如「需要数据库连接串」）。机制：claude 调一个 MCP 工具 `ask_user` → 触发 HumanGate（source=agent_ask）。

### 5.2 工具签名（占位实现，phase 6 提供壳的 resolve 路径；MCP server 完整实现在 phase 10）

```python
# orca/gates/ask_user.py（phase 6 提供 handler 接入；MCP 工具注册归 phase 10）
async def ask_user(handler: HumanGateHandler, prompt: str, options: list[str] | None = None,
                   context: dict | None = None, run_id: str = "", node: str | None = None) -> str:
    """agent 主动问用户。触发 HumanGate(source=agent_ask)，等任一壳答。"""
    gate = HumanGate(
        id=uuid4().hex, prompt=prompt, options=options,
        context=context or {}, source="agent_ask",
        run_id=run_id, node=node,
    )
    answer, _source = await handler.request(gate)
    return answer
```

**phase 6 范围**：提供 `ask_user` 函数（接 handler）+ 测试（触发 agent_ask gate，壳 resolve）。**MCP 工具注册**（让 claude 能调 `ask_user`）归 phase 10 MCP 壳。

---

## 6. session_id → run_id/node 映射（hook 桥定位用）

hook 是独立进程，不知道 Orca 的 run 上下文，但 hook stdin 含 claude 的 `session_id`。Orca executor（phase 4）生成 session_id 时，需建立映射：

```python
# orca/gates/context_registry.py
class SessionContextRegistry:
    """session_id → (run_id, node) 映射。orchestrator spawn claude 时注册，hook 桥查询。"""
    def register(self, session_id: str, run_id: str, node: str) -> None: ...
    def lookup(self, session_id: str) -> tuple[str, str] | None: ...
    def unregister(self, session_id: str) -> None: ...  # node 完成时清理
```

> **关键约束**：Orca executor 生成的 session_id（phase 4）与 claude 流里的 session_id **不同**（phase 4 SPEC §3.2 决策 5）。hook stdin 的是 **claude 的 session_id**。因此 hook 桥需要 claude session_id → Orca 上下文的映射。**实现时**：spawn claude 后，从 `system/init` 事件（fixture 行 3，含 claude 的 session_id）提取，注册到映射表。详见开发计划 §session 映射 task。

---

## 7. 验收标准

### 7.0 验收总则（5 条铁律）
1. **gate 事件写 tape**：requested/resolved 都 emit 到 EventBus 写 tape（唯一真相）。
2. **三壳读同一份 tape**：无第二份 gate 状态存储。
3. **依赖单向**：gates → events + schema，不依赖 run/exec/iface。
4. **安全优先**：hook 桥超时/不可达 = 拒绝（exit 2），绝不放行。
5. **广播语义**：任一壳 resolve → 全壳收到 resolved 事件（视觉同步）。

### 7.1 HumanGate 原语
- [ ] frozen dataclass，含 id/prompt/options/context/source/run_id/node
- [ ] source 仅 "tool_permission" | "agent_ask"
- [ ] 两个 source 共用同一模型，仅 context 内容不同

### 7.2 HumanGateHandler
- [ ] `request(gate)` emit `human_decision_requested` 写 tape + await 等 resolve
- [ ] `resolve(gate_id, answer, source)` 唤醒 request，返回是否赢家
- [ ] 已 resolved 的 resolve → False + warning（fail loud）
- [ ] gate 无限等（无 timeout）
- [ ] 纯逻辑测试：request/resolve 不依赖具体壳

### 7.3 三通道竞速（广播）
- [ ] 多个「壳」（测试用 fake resolver）同时 resolve，只有赢家返回 True
- [ ] resolved 后 emit `human_decision_resolved`（广播）
- [ ] 输家（晚到 resolve）返回 False + warning
- [ ] request 返回 (answer, source) 正确

### 7.4 PreToolUse hook 桥
- [ ] hook 脚本能 POST /gate 并解析响应
- [ ] Orca server /gate 端点构造 HumanGate(source=tool_permission) → handler.request
- [ ] **安全语义**：Orca 不可达 → hook exit 2；超时 → exit 2；批准 → exit 0；拒绝 → exit 2
- [ ] hook stdin 的 session_id → Orca 上下文映射正确

### 7.5 ask_user（agent 主动问）
- [ ] `ask_user(handler, prompt, options)` 触发 HumanGate(source=agent_ask)
- [ ] 壳 resolve 后 ask_user 返回 answer

### 7.6 端到端（不依赖壳的具体渲染）
- [ ] 模拟「claude 想调工具」→ hook 桥 → gate → fake 壳 resolve → claude resume（mock claude）
- [ ] 模拟「agent ask_user」→ gate → fake 壳 resolve → 返回 answer
- [ ] 竞速：两个 fake 壳，第一个 resolve 赢，第二个 fail loud

### 7.7 测试
- [ ] `tests/gates/test_handler.py`：request/resolve/竞速/广播/fail loud
- [ ] `tests/gates/test_hook_bridge.py`：hook 脚本 + /gate 端点 + 安全语义（用 TestClient，不真 spawn claude）
- [ ] `tests/gates/test_ask_user.py`：agent_ask gate
- [ ] `tests/gates/test_context_registry.py`：session_id 映射
- [ ] 全部通过（含 phase 1-5 不回归）

---

## 8. 给后续阶段的契约

| 后续 | phase 6 提供 |
|---|---|
| phase 7 cli | `HumanGateHandler`（CLI 壳调 resolve）+ gate 事件订阅 |
| phase 9 web | `/gate` + `/gate/respond` 端点（web server 复用）+ HumanGateHandler |
| phase 10 mcp | `ask_user` 函数 + `resolve_gate` 接入 handler（MCP 工具注册归 phase 10）|

---

## 9. 不做的事

- ❌ **壳的具体渲染**（CLI ModalScreen / Web 弹窗 / MCP tool 注册）—— phase 7/9/10
- ❌ **真 spawn claude 测 hook**（用 TestClient + mock）—— integration 测试归后续
- ❌ ** elicitation**（CC 未支持）—— 不做
- ❌ **gate 持久化恢复**（崩溃后未 resolved 的 gate 怎么办）—— 后续（phase 6 gate 写 tape，但崩溃恢复语义留后）
- ❌ **三壳同时真跑竞速**（单壳能 resolve 即可，三壳并发竞速的端到端测试归 phase 7+9 集成）

---

## 10. 关键决策备忘（防 drift）

1. **HumanGate 统一两个来源**（tool_permission / agent_ask），source 仅驱动渲染
2. **gate 事件写 tape**（requested/resolved），唯一真相，三壳读同一份
3. **request 是 async**（await emit + await fut），resolve 同步非阻塞
4. **gate 无限等**（语义层无 timeout），超时只在 hook 桥传输层
5. **hook 桥安全优先**（超时/不可达 = exit 2，绝不放行）
6. **三通道竞速 = 广播**（任一 resolve → 全壳收 resolved 事件，视觉同步）
7. **晚到 resolve = fail loud**（False + warning，不静默吞）
8. **gates 不依赖 run/exec/iface**（依赖单向：gates → events + schema）
9. **gate 不打断编排拓扑**，只打断单个 node 内的 claude 工具调用循环
10. **session_id 映射**（claude session_id → Orca run_id/node），hook 桥定位用
11. **广播由 _broadcaster 协程统一 emit**（resolve 不直接 emit，避免阻塞）
