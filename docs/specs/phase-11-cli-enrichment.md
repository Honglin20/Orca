# 阶段 11 SPEC —— CLI feature 补全（中断 / Guidance / ask_user / Dialog / Resume / Daemon）

> **状态**：最终版（待分发实现）
> **依据**：[phase-3-events.md](phase-3-events.md) §3 §6 · [phase-4-exec.md](phase-4-exec.md) §2 §5 · [phase-6-gates.md](phase-6-gates.md) §1-5 · [phase-7-cli.md](phase-7-cli.md) §3 §6 · [conductor-gap-analysis](../specs/conductor-gap-analysis.md)（如有）
> **范围**：在 CLI/后端层面补齐 Conductor 已有的 6 类核心 feature（不含 Web，Web 单列后续 phase）
> **前置**：phase 1-10 全部完成（master 已合并）
> **里程碑**：phase 11 完成后 Orca 在 CLI 场景下达到 Conductor 等量功能水平

---

## 0. 阶段目标

phase 11 回答一个问题：**「与 Conductor 相比，CLI 路径下还差什么？补齐到等量水平。」**

通过迁移 mxint-analysis 实测 + 三向客观对比，已识别 6 类真实 gap。本 phase 系统化补齐，**保留 Orca 单 Tape + 纯 reducer + 单向依赖架构**，不动核心。

| 子任务 | 解决什么 | 优先级 | 成本 | Conductor 参考 |
|---|---|---|---|---|
| CI | 生产就绪硬要求，防回归 | P0 | 1-2 天 | `.github/workflows/` |
| **Retry Policy**（review 补充）| API 429/500/transient 错误自动重试，鲁棒性基础设施 | **P0** | 3-5 天 | `config/schema.py:402-451` |
| 优雅中断 UI | 长跑 workflow 想纠偏，Ctrl+C 硬杀体验差 | P1 | 3-5 天 | `gates/interrupt.py:59-290` |
| mid-run Guidance 注入 | 中断后追加一句话给后续 agent | P1 | 1 周 | `engine/context.py:115-140` |
| ask_user 工具挂载 | 让被编排的 claude agent 能主动问 user | P1 | 3-5 天 | `mcp/manager.py:33-150`（机制参考）|
| **Semantic Output Validator**（review 补充）| LLM 二次校验 agent 输出语义质量（非 shape/type）| **P2** | 1 周 | `engine/validator.py:1-267` |
| Dialog | agent 跑完后多轮对话 | P2 | 2 周 | `gates/dialog.py:131-359` |
| Checkpoint Resume | 崩溃后续跑（Orca 优势：Tape 即 checkpoint）| P2 | 1 周 | `engine/checkpoint.py:171-442` |
| **Wait Node**（review 补充）| `type: wait` 节点，asyncio.sleep + 可被 interrupt 打断 | **P3** | 3-5 天 | `executor/wait.py:60-193` |
| daemon `--background` | 长跑不占终端 + 多 run 并发 | P3 | 1-2 周 | `cli/bg_runner.py` |
| Skip to Agent | debug 场景跳转 node | P4 | 1 周 | `gates/interrupt.py:236-289` |

**总时间预算**：6-9 周完成 P0-P2（核心闭环，含 review 补充的 Retry / Validator），P3-P4 可滚动。

### 0.1 锁定的核心决策（不再讨论）

1. **保持 `claude -p` CLI 子进程路线，不切 Claude Agent SDK**（详见 §1.1）
2. **Web feature 推迟**（本 phase 只动 CLI + 后端）
3. **多 backend（vendor-neutral）推迟**（用户已说不优先）
4. **token 级流不打折**（-p 路线的独家优势保留）
5. **每个 feature 完成后必须实跑 `orca run` 验证**（学 mxint_analysis 经验）

### 0.2 与既有架构的关系（铁律不变）

- **单 Tape 唯一真相源**：所有新 feature 的事件都写 Tape，三壳统一读
- **单向依赖**：`schema/run/exec/events/iface` 严格分层，新 feature 加在 `gates/` / `run/` / `iface/cli/`
- **fail loud**：边界 / 失败路径显式 raise，不静默吞错
- **DRY**：guidance 累积 / interrupt handler / dialog 历史等共享 RunContext 字段，不另起 stovepipe

---

## 1. 关键技术决策

### 1.1 为什么保持 `claude -p`，不切 Claude Agent SDK

**结论**：切 SDK 的唯一收益是「ask_user 自定义工具实现略简」（直接 Python 函数 vs MCP 包装，省 ~30 行），代价是「2-3 周重写 ClaudeExecutor + 1-2 周修 654 个测试 + ccr code 兼容性风险」。**ROI 极低，不切**。

**实证依据**：

| 论点 | 证据 |
|---|---|
| Conductor 用 SDK 也是重 spawn 实现 guidance | `engine/workflow.py:2258` `accumulated_guidance=list(self.context.user_guidance)` |
| Conductor Dialog 也是重 spawn + 拼历史 | `gates/dialog.py:261` `Track conversation history for the provider` |
| SDK 也能复用 Claude Code 内置工具 | [Agent SDK docs](https://code.claude.com/docs/en/agent-sdk/python) + [Issue #215](https://github.com/anthropics/claude-agent-sdk-python/issues/215) —— 但 **-p 也完整复用**（claude -p 自带 Bash/Read/Edit/Grep/Glob/TodoWrite/WebSearch） |
| 用户当前用 ccr code 中转 | env `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` 已设；ccr 是 CLI 协议代理，-p 完美兼容；SDK 走 API proxy 模式需重新验证 |

### 1.2 mid-run Guidance 的实现路线（与 Conductor 一致）

**Conductor 的真实做法**（`engine/context.py:115-140` 实证）：

```python
user_guidance: list[str] = field(default_factory=list)

def add_guidance(self, text: str) -> None:
    self.user_guidance.append(text)

def get_guidance_prompt_section(self) -> str | None:
    if not self.user_guidance:
        return None
    entries = "\n".join(f"- {g}" for g in self.user_guidance)
    return (
        "\n\n[User Guidance]\n"
        "The following guidance was provided by the user during workflow execution. "
        "Incorporate this guidance into your response:\n"
        f"{entries}"
    )
```

**关键**：guidance **不是** SDK session 保活，而是「重 spawn 时拼进 prompt」。**-p 路线完全等价**。

**Orca 实现路径**（详见 §3）：

1. 用户 Ctrl+G → InterruptModal（含 guidance textarea）→ CONTINUE
2. SIGINT 杀当前 claude -p 子进程
3. Orchestrator 在 node 边界检查 interrupt 状态
4. `ctx.user_guidance.append(text)`，emit `interrupt_resolved` 写 Tape
5. 同一 node 重 spawn 时，`render_prompt` 把 `get_guidance_prompt_section()` 拼到 prompt 末尾

**失去的**：claude 的 in-process 上下文（Orca 本来就是 node 边界 spawn，没有 in-process 上下文）。
**保留的**：tape 历史 + node 间 outputs + workflow 进度。

### 1.3 ask_user 的实现路线（不切 SDK）

`claude -p` 通过 `--mcp-config <path>` 加载 MCP server（`claude --help` 实证），Orca 已留接口（原计划 phase 9 填充 `mcp_flag_args`，phase 11 提前填肉——review C10）：

```python
# orca/exec/runner.py:72
@dataclass
class SpawnConfig:
    ...
    mcp_flag_args: list[str] = field(default_factory=list)  # 当前空，本 phase 填肉

# orca/profiles/builtin/claude.py:57
PROFILE = CliProfile(
    ...
    mcp_flag_template="--mcp-config {path}",
    ...
)
```

**实现路径**（详见 §4）：

1. orca 进程内嵌一个 socket MCP server（监听 loopback port）
2. 注册 `ask_user` MCP 工具（直接调 `orca.gates.ask_user(handler, ...)`）
3. `ClaudeExecutor._build_spawn_config` 把 socket config 写成临时 JSON，填到 `mcp_flag_args`
4. claude -p 启动时通过 `--mcp-config` 加载，调 ask_user 即触发 HumanGate
5. session_id 路由：复用 `SessionContextRegistry`（phase 6 已实现给 hook 桥用）

### 1.4 Checkpoint Resume 的 Orca 优势

**Conductor 的 checkpoint 是独立系统**（`engine/checkpoint.py` 400+ 行）——需要单独序列化 workflow 状态。

**Orca 的 Tape 天然就是 checkpoint**——append-only JSONL 已经记录了所有历史。**只需要一个 `orca resume` 命令读 Tape 重放到崩溃前位置 + 从该 node 续跑**。

工作量：1 周（vs Conductor 4 周）。

### 1.5 daemon 模式与 `claude --bg` 的区别

`claude --bg` 是 claude CLI 自己的后台 agent（fire-and-forget 异步任务），与 Orca 编排无关。

Orca daemon 是「`orca run` 进程脱离终端继续跑」，配合 `attach/logs/ps` 子命令。两者正交。

---

## 2. 共享机制扩展（前置依赖）

### 2.1 RunContext 扩展：累积状态字段

**文件**：`orca/exec/context.py`（已存在，本 phase 加字段）

```python
@dataclass(frozen=True)
class RunContext:
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    run_id: str
    task: str | None
    # ── phase 11 新增 ──────────────────────────────────────
    user_guidance: tuple[str, ...] = ()       # 累积 guidance（frozen 用 tuple）
    interrupt_history: tuple[dict, ...] = ()  # 历次中断记录（debug/replay 用）
    dialog_history: tuple[dict, ...] = ()     # Dialog 多轮历史（agent 跑完后）
```

**为什么 frozen 用 tuple**：RunContext 已是 frozen（phase 4 设计），不可变。累积状态用 tuple + `dataclasses.replace` 派生新实例（与 `outputs` 累加机制一致）。

**为什么三个字段独立**：语义不同（guidance 是 prompt 注入 / interrupt 是控制流 / dialog 是 post-run 对话），不强合并。

### 2.2 新增事件类型

**文件**：`orca/schema/event.py`（EventType Literal 加 6 + 8 = 14 个）

```python
EventType = Literal[
    ...,
    # ── phase 11 新增（基础 6 个）──────────────────────────
    "interrupt_requested",      # data: {reason, node, source}
    "interrupt_resolved",       # data: {action: continue|skip|abort, guidance: str?, node}
    "dialog_started",           # data: {node, session_id, initial_prompt}
    "dialog_message",           # data: {role: user|agent, text, turn}
    "dialog_ended",             # data: {node, total_turns, conclusion}
    "workflow_resumed",         # data: {from_tape, resumed_node, replayed_events}
    "prompt_rendered",          # data: {node, session_id, preview: str（末尾 ~200 字符，含 [User Guidance] 段）}  review B5：guidance 注入的可观测证据
    # ── phase 11 review 补充（8 个：Retry 3 + Validator 3 + Wait 2）─────
    "retry_started",            # data: {attempt, max_attempts, error_type, delay_seconds, node}
    "retry_succeeded",          # data: {attempt_total, node}
    "retry_exhausted",          # data: {attempts, last_error_type, node}
    "validator_started",        # data: {node, criteria_preview}
    "validator_passed",         # data: {node, issues: []}
    "validator_failed",         # data: {node, issues: [str], retrying: bool}
    "wait_started",             # data: {node, duration_seconds, reason}
    "wait_completed",           # data: {node, elapsed_seconds, interrupted: bool}
]
```

**事件写 Tape**（不变量保持）：所有新事件都经 EventBus.emit 写 Tape，三壳统一从 Tape 重放。

### 2.3 Orchestrator 扩展：interrupt checkpoint

**文件**：`orca/run/orchestrator.py`（`_drive_loop` 加 interrupt 检查）

**公开通道（review 修正 测试A）**：CLI 层通过 orchestrator 的**公开方法** `request_interrupt` 设置 pending，**不**直接 mutate 私有属性、**不**经任何 `_orchestrator_proxy`：

```python
def request_interrupt(self, ireq: InterruptRequest) -> None:
    """CLI 层（InterruptModal dismiss 后）调此方法登记一次中断请求。

    幂等：重复登记同一 run 仅保留最新一条（node 边界只消费一次）。
    """
    self._interrupt_pending = ireq
```

`outputs` 派生继续走 orchestrator 既有的 `_make_ctx(outputs_acc)` 构造路径，**不新增** `RunContext.with_outputs`（review 修正 C3）。

```python
async def _drive_loop(self):
    current = self.wf.entry
    outputs_acc = {}
    while current != "$end":
        # ── phase 11：node 边界检查 interrupt ──────────────────
        if self._interrupt_pending:
            action, guidance = await self._handle_interrupt(current)
            if action == "abort":
                raise WorkflowAborted(current)
            if action == "skip":
                current = self._skip_to_next(current, outputs_acc)
                continue
            # continue: 把 guidance 累积到 ctx
            ctx = ctx_with_guidance(ctx, guidance)
        # ──────────────────────────────────────────────
        step_ctx = self._make_ctx(outputs_acc)
        raw_output = await self._dispatch(current, step_ctx)
        outputs_acc[current] = {"output": raw_output}
        ...
```

**node 边界检查**（不是工具调用中）：-p 路线限制（无法中断工具调用），但 Conductor SDK 也只在 message 间（同样粒度）—— 实测够用。

### 2.4 CLI App 扩展：键位绑定 + InterruptModal 注册

**文件**：`orca/iface/cli/app.py`

```python
BINDINGS = [
    Binding("q", "quit", "退出"),
    Binding("g", "goto_gate", "跳到 gate"),
    # ── phase 11 新增 ──────────────────────────────────────
    Binding("ctrl+g", "interrupt", "中断/纠偏"),  # 弹 InterruptModal
    Binding("d", "dialog", "对话"),               # node 完成后弹 DialogModal
]
```

---

## 3. P0.2 优雅中断 UI（CONTINUE / SKIP / ABORT）

### 3.1 用户流程

```
workflow 跑到 node=configurator（已 30s）
  ↓
用户按 Ctrl+G
  ↓
InterruptModal 弹出（DAG 继续跑，ModalScreen 阻塞主进程响应）
  ┌──────────────────────────────────────────┐
  │ ⏸ INTERRUPT · node=configurator          │
  │ ───────────────────────────────────      │
  │ 当前 node 已跑 30s（猜测剩余 90s）         │
  │                                          │
  │ Guidance（可选，CONTINUE 时拼进 prompt）：│
  │ ┌────────────────────────────────────┐   │
  │ │ 用更保守的方案                       │   │
  │ └────────────────────────────────────┘   │
  │                                          │
  │ [CONTINUE]  [SKIP]  [ABORT]              │
  └──────────────────────────────────────────┘
  ↓
用户输入 guidance + 选 CONTINUE
  ↓
InterruptModal dismiss → handler.resolve(interrupt_id, action, guidance)
  ↓
Orchestrator _drive_loop 在 node 边界收到 interrupt_pending
  ↓ SIGINT 杀当前 claude -p
  ↓ 同 node 重 spawn（prompt 含 guidance section）
  ↓ emit interrupt_resolved（写 Tape）
```

### 3.2 数据契约

**InterruptRequest**（`orca/gates/types.py` 加类）：

```python
class InterruptRequest(BaseModel):
    id: str                       # gate_id 同算法
    node: str                     # 哪个 node 触发的
    run_id: str
    session_id: str | None        # 当前 claude session
    source: Literal["cli", "web", "mcp"] = "cli"
    elapsed_at_request: float     # 中断时已耗时
    context: dict = {}            # 任意附加（如 node prompt 摘要）
```

**事件 payload**：

| 事件 | data |
|---|---|
| `interrupt_requested` | `{interrupt_id, node, run_id, session_id, elapsed_at_request, source}` |
| `interrupt_resolved` | `{interrupt_id, action: "continue"\|"skip"\|"abort", guidance: str\|null, resolved_by}` |

### 3.3 接口（`orca/gates/interrupt.py` 新建）

```python
class InterruptHandler:
    """累积中断请求 + 等用户响应 + 广播 resolved。"""

    def __init__(self, bus: EventBus):
        self._bus = bus
        self._pending: dict[str, asyncio.Future[tuple[str, str | None]]] = {}
        self._lock = asyncio.Lock()
        self._resolve_lock = threading.Lock()
        self._resolved_queue: asyncio.Queue | None = None
        self._broadcaster_task: asyncio.Task | None = None

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def request(self, ireq: InterruptRequest) -> tuple[str, str | None]:
        """emit interrupt_requested + 等用户答。返回 (action, guidance)。"""
    def resolve(self, interrupt_id: str, action: str, guidance: str | None, source: str) -> bool: ...
```

**与 HumanGateHandler 的关系**：独立类（语义不同 —— gate 是「等决策」，interrupt 是「等用户意图」），但**共享 broadcaster pattern**（DRY：抽 `orca/gates/_broadcaster_mixin.py` 共享 start/stop/_broadcaster 三方法，HumanGateHandler 和 InterruptHandler 都继承）。

---

## 4. P1.1 mid-run Guidance 注入

### 4.1 实现路径（依赖 §3 完成）

**RunContext 扩展**（详见 §2.1）：

```python
user_guidance: tuple[str, ...] = ()

def with_guidance(self, text: str) -> "RunContext":
    return dataclasses.replace(self, user_guidance=self.user_guidance + (text,))

def guidance_prompt_section(self) -> str | None:
    if not self.user_guidance:
        return None
    entries = "\n".join(f"- {g}" for g in self.user_guidance)
    return (
        "\n\n[User Guidance]\n"
        "The following guidance was provided by the user during workflow execution. "
        "Incorporate this guidance into your response:\n"
        f"{entries}"
    )
```

**render_prompt 注入**（`orca/exec/render.py:render_prompt`）：

```python
def render_prompt(node, ctx: RunContext) -> str:
    # 既有：node.prompt 非空 → 内联短 prompt 渲染；None → 加载 agents/<name>.md
    base_prompt = ...  # 既有逻辑

    # ── phase 11 新增：拼 guidance section ──
    guidance_section = ctx.guidance_prompt_section()
    if guidance_section:
        return base_prompt + guidance_section
    return base_prompt
```

**Orchestrator 中断处理**（`orca/run/orchestrator.py`）：

```python
async def _handle_interrupt(self, current_node, ctx):
    ireq = InterruptRequest(
        id=uuid4().hex,
        node=current_node,
        run_id=self.run_id,
        session_id=...,
        elapsed_at_request=...,
    )
    emit("interrupt_requested", ireq)
    action, guidance = await self._interrupt_handler.request(ireq)

    # 杀当前 claude -p（如果有）
    if self._current_runner:
        self._current_runner.send_sigint()

    if action == "continue":
        if guidance:
            ctx = ctx.with_guidance(guidance)
        emit("interrupt_resolved", {"interrupt_id": ireq.id, "action": "continue",
                                     "guidance": guidance, "resolved_by": ...})
        return "continue", ctx
    elif action == "skip":
        emit("interrupt_resolved", {"interrupt_id": ireq.id, "action": "skip", ...})
        return "skip", None
    else:  # abort
        emit("interrupt_resolved", {"interrupt_id": ireq.id, "action": "abort", ...})
        return "abort", None
```

### 4.2 中断后的 claude -p 处理

**`orca/exec/runner.py::CLIRunner` 扩展**：

```python
class CLIRunner:
    ...
    def send_sigint(self) -> None:
        """向子进程发 SIGINT（用户 Ctrl+G 触发 interrupt 时调）。

        -p 路线：SIGINT 让 claude 优雅退出（写最后的 stream-json result 行）。
        比 kill -9 友好（不丢失 buffered output）。
        """
        if self._proc and self._proc.returncode is None:
            self._proc.send_signal(signal.SIGINT)
```

**node 边界语义**：SIGINT 后 Orchestrator 不立刻 fail node，而是等子进程退出 → 标记 node 为「interrupted」→ 在 _drive_loop 顶部检查 _interrupt_pending → 决定 continue/skip/abort。

### 4.3 重 spawn 时 prompt 拼接示例

第一次 spawn configurator 的 prompt：

```
你接收 analyzer 的分析结果，生成可运行的诊断 adapter + CLI 命令...
```

用户 Ctrl+G + guidance「用更保守的方案」+ CONTINUE 后，重 spawn 的 prompt：

```
你接收 analyzer 的分析结果，生成可运行的诊断 adapter + CLI 命令...

[User Guidance]
The following guidance was provided by the user during workflow execution. Incorporate this guidance into your response:
- 用更保守的方案
```

---

## 5. P1.2 ask_user 工具挂载（被编排 claude 用）

### 5.1 架构（不切 SDK，socket MCP server）

```
                orca 进程（1 个）
                  ├── RunManager
                  ├── HumanGateHandler（既有）
                  ├── InterruptHandler（phase 11 §3 新增）
                  ├── EventBus / Tape（既有）
                  └── AgentToolsMcpServer（phase 11 §5 新增，监听 loopback :7422）
                            ▲
              ┌─────────────┼─────────────┐
              │             │             │
        claude -p #1   claude -p #2   claude -p #3   （被 orca 编排的子进程）
        (analyzer)     (configurator) (runner)
              │             │             │
              └─────────────┴─────────────┘
                    通过 --mcp-config 连同一个 server
                    每次调用带 session_id 路由到正确 gate
```

**MCP server 部署**：进程内 1 个实例，监听 1 个 loopback port，所有 claude -p 共享。**懒启动**：第一个 agent spawn 时才起，workflow 跑完关闭。

### 5.2 数据契约

**AgentToolsMcpServer 工具清单**（初始只 1 个，render_chart 后续 phase 加）：

| 工具名 | 入参 | 返回 | 实现 |
|---|---|---|---|
| `ask_user` | `prompt: str, options: list[str]?` | `answer: str` | 调 `orca.gates.ask_user(handler, ...)` |

**MCP config 临时文件**（每个 claude -p spawn 时写）：

```json
{
  "mcpServers": {
    "orca-agent-tools": {
      "type": "sse",
      "url": "http://127.0.0.1:7422/sse"
    }
  }
}
```

写到 `runs/<run_id>/mcp_config_<session_id>.json`，spawn 完后删除。

### 5.3 接口（`orca/exec/mcp_tools/server.py` 新建）

> **import（review 修正 C2）**：用 `from mcp.server.fastmcp import FastMCP`（与 phase 10 `OrcaMcpServer` 同款，mcp SDK 自带），**不是**第三方 `fastmcp` 包、**不是** `fastmcp.testing`。
>
> **路由设计（review 修正 item4 + 决策 D4）**：**确定性 tool-params 路由**，不依赖 MCP session 标识。`ask_user` 工具入参强制带 `_orca_run_id` / `_orca_node`（hidden params），由 `render_prompt` 在 agent prompt 里 instruct claude 调用时必填。这比依赖 MCP session 更可靠（claude -p 不主动报 MCP session），与 SSE spike 结果解耦。
>
> **SSE 连接 spike（前置）**：P1.2 开工前必须跑通最小验证——`mcp.server.fastmcp` 起 SSE server → claude -p 经 `--mcp-config` 连上 → 成功调一次 `ask_user`。spike 失败则 ask_user feature **整体推迟**（不接受 stdio N-子进程 fallback，review C9 已证不可行）。

```python
from mcp.server.fastmcp import FastMCP   # 与 phase 10 一致

class AgentToolsMcpServer:
    """内嵌 socket MCP server（FastMCP SSE），暴露 ask_user 等工具给被 orca 编排的 claude -p。

    与 phase 10 OrcaMcpServer 的区别：
    - phase 10 OrcaMcpServer：stdio MCP，给外部 CC 主对话用，暴露 start_workflow 等
    - 本类 AgentToolsMcpServer：socket SSE MCP，给 orca 内部 spawn 的 claude 用，暴露 ask_user 等
    """

    def __init__(self, handler: HumanGateHandler, registry: SessionContextRegistry):
        self._handler = handler
        self._registry = registry
        self._mcp = FastMCP("orca-agent-tools")
        self._server_task: asyncio.Task | None = None
        self._port: int | None = None

    async def start(self) -> int:
        """启动 socket MCP server，返回 port。懒启动：第一次 agent spawn 时调。"""
        ...

    async def stop(self) -> None: ...

    def write_config(self, session_id: str, run_id: str, node: str) -> Path:
        """写临时 mcp config JSON，给 claude -p 的 --mcp-config flag 用。"""
        ...

    @property
    def port(self) -> int | None: ...
```

**ask_user 工具签名（确定性路由）**：

```python
@self._mcp.tool()
async def ask_user(
    prompt: str,
    options: list[str] | None = None,
    _orca_run_id: str = "",   # hidden, render_prompt instructs claude 必填
    _orca_node: str = "",     # hidden
) -> str:
    """Ask the user a question. Blocks until user answers."""
    if not _orca_run_id or not _orca_node:
        raise RuntimeError("ask_user missing _orca_run_id/_orca_node routing params")
    answer, _ = await ask_user(
        handler=self._handler, prompt=prompt, options=options,
        run_id=_orca_run_id, node=_orca_node,
        session_id=f"{_orca_run_id}:{_orca_node}",
    )
    return answer
```

### 5.4 ClaudeExecutor 集成

**make_executor 注入路径（review 修正 C4）**：`make_executor` 加可选第二参数，仅 agent 分支透传，script/set/foreach/wait 分支忽略：

```python
def make_executor(
    node: Node, agent_tools_server: AgentToolsMcpServer | None = None,
) -> Executor:
    if node.kind == "agent":
        return ClaudeExecutor(profile=..., agent_tools_server=agent_tools_server)
    if node.kind == "script": return ScriptExecutor()
    ...
```

**`orca/exec/claude/executor.py::_build_spawn_config` 修改**：

```python
def _build_spawn_config(node, profile, prompt, ctx, agent_tools_server):
    extra_args = []
    ...
    # ── phase 11：动态填 mcp_flag_args ──────────────────
    mcp_flag_args = []
    if agent_tools_server is not None:
        config_path = agent_tools_server.write_config(
            session_id=ctx.session_id, run_id=ctx.run_id, node=node.name,
        )
        mcp_flag_args = ["--mcp-config", str(config_path)]
    return SpawnConfig(..., mcp_flag_args=mcp_flag_args, ...)
```

**ClaudeExecutor 路由 mcp_flag_args 透传到 SpawnConfig**（已留接口，只填值）。

### 5.5 路由与会话登记（review 修正 item4 + B2 + 决策 D4）

**路由**：见 §5.3——`ask_user` 用 `_orca_run_id` / `_orca_node` 工具入参确定性路由，**不**依赖 MCP session 反查。`lookup_by_mcp_session` 设计**删除**（claude -p 不主动报 MCP session，假设不成立）。

**phase 6 遗留债（必须先还，review B2）**：`SessionContextRegistry.register(claude_session_id, run_id, node)` 的调用方**当前缺失**（phase 6 SPEC 约定但未接线）。phase 11 P1.2 必须先在 ClaudeExecutor spawn claude 成功、拿到 claude session_id 后调 `registry.register(...)`，HumanGateHandler 才能 `lookup(session_id)` 把 gate 答案送回正确 agent。**这是 ask_user 闭环的前置依赖，不补则 gate 答案无法回流。**

**gates.RunContext 重命名（review B2）**：`orca/gates/context_registry.py` 的 `RunContext`（NamedTuple）与 `orca/exec/context.py` 的 `RunContext`（frozen dataclass）**同名混淆**。phase 11 把前者重命名为 `SessionLoc`（NamedTuple 字段不变），同步更新 phase 6 SPEC §5 引用。这是跨阶段契约变更，release note 记录。

---

## 6. P2.1 Dialog（agent 跑完后多轮聊）

### 6.1 用户流程

```
workflow 跑到 node=configurator 完成（output 已写 Tape）
  ↓
用户按 d 键
  ↓
DialogModal 弹出（不在 node 边界阻塞主循环，是 post-completion 模式）
  ┌──────────────────────────────────────────┐
  │ 💬 DIALOG · node=configurator            │
  │ ───────────────────────────────────      │
  │ [agent 之前的 output 摘要]                │
  │                                          │
  │ user> 为什么 dataset 字段是 NOT_FOUND？   │
  │ agent> 因为 target_project 里没有...      │
  │                                          │
  │ > [输入下一轮问题]                        │
  │                                          │
  │ [发送]  [结束对话，继续下游]               │
  └──────────────────────────────────────────┘
```

### 6.2 实现路径（重 spawn + 拼历史，参考 Conductor）

**DialogHandler**（`orca/gates/dialog.py` 新建）：

```python
class DialogHandler:
    """agent 跑完后多轮对话（重 spawn + 拼历史）。"""

    async def start_dialog(self, node: str, agent_output: Any, ctx: RunContext) -> str:
        """用户按 d 键触发。返回 dialog 结束信号。"""
        # 1. 构造初始 prompt：含 agent 之前 output + system prompt（参考 Conductor
        #    DIALOG_AGENT_SYSTEM_PROMPT）
        # 2. 重 spawn claude -p（用临时 AgentNode，prompt 是对话入口）
        # 3. 进入循环：用户输入 → claude 回 → 写 dialog_message 事件 → 直到用户结束
        # 4. 把对话历史存进 ctx.dialog_history
```

**DialogModal**（`orca/iface/cli/screens/dialog_modal.py` 新建）：

```python
class DialogModal(ModalScreen[str]):
    """多轮对话模态。Textual 原生支持 input + scroll history。"""
    ...
```

**事件**：`dialog_started` / `dialog_message`（role+text+turn）/ `dialog_ended`。

### 6.3 与 InterruptModal 的区别

| 维度 | InterruptModal | DialogModal |
|---|---|---|
| 触发时机 | node 跑中（中断）| node 跑完后（追问）|
| 阻塞主循环 | 是（用户必须先答）| 否（用户可不开 dialog，直接 route 下游）|
| 实现 | SIGINT 杀 + 重 spawn | 重 spawn + 拼历史（不杀原 agent，原 agent 已结束）|

---

## 7. P2.2 Checkpoint Resume（崩溃续跑）

### 7.1 Orca 优势：Tape 即 Checkpoint

Tape 是 append-only JSONL，已经记录了 workflow 的全部历史。**Tape 本身就是 checkpoint**——只需要一个 `resume` 命令读 Tape 重放到崩溃前位置。

### 7.2 实现路径

**新命令 `orca resume`**：

```bash
orca resume runs/<run_id>.jsonl
# 或
orca resume <run_id>  # 从默认 runs/ 目录找
```

**Orchestrator 加 `run_from_state` 入口**：

```python
class Orchestrator:
    ...
    @classmethod
    def from_tape(cls, tape_path: Path, bus: EventBus) -> "Orchestrator":
        """从 Tape 重放构造 Orchestrator，恢复到崩溃前状态。

        - 读 Tape → replay_state → 拿已完成 node 列表 + outputs
        - 构造 ctx（含已完成 outputs）
        - entry point = 第一个未完成 node
        """
        tape = Tape(tape_path, run_id=...)
        state = replay_state(tape)
        # 找最后一个 node_completed，下一个 node 是 resume 起点
        ...
```

**resume 语义**：

1. 读 Tape，重放到最后一个 `node_completed`
2. 找下一个 node（routes 求值）
3. 重新构造 RunContext（含已完成 outputs）
4. emit `workflow_resumed` 事件（写 Tape）
5. 从该 node 继续跑

### 7.3 失败模式

| 场景 | 行为 |
|---|---|
| Tape 文件不存在 | exit 2 + 提示 |
| Tape 文件为空（0 字节） | exit 2 + 提示「Tape 为空，无状态可恢复」 |
| Tape **末尾残行**（崩溃时写了一半的 JSON） | **fail-soft**：warning + 截断残行后继续（复用 `tape.py::_truncate_trailing_partial`，**非** exit 2）。`from_tape` 用 `Tape(path, resume=True)` 触发截断。 |
| Tape 中段损坏（合法行后跟乱码行） | exit 2 + 显示首个不可解析行的位置 |
| Tape 已是终态（workflow_completed） | exit 0 + 提示「workflow 已完成，无需 resume」 |
| Tape 中有未完成 node（崩溃点） | resume 从该 node 重跑 |

---

## 8. P3 daemon / `--background` 模式

### 8.1 用例

```bash
# 启动后台 workflow（立即返回 run_id）
$ orca run examples/long.yaml --background
Started background run: mxint_analysis-20260701-192804-41a0a8
PID: 12345, logs: ~/.orca/runs/mxint_analysis-20260701-192804-41a0a8/log

# 列出活跃 run
$ orca ps
RUN_ID                              WORKFLOW      STATUS     ELAPSED  PID
mxint_analysis-20260701-192804-...  mxint_analysis  running   2m30s    12345

# 查日志
$ orca logs mxint_analysis-20260701-192804-...

# attach 到 TUI（中途加入观察）
$ orca attach mxint_analysis-20260701-192804-...
（进入 Textual TUI，连到 EventBus）

# 等待完成
$ orca wait mxint_analysis-20260701-192804-...
```

### 8.2 实现路径

**fork detached**（参考 Conductor `cli/bg_runner.py`）：

```python
def daemonize(yaml_path: Path, run_id: str) -> int:
    """fork detached 子进程，主进程立即退出。返回 pid。"""
    pid = os.fork()
    if pid > 0:
        return pid  # 父进程返回
    # 子进程：setsid 脱离终端 + 重定向 stdin/stdout/stderr 到 log 文件
    os.setsid()
    log_path = Path.home() / ".orca/runs" / run_id / "log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fd = log_path.open("w")
    os.dup2(fd.fileno(), 0)
    os.dup2(fd.fileno(), 1)
    os.dup2(fd.fileno(), 2)
    # 继续跑 workflow...
```

**持久化 run 元数据**：

`~/.orca/runs/<run_id>.json`：

```json
{
  "run_id": "mxint_analysis-...",
  "pid": 12345,
  "yaml_path": "/Users/.../examples/mxint_analysis.yaml",
  "started_at": 1782904868.0,
  "log_path": "~/.orca/runs/mxint_analysis-.../log",
  "tape_path": "/Users/.../runs/mxint_analysis-....jsonl",
  "status": "running"
}
```

### 8.3 `attach` 机制

`orca attach <run_id>` 连到运行中 orca 进程的 EventBus。**不通过 IPC 共享对象**（不安全），通过**重新订阅 Tape + 监听增量**：

1. 读 Tape → replay_state → 初始化 UI
2. tail -f Tape 文件 → 新事件实时入 UI
3. 用户 gate 答 / interrupt 等通过 Unix Domain Socket 发到 daemon

**复杂度**：attach 需要 daemon 暴露一个控制通道（UDS）接收答 gate / 触发 interrupt。**phase 11 P3 简化版**：只支持「只读 attach」（看 + 滚日志，不能交互）。读写 attach 留后续。

---

## 9. P4 Skip to Agent

### 9.1 用例

```bash
# 中断时选 SKIP，跳过当前 node 直接到指定下游
（InterruptModal 选 SKIP → 弹出 node 选择器 → 选目标 node）
```

### 9.2 实现路径

**Orchestrator 加 `skip_to_node`**：

```python
def _skip_to_next(self, current: str, outputs_acc: dict) -> str:
    """用户选 SKIP 时调。从 current 的 routes 求下一个 node（不执行 current）。

    skip 的语义：current node 的 output 标记为 None（或 set 默认值），routes
    据此求值下一步。
    """
    # 复杂点：skipped node 的 output 是 None，下游引用 {{ skipped_node.output }}
    # 会 UndefinedError → 需要给 skipped node 写一个空 output
    outputs_acc[current] = {"output": None, "skipped": True}
    routes = self._routes_of(current)
    return resolve(routes, None, self._make_ctx(outputs_acc))
```

**route 求值容错**：`resolve` 函数遇到 `output is None` 时，`output.field` 求值会失败 → 走兜底 route（`when=None`）。

---

---

## 9.5 P0.3 Retry Policy（review 补充，2026-07-01）

### 9.5.1 用户场景

```yaml
nodes:
  - name: fetch_api
    kind: agent
    prompt: "调外部 API 拉数据"
    retry:
      max_attempts: 3
      backoff: exponential       # constant / linear / exponential
      initial_delay_seconds: 1.0
      retry_on:
        - spawn_error            # 子进程崩（exit code != 0）
        - timeout                # 超时
        - api_error              # claude API 返 error（result.is_error=true）
        - http_429               # 限流（claude stream-json 的 api_retry subtype）
```

### 9.5.2 数据契约

**RetryPolicy**（`orca/schema/workflow.py` AgentNode 加字段）：

> **设计归属**（review 修正 2026-07-01）：本字段为 **Orca 自创设计**，借鉴 Conductor 思路但字段不同。Conductor 实际（`config/schema.py`）为 `backoff: Literal["fixed","exponential"]` + 单 `delay_seconds` + `retry_on: Literal["provider_error","timeout"]`，无 `jitter`/`max_delay`/`linear`。不冒充「Conductor 参考」。

```python
class RetryPolicy(BaseModel):
    """节点级重试策略。frozen。"""
    max_attempts: int = 3                      # 总尝试次数（含首次）
    backoff: Literal["constant", "linear", "exponential"] = "exponential"
    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0            # 单次延迟上限
    retry_on: list[Literal[
        "spawn_error", "timeout", "api_error", "http_429", "validator_failed"
    ]] = ["spawn_error"]                       # 触发重试的 error_type 白名单（类型安全 Literal）
    jitter: bool = True                        # 加 ±20% jitter 防雪崩
```

**AgentNode 扩展**：

```python
class AgentNode(Node):
    ...
    retry: RetryPolicy | None = None           # None = 不重试（向后兼容）
    validator: ValidatorConfig | None = None   # 见 §9.6
```

**error_type 对齐表**（review 修正：retry_on 取值必须与 executor 实际产出的 `node_failed.data` 字段对齐，否则 retry 永不触发）：

| ClaudeExecutor 实际 `phase` / 条件 | `node_failed.data["error_type"]` | 命中 retry_on |
|---|---|---|
| `phase="timeout"` | `"timeout"` | `timeout` |
| `phase="spawn"`（exit_code != 0，**非** SIGINT） | `"spawn_error"` | `spawn_error` |
| `phase="stream"`（`result.is_error=true`，含 429/500/overloaded） | `"api_error"`；若 `result_text` 含 `rate_limit`/`overloaded`/`429` → `"http_429"` | `api_error` / `http_429` |
| `phase="result_parse"`（输出 schema 校验失败） | `"result_parse"` | **不重试**（配置错误，fail loud） |
| validator 失败（§9.6） | `"validator_failed"` | `validator_failed` |
| 用户 SIGINT 中断 | `node_failed.data["was_interrupted"]=true` | **不重试**（短路退出，见下） |

**前置：`was_interrupted` 字段**（review 修正 C7）：CLIRunner 接 SIGINT 后置 `runner.was_interrupted=True`；ClaudeExecutor 把它写入 `node_failed.data["was_interrupted"]: bool`。retry loop 优先检查此字段——为真则**短路退出，不进入 retry_on 白名单判定**（用户主动中断不属于 transient error）。

### 9.5.3 新增事件类型

```python
EventType = Literal[
    ...,
    "retry_started",         # data: {attempt, max_attempts, error_type, delay_seconds, node}
    "retry_succeeded",       # data: {attempt_total, node}（重试后成功）
    "retry_exhausted",       # data: {attempts, last_error_type, node}（重试用完仍失败）
]
```

### 9.5.4 接口（`orca/run/retry.py` 新建）

```python
async def execute_with_retry(
    executor: Executor,
    node: AgentNode,
    ctx: RunContext,
    bus: EventBus,
) -> tuple[Any, list[Event]]:
    """执行 node，按 RetryPolicy 自动重试。

    返回 (final_output, all_events)。所有 attempt 的事件流都 emit 到 bus。

    重试时机：node_failed 事件后，检查 error_type 是否在 retry_on 白名单。
    重试间隔：按 backoff 策略 + jitter 计算 sleep duration。
    重试上限：max_attempts（含首次）。耗尽后 re-raise 最后一次 ExecError。
    """
```

### 9.5.5 与 orchestrator 集成

`orca/run/orchestrator.py::_dispatch`：

```python
async def _dispatch(self, current, ctx):
    node = self._node_by_name[current]
    if node.kind == "agent" and node.retry is not None:
        from orca.run.retry import execute_with_retry
        executor = make_executor(node)
        output, _ = await execute_with_retry(executor, node, ctx, self.bus)
        return output
    # 既有路径（无 retry）
    return await execute_and_emit(make_executor(node), node, ctx, self.bus)
```

### 9.5.6 边界与失败模式

| 场景 | 行为 |
|---|---|
| RetryPolicy.max_attempts=1 | 等价无 retry，行为不变 |
| RetryPolicy.retry_on 不含实际 error_type | 不重试，直接 raise |
| RetryPolicy.backoff=exponential + initial=1s | 1s, 2s, 4s, 8s... 上限 max_delay_seconds |
| 用户 Ctrl+G interrupt 触发的 was_interrupted=True | **不重试**（用户主动中断，不属于 transient error） |
| 重试期间再次中断 | 当前 attempt 直接 abort |

---

## 9.6 P2 Semantic Output Validator（review 补充）

### 9.6.1 用户场景

```yaml
nodes:
  - name: analyzer
    kind: agent
    prompt: "分析项目，输出 JSON"
    output_schema: { ... }        # shape/type 校验（既有）
    validator:                     # 语义校验（新）
      criteria: "model_class 必须是合法的 Python 标识符；weights_path 必须是绝对路径"
      max_retries: 1               # 校验失败时重试 1 次
      model: null                  # 用默认模型；可指定 sonnet/haiku 等省 token
```

### 9.6.2 数据契约

**ValidatorConfig**（AgentNode 加字段）：

```python
class ValidatorConfig(BaseModel):
    criteria: str                                # 自然语言描述的校验标准
    max_retries: int = 1                         # 校验失败时重试次数
    model: str | None = None                     # 校验用的 LLM 模型
```

**AgentNode 扩展**：

```python
class AgentNode(Node):
    ...
    validator: ValidatorConfig | None = None
```

### 9.6.3 新增事件类型

```python
"validator_started",       # data: {node, criteria_preview}
"validator_passed",        # data: {node, issues: []}
"validator_failed",        # data: {node, issues: [str], retrying: bool}
```

### 9.6.4 接口（`orca/exec/validator.py` 新建）

```python
async def validate_output(
    output: Any,
    config: ValidatorConfig,
    node: str,
    bus: EventBus,
    profile: CliProfile,            # review 修正 C5：必须经 profile.resolve_cli_path()，兼容 ccr 中转
    model: str | None = None,
) -> tuple[bool, list[str]]:
    """用 LLM 二次校验 output 是否满足 config.criteria。

    返回 (passed, issues)。passed=False 时 issues 含具体问题列表。

    实现：spawn 第二个 claude -p（短 prompt 含 output + criteria），让它返回
    {"passed": bool, "issues": [str]} JSON。

    SpawnConfig 必须用 `cli_path=profile.resolve_cli_path()` + `flags=profile.flags`，
    **不得硬编码 `cli_path="claude"`**（否则用户 ccr 中转失效）。
    """
```

### 9.6.5 与 orchestrator 集成（单一 retry loop，review 修正 B3）

**关键**：retry 与 validator **共享同一个 retry loop、同一份 `max_attempts` 计数**，**不**双层嵌套。validator 失败消耗 1 次 attempt，error_type=`validator_failed`（在 retry_on 白名单内才重试）。

```python
async def _dispatch(self, current, ctx):
    node = self._node_by_name[current]
    if node.kind != "agent":
        return await self._dispatch_non_agent(current, ctx)

    policy = node.retry                       # RetryPolicy | None
    total_attempts = policy.max_attempts if policy else 1
    validator = node.validator                # ValidatorConfig | None
    # validator 自身的 max_retries 折进 total_attempts：默认 validator.max_retries=1
    # 即「校验失败可再跑 1 次」，与 retry.max_attempts 取较小者作为总预算上限语义不变。

    last_issues: list[str] = []
    for attempt in range(1, total_attempts + 1):
        # 1) 执行（单次，不内嵌 retry）
        output = await self._execute_once(current, ctx)   # 流式 emit 不变

        # 2) 无 validator → 直接返回（retry 只在 executor 失败时由下层 emit node_failed 触发）
        if validator is None:
            return output

        # 3) 有 validator → 二次校验
        passed, issues = await validate_output(
            output, validator, node.name, self.bus, profile=self.profile,
        )
        if passed:
            return output

        # 4) validator 失败：判定是否还能重试
        last_issues = issues
        can_retry = (
            policy is not None
            and attempt < total_attempts
            and "validator_failed" in policy.retry_on
        )
        self.bus.emit("validator_failed", {
            "node": node.name, "issues": issues, "retrying": can_retry,
        })
        if not can_retry:
            raise ExecError(phase="validator",
                            message=f"validator failed: {issues}")
        # 把 issues 作为 guidance 反馈给下次 spawn
        ctx = ctx.with_guidance(f"上次输出未通过校验：{'; '.join(issues)}")

    raise ExecError(phase="validator",
                    message=f"validator exhausted: last issues={last_issues}")
```

**executor 级 transient 失败**（spawn_error/timeout/api_error）由 `execute_with_retry`（§9.5.4）在 `_execute_once` 内部按 `retry_on` 白名单重试，**与 validator loop 正交**：execute_with_retry 用尽后 raise，向上冒泡为 node 失败（不再进 validator）。两个 loop 各管各的失败域，不嵌套计数。

### 9.6.6 边界

| 场景 | 行为 |
|---|---|
| validator=None | 不校验（向后兼容） |
| validator.passed=True | emit validator_passed，正常 route 下游 |
| validator.passed=False + retry 未用完 | emit validator_failed(retrying=True)，重 spawn agent（prompt 加 issues 反馈） |
| validator.passed=False + retry 用完 | emit validator_failed(retrying=False)，raise ExecError |
| 校验 LLM 自己崩 | 当作 validator.passed=True（不阻塞 workflow，记 warning）—— 安全 fallback |

---

## 9.7 P3 Wait Node（review 补充）

### 9.7.1 用户场景

```yaml
nodes:
  - name: fetch
    kind: script
    command: "curl https://api.example.com/data"
    routes:
      - when: "output.exit_code == 0"
        to: process
      - to: rate_limit_wait   # 失败（被限流）

  - name: rate_limit_wait
    kind: wait
    duration: "60s"             # 支持 Jinja2 渲染：duration: "{{ inputs.wait_time }}s"
    reason: "等待 API rate limit 恢复"
    routes:
      - to: fetch               # 重试

  - name: process
    kind: agent
    prompt: "处理 {{ fetch.output.stdout }}"
```

### 9.7.2 数据契约

**WaitNode**（`orca/schema/workflow.py` 加新 kind）：

```python
class WaitNode(Node):
    kind: Literal["wait"] = "wait"
    duration: str                # Jinja2 渲染，支持 "30s"/"5m"/"2h" 或纯秒数 "30"
    reason: str = ""             # 人类可读说明（写入 tape 用于调试）
    interruptible: bool = True   # True=可被 Ctrl+G 打断；False=必须等完
```

### 9.7.3 新增事件类型

```python
"wait_started",            # data: {node, duration_seconds, reason}
"wait_completed",          # data: {node, elapsed_seconds, interrupted: bool}
```

### 9.7.4 接口（`orca/exec/wait.py` 新建）

```python
class WaitExecutor(Executor):
    """asyncio.sleep 实现 wait node，可被 interrupt 打断。"""

    async def exec(self, node: WaitNode, ctx: RunContext) -> AsyncIterator[Event]:
        session_id = uuid.uuid4().hex
        yield _ev("node_started", {"kind": "wait"}, session_id=session_id)

        duration_str = render_template(node.duration, ctx)
        duration_seconds = parse_duration(duration_str)  # "30s" → 30, "5m" → 300

        yield _ev("wait_started", {
            "duration_seconds": duration_seconds,
            "reason": node.reason,
        }, session_id=session_id)

        if node.interruptible:
            # 用 asyncio.Event 让 interrupt_handler 能打断
            interrupt_evt = asyncio.Event()
            self._bus.register_wait_handle(interrupt_evt)  # 见 §9.7.6 契约
            try:
                done, _ = await asyncio.wait(
                    [asyncio.create_task(asyncio.sleep(duration_seconds)),
                     asyncio.create_task(interrupt_evt.wait())],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                interrupted = interrupt_evt.is_set()
            finally:
                self._bus.unregister_wait_handle(interrupt_evt)
        else:
            await asyncio.sleep(duration_seconds)
            interrupted = False

        yield _ev("wait_completed", {
            "elapsed_seconds": duration_seconds,
            "interrupted": interrupted,
        }, session_id=session_id)
        yield _ev("node_completed", {
            "output": {"interrupted": interrupted},
            "elapsed": duration_seconds,
        }, session_id=session_id)
```

### 9.7.5 边界

| 场景 | 行为 |
|---|---|
| duration="abc"（非法）| raise ExecError(phase="render") |
| duration 超过 24h | raise ExecError（硬上限，防配置错） |
| interruptible=False + 用户 Ctrl+G | 必须等完，Ctrl+G 等下一 node 边界生效 |
| parallel group 内 wait node | 与其他 branch 并行 sleep，互不干扰 |

### 9.7.6 与 interrupt 系统协同（review 修正 B1）

**EventBus 新增公开接口契约**（`orca/events/bus.py`，**取代** PLAN 里虚构的 `register_interrupt_target`）：

```python
class EventBus:
    ...
    def register_wait_handle(self, handle: asyncio.Event) -> None:
        """WaitExecutor 进入 interruptible sleep 前注册一个 handle。幂等。"""

    def unregister_wait_handle(self, handle: asyncio.Event) -> None:
        """sleep 结束（正常/被打断）后注销。幂等（注销未注册的 handle 不报错）。"""

    def notify_all_waits(self) -> int:
        """InterruptHandler 收到 Ctrl+G 时调：set 所有已注册的 wait handle。
        返回被唤醒的 handle 数。线程安全（用内部 lock 保护 _wait_handles 集合）。"""
```

interrupt_handler 收到 Ctrl+G 时，**除了杀当前 claude -p**，也调 `bus.notify_all_waits()`——让所有 interruptible wait node 立即结束（`wait_completed.interrupted=True`）。这是 phase 11 §3 中断 UI 的扩展。WaitExecutor 用 `register_wait_handle`/`unregister_wait_handle`，**不**访问 bus 私有属性。

---

## 10. 验收标准（整体）

### 10.1 每个 feature 必须满足

| 验收项 | 通用标准 |
|---|---|
| **SPEC 符合** | 实现 = SPEC 描述（接口签名 / 事件 payload / 行为语义逐字一致）|
| **测试覆盖** | 单测 + 集成测试 + E2E 实跑三档齐 |
| **Tape 持久化** | 新事件类型全部写 Tape，replay_state 能还原 |
| **CLI 打印** | format_event 加新事件类型描述，LogStream 显示清晰 |
| **零回归** | 全量 `pytest tests/` 0 failures（基线 683 passed）|
| **真实 E2E** | `orca run examples/mxint_analysis.yaml` 仍 exit 0 |
| **release note** | 完整记录（背景 / 改动 / 验证 / commit / review）|

### 10.2 整体 phase 验收

完成 P0-P2 后必须能：

1. ✅ `git push` → CI 自动跑测试，绿
2. ✅ **mock ClaudeExecutor 产出 `node_failed{error_type:"api_error"}`（`result.is_error=true` + result_text 含 `rate_limit`）→ Retry Policy 按 `retry_on=["api_error"]` 重试 N 次后成功**（review item2：API 错误走 result.is_error，**非** exit code）
3. ✅ `orca run examples/mxint_analysis.yaml` 跑到 configurator 时按 Ctrl+G → 输入「skip weights」+ CONTINUE → workflow 继续。**断言可观测中间产物**（review B5：LLM output 非确定，不断言「output 含 guidance」）：(a) tape 的 `interrupt_resolved` 事件 data 含 `guidance:"skip weights"`；(b) 重 spawn 的 `prompt_rendered` 事件（ClaudeExecutor 新增，data 含 prompt 末尾预览）含 `[User Guidance]` 段。
4. ✅ agent 调 `ask_user` MCP 工具（带 `_orca_run_id`/`_orca_node` 路由参）→ CLI 弹 AskGate → 用户答 → agent 收到答案继续；tape 含 `human_decision_requested`（source=agent_ask）+ `human_decision_resolved` 配对
5. ✅ **agent 声明 validator.criteria → agent 输出后跑 LLM 二次校验 → 校验失败 retry 一次**
6. ✅ workflow 跑完后按 d → DialogModal 弹出 → 多轮对话 → 结束
7. ✅ kill -9 orca 进程 → `orca resume runs/<run_id>.jsonl` → 从崩溃点继续（末尾残行 fail-soft 截断）
8. ✅ 全程 LogStream 显示所有新事件类型（interrupt_*/dialog_*/workflow_resumed/**retry_started/retry_succeeded/retry_exhausted/validator_started/validator_passed/validator_failed/wait_started/wait_completed/prompt_rendered**）+ token 数

完成 P3-P4 额外：

9. ✅ **`orca run examples/with_wait.yaml`（含 wait node duration=2s）→ workflow 暂停 ~2s 后继续（`wait_completed.interrupted=False`）；再跑一次，1s 时 Ctrl+G → wait node 立即结束（`wait_completed.interrupted=True`）→ 下游看到 interrupted=True**（review item9：补 interruptible 覆盖）
10. ✅ `orca run examples/long.yaml --background` → 立即返回 run_id，不阻塞终端
11. ✅ `orca ps` / `orca logs` / `orca wait` 三件套可用（review D2：**只读 `attach` descoped** 到后续 phase——价值低于 `tail -f` tape）
12. ✅ InterruptModal 选 SKIP → 若当前 node 有 `when=None` 兜底 route 则沿 route 跳；**否则弹 node 选择器**让用户显式选目标 node（review item12：无兜底 route 时自动求值会 NoRouteMatch 崩溃，必须 fallback 到选择器）

---

## 10.3 Review 驱动的 SPEC 修订（2026-07-01，对抗评审闭环）

`spec-review-adversarial` 评审裁决 **fail → conditional-pass**（22 条真问题已逐条闭环）。修订汇总：

| 评审条目 | 严重度 | 修订位置 | 修订内容 |
|---|---|---|---|
| B1 Wait↔interrupt 虚构接口 | critical | §9.7.6 | 定义 EventBus `register_wait_handle`/`unregister_wait_handle`/`notify_all_waits` 公开契约；§9.7.4 代码改名 |
| B2 同名 RunContext + phase 6 register 债 | critical | §5.5 | gates.RunContext → `SessionLoc`；phase 6 register 调用必须先补 |
| B3 retry↔validator 双 loop | critical | §9.6.5 | 重写为单一 retry loop，共享 max_attempts 计数 |
| B5 item3 不可验证 | critical | §10.2 item3 | 改断言 `interrupt_resolved.guidance` + `prompt_rendered` 事件（§2.2 新增） |
| C1 RetryPolicy 与 Conductor 不符 + retry_on 错配 | critical | §9.5.2 | retry_on 改 Literal 枚举 + error_type 对齐表 + 标注「Orca 自创」 |
| C2 FastMCP 三层不成立 | critical | §5.3 | import 改 `mcp.server.fastmcp`；删 `fastmcp.testing`；前置 SSE spike |
| C3 with_outputs 虚构 | critical | §2.3 | 明确走既有 `_make_ctx`，不新增 with_outputs |
| C4 make_executor 注入断裂 | major | §5.4 | `make_executor(node, agent_tools_server=None)` 新签名 |
| C5 validator 硬编码 cli_path | major | §9.6.4 | validate_output 加 `profile` 参数 |
| C6 resume 半截行 | major | §7.3 | 末尾残行 fail-soft 截断（非 exit 2） |
| C7 was_interrupted 字段缺失 | major | §9.5.2 | CLIRunner.was_interrupted + node_failed data 字段 + retry 短路 |
| C8 _reconstruct_outputs 虚构 | major | PLAN | 测试改断言 replay_state 结果 |
| C9 stdio fallback 不可行 | major | §5.3 | 锁定 SSE，spike 失败则 ask_user 推迟 |
| C10 §1.3 跨阶段优先级 | minor | §1.3 | 补「原 phase 9，phase 11 提前」 |
| item2 exit code 前提错 | critical | §10.2 item2 | 改 result.is_error + rate_limit |
| item4 MCP session 假设错 | critical | §5.3/§5.5 | 确定性 `_orca_run_id`/`_orca_node` tool-params 路由 |
| item9 漏 interruptible | major | §10.2 item9 | 补 Ctrl+G 打断 wait 测试 |
| item12 SKIP 无兜底崩溃 | minor | §10.2 item12 | 无 when=None 时弹 node 选择器 |
| 测试A _orchestrator_proxy 虚构 | critical | §2.3 | Orchestrator.request_interrupt 公开方法 |
| 测试B run_dialog_turn 不存在 | major | PLAN/§6.2 | DialogHandler 统一 run_dialog（整体跑） |
| item10/11 attach 价值低 | 权衡 | §10.2 item11 | 只读 attach descoped |

**4 个用户决策**（已由 orchestrator 裁定，Rule 7）：D1=wave 顺序（B，仍全做）/ D2=descope attach（B）/ D3=Budget OUT（SPEC §12 胜）/ D4=确定性 tool-params 路由（A）。

**实施前置 blockers**（clean-code-builder 开工前 MUST）：见 §9.5.2（error_type 对齐）/§5.3（SSE spike）/§2.3（request_interrupt）/§9.7.6（wait handle）/§5.4（make_executor）/§5.5（register 债 + SessionLoc 重命名）/§9.6.4（profile）。

---

## 11. 偏离 SPEC 处

（实现中如需偏离，在 plan + release note 同步记录 + 理由。SPEC 是契约，不允许静默偏离。）

### 11.1 CLI 单壳中断路径不经 await-future（P1.1 Step B，2026-07-02）

**偏离**：SPEC §2.3 规定 `Orchestrator.request_interrupt(ireq)` 公开方法登记 pending，但未规定
「resolve 何时被调」。Step A 实现的 `action_interrupt` 把「登记 pending + resolve」连调，撞
**critical 时序死锁**（review §2.1）：CLI 单壳用户在 InterruptModal 答完时 orchestrator 还没到
node 边界（`handler.request` 未调、future 未注册），`resolve` 必然落空 + workflow 永久卡死。

**裁定（Rule 7）**：CLI 单壳路径**新增** `Orchestrator.request_interrupt(ireq, answer=None)` +
`InterruptHandler.record_resolved(...)`——answer 随请求带入，`_handle_interrupt` 在 node 边界
直接消费（emit requested + 入队 resolved 写 Tape），**不经 await-future**。多壳路径（web/mcp，
await-future 竞速）完整保留给 P3。SPEC §3.1 流程图（resolve 在 node 边界后）对 CLI 单壳不成立，
以本裁定 + release note `2026-07-02-phase11-guidance-injection.md` 为准。

### 11.2 ask_user 路由参名去掉下划线前缀（P1.2，2026-07-02）

**偏离**：SPEC §5.3 规定 ask_user 工具的确定性路由参为 `_orca_run_id` / `_orca_node`
（下划线前缀 hidden params）。实现时 `mcp.server.fastmcp` 拒绝以下划线开头的参数
（``InvalidSignature: Parameter ... cannot start with '_'``，FastMCP 把它当私有/内部，
``func_metadata`` 显式 raise）。

**裁定（Rule 7）**：路由参改名为 `orca_run_id` / `orca_node`（去掉下划线前缀）。语义不变——
仍是确定性 tool-params 路由（决策 D4），仅命名调整。render_prompt 的 instruct 文本同步用
`orca_run_id` / `orca_node`。SPEC §5.3 的 `_orca_run_id` / `_orca_node` 字面量以本裁定为准。

### 11.3 ask_user 工具权限必须显式 --allowed-tools（P1.2，2026-07-02）

**偏离/补充**：SPEC §5.4 未提及 claude -p 默认不给 MCP 工具授权。Spike 实证（2026-07-02）：
claude -p 即使连上 SSE MCP server、发现工具，**默认拒绝调用**（``The tool call was blocked —
permission hasn't been granted``），必须 ``--allowed-tools mcp__<server>__<tool>`` 显式授权才能调。

**裁定（Rule 7）**：`_build_spawn_config` 在注入 agent_tools_server 时，自动把
`mcp__orca-agent-tools__ask_user` 加进 ``--allowed-tools``（用户声明白名单时 append，全开时
单声明）。这是 spike 的硬约束，不是可选优化。SPEC §5.4 此处以本裁定 + 实现为准。

### 11.4 register_session 时机前移 + session 路由按 run 批清（P1.2，2026-07-02）

**偏离**：SPEC §5.5 写「ClaudeExecutor spawn claude 成功、拿到 claude session_id（从
``system/init`` 事件）后调 ``registry.register(...)``」。实现中 register **前移到 spawn 之前**
（写 mcp-config 之后、CLIRunner 启动之前），且 session_id 用 **executor 入口生成的 uuid**
（``uuid4().hex``，SPEC §4.3 铁律 5），**非** claude 流里 ``system/init`` 的 session_id。

**理由（决策 D4 副产物）**：D4 放弃了「MCP session 反查」假设（claude -p 不主动报 MCP session），
ask_user 路由改靠确定性 tool-params（``orca_run_id``/``orca_node``）。register 的作用降级为给
**hook 桥**（PreToolUse，仍走 claude session_id）留路由——而 hook 桥的 session_id 也是 claude
流里的，与 executor uuid 不同源。当前实现 register 用 executor uuid 主要是 mcp-config 文件名
稳定性 + 未来 hook 桥接入时再补 claude session_id 的 register。register 时机前移到 spawn 前是
为了确保 ask_user 调用到达时路由已就绪（避免 spawn-到-ask_user 的 race）。

**session 路由清理（SPEC §6）**：SPEC §6 写「node 完成时 unregister」。实现改为**run 结束时
按 run_id 批清**（``registry.unregister_run(run_id)``，orchestrator._stop_agent_tools 调）。
理由：session_id 由 executor 内部 uuid 生成，orchestrator 不持有，无法逐 node 精确 unregister；
按 run 批清同等防泄漏（registry 是 per-run 实例，run 结束即整体废弃），且简单（Rule 2）。
逐 node 清理留后续 hook 桥接入时按需补。

SPEC §5.5 的 register 时机 / §6 的 unregister 粒度以本裁定 + 实现为准。

---

## 12. 不在 phase 11 范围

明确**不做**的：

- ❌ Web cancel 端点 + 前端按钮（推迟到 Web phase）
- ❌ Web 端 InterruptModal / DialogModal 渲染（推迟到 Web phase）
- ❌ 多 backend（vendor-neutral，推迟）
- ❌ token 级流降级（保留 -p 路线独家优势）
- ❌ 切 Claude Agent SDK（已论证不切）
- ❌ cron / 定时（无需求）
- ❌ **CLI Self-Update**（`conductor update`）——用户体验优化，非核心，推迟到 phase 12+
- ❌ **Budget Enforcement**（review 评估后用户决定不做）——`budget_usd + budget_mode`，防烧钱。决定不做理由：用户当前主要在开发期单 run 场景，超支手动监控即可；生产场景未来如有需要再加。Conductor 参考：`engine/limits.py:75-148`。
- ❌ **Workflow Registry / Template Store**（`conductor registry`）——生态建设，推迟到 phase 12+
- ❌ **Linkify Markdown**（gate prompt 自动链接化）——UI 优化，推迟
- ❌ **Dialog Evaluator (LLM 自动触发 Dialog)**——手动 Dialog 已够用，自动触发是增强
- ❌ **Sub-Workflow / Recursive Workflow**——复杂度高，用户场景少
- ❌ **Terminate Node**（显式终止节点）——隐式 `$end` 已够用
- ❌ **Pricing Tables**（完整模型定价表）——claude -p 已返回 cost_usd，不需要本地定价表

---

## 附录 A：phase 11 文件清单

**新增**：
- `orca/gates/interrupt.py`（InterruptHandler + 事件类型）
- `orca/gates/dialog.py`（DialogHandler）
- `orca/gates/_broadcaster_mixin.py`（共享 start/stop/_broadcaster）
- `orca/exec/mcp_tools/__init__.py`
- `orca/exec/mcp_tools/server.py`（AgentToolsMcpServer）
- `orca/exec/validator.py`（**review 补充**：SemanticValidator）
- `orca/exec/wait.py`（**review 补充**：WaitExecutor）
- `orca/run/retry.py`（**review 补充**：execute_with_retry）
- `orca/run/resume.py`（resume 命令实现）
- `orca/iface/cli/bg_runner.py`（daemon fork)
- `.github/workflows/test.yml`（CI）
- `.github/workflows/integration.yml`（CI integration）
- `tests/gates/test_interrupt.py`
- `tests/gates/test_dialog.py`
- `tests/exec/mcp_tools/test_server.py`
- `tests/exec/test_validator.py`（**review 补充**）
- `tests/exec/test_wait.py`（**review 补充**）
- `tests/run/test_retry.py`（**review 补充**）
- `tests/iface/cli/test_interrupt_modal.py`
- `tests/iface/cli/test_dialog_modal.py`
- `tests/run/test_resume.py`

**修改**：
- `orca/exec/context.py`（RunContext 加 3 字段 + 2 方法）
- `orca/schema/event.py`（EventType 加 14 个：6 基础 + 8 review 补充）
- `orca/schema/workflow.py`（AgentNode 加 retry / validator 字段；新增 WaitNode kind）
- `orca/run/orchestrator.py`（_drive_loop 加 interrupt 检查 + retry 集成 + run_from_state）
- `orca/exec/claude/executor.py`（_build_spawn_config 填 mcp_flag_args）
- `orca/exec/runner.py`（CLIRunner 加 send_sigint）
- `orca/exec/render.py`（render_prompt 拼 guidance section）
- `orca/exec/factory.py`（make_executor 分派 WaitNode → WaitExecutor）
- `orca/gates/handler.py`（继承 _broadcaster_mixin，去重）
- `orca/gates/context_registry.py`（加 lookup_by_mcp_session）
- `orca/iface/cli/app.py`（绑定 ctrl+g / d 键 + 注册 InterruptModal/DialogModal）
- `orca/iface/cli/commands.py`（加 resume / ps / logs / attach / wait 子命令 + --background flag）
- `orca/iface/cli/widgets/log_stream.py`（format_event 加 14 个新事件类型描述）
- `tests/iface/cli/test_app.py`（_app helper 默认 mock InterruptHandler）

**示例**：
- `examples/with_ask_user.yaml`（演示 ask_user 工具调用）
- `examples/with_dialog.yaml`（演示 dialog 模式）
- `examples/with_retry.yaml`（**review 补充**：演示 retry policy）
- `examples/with_validator.yaml`（**review 补充**：演示 semantic validator）
- `examples/with_wait.yaml`（**review 补充**：演示 wait node）

### 11.5 Wait Node 实现（P3.1，2026-07-02）—— 与 SPEC §9.7 的字面偏离

实现 WaitExecutor（`orca/exec/wait.py`）时，与 §9.7.4 / §9.7.6 字面代码示例有 3 处合理偏离，
均在 release note `2026-07-02-phase11-wait-node.md` 记录。SPEC 契约以本节裁定为准：

1. **WaitExecutor 依赖 `WaitHandleRegistry` Protocol 而非 `EventBus`**（铁律 2 张力化解）：
   SPEC §9.7.4 示例写 `self._bus.register_wait_handle(...)`，暗示 WaitExecutor 持 `EventBus`。
   但铁律 2（`tests/exec/test_contract.py::test_dependency_no_events_bus_no_tape`）禁 exec/
   import `orca.events.bus` / 持 `EventBus`（executor 不写 tape / emit）。裁定：在 `exec/wait.py`
   定义 `WaitHandleRegistry` Protocol（仅 `register_wait_handle`/`unregister_wait_handle` 两方法，
   能力裁剪到最小），exec/ 依赖 Protocol（ISP/DIP）；EventBus 结构化满足 Protocol（duck typing），
   `make_executor` 把 orchestrator 持有的 bus 实例透传进来。executor 无法经此 Protocol 写 tape/emit。

2. **`elapsed_seconds` 取 `monotonic()` 实测值**：SPEC §9.7.4 示例写 `"elapsed_seconds": duration_seconds`
   （额定秒数）。实现取 `time.monotonic() - start`（实际经过时长）——被打断时远小于 `duration_seconds`，
   语义更准确（`elapsed` 字面义就是「已逝」）。SPEC 示例是伪代码简化。

3. **超上限走 `phase="config"` / `error_type="ConfigError"`**：SPEC §9.7.5 边界表写超上限
   `raise ExecError`（未指定 phase）。实现 emit `node_failed{phase:"config", error_type:"ConfigError"}`
   以区分「渲染语法错」（render）与「值超限」（config）。同步在 `orca/exec/error.py` 的
   `_PHASE_TO_ERROR_TYPE` 登记 `"config": "ConfigError"`（OCP 局部扩展，SPEC §6 映射表单点真相）。

**make_executor 签名扩展**：`make_executor(node, agent_tools_server=None, bus=None)`——`bus` 仅 wait
分支透传给 WaitExecutor（缺 bus → `ValueError` fail loud，打断契约不能静默失效）；script/set/
foreach 分支忽略此参（向后兼容）。orchestrator `_dispatch` 与 `parallel.run_one` 透传 `self.bus`。

### 11.6 Semantic Validator 实现（P2.1，2026-07-02）—— 与 SPEC §9.6.4 / §9.6.5 的字面偏离

实现 `orca/exec/validator.py` + orchestrator `_dispatch_with_validator` loop 时，与 §9.6.4 签名 /
§9.6.5「单一 retry loop」字面描述有 2 处合理偏离，均在 release note `2026-07-02-phase11-validator.md`
记录。SPEC 契约以本节裁定为准：

1. **`validate_output` 不持 bus、不 emit（铁律 2 张力化解）**：SPEC §9.6.4 示例签名写
   `validate_output(..., bus: EventBus, ...)` 且让它自己 emit `validator_started` / `validator_passed`。
   但铁律 2（`tests/exec/test_contract.py::test_dependency_no_events_bus_no_tape`）禁 exec/ import
   `orca.events.bus` / 持 `EventBus`（executor 产 `AsyncIterator[Event]`，写 tape / emit 归
   orchestrator）。两条原则冲突。**裁定（Rule 7，选 B）**：`validate_output` 移除 `bus` 参数，
   **纯返回 `(passed, issues)`**（计算 + 一次子进程 spawn，无副作用）；三类 validator_* 事件
   （started / passed / failed）**全部由 orchestrator 的 `_dispatch_with_validator` loop emit**——
   单一 emitter，职责清晰，与 retry_*（也由 retry loop 在 run/ 层 emit）模式一致。这比 SPEC 示例
   的「split emit」（validate_output 发 started/passed、orchestrator 发 failed）更内聚。

2. **validator 与 retry 是独立机制 + 独立预算（§9.6.5「单一 retry loop」改写）**：SPEC §9.6.5 原文
   写「retry 与 validator **共享同一个 retry loop、同一份 max_attempts 计数**，不双层嵌套」。但
   wave-2 `execute_with_retry`（`orca/run/retry.py`）已是自包含 transient-retry primitive（27 测试
   committed）。**裁定（Rule 7）**：validator 与 retry **正交**——`_dispatch_with_validator` 在
   `execute_with_retry` **外层**包一层 validator loop：
   - `execute_with_retry`（wave 2，unchanged）：管 transient EXECUTOR 失败（spawn_error/timeout/
     api_error/http_429）。预算 = `retry.max_attempts`，由 `retry_on` 白名单驱动。
   - validator loop（wave 3，本节）：每次成功 execute 产 output 后跑 `validate_output`。validator
     重试由 **`validator.max_retries`** 驱动（**非** retry_on），总尝试 = `max_retries + 1`。失败且有
     预算 → emit `validator_failed(retrying=True)` + issues 作 guidance（`ctx.with_guidance`）→
     重 execute + 重 validate。用尽 → emit `validator_failed(retrying=False)` + raise
     `ExecError(phase="validator")`。

   `validator_failed` 保留在 `RetryPolicy.retry_on` Literal（harmless：executor 不发此 error_type，
   对 retry loop 是 no-op）—— 不 churn wave-2 schema。两 loop 各管各的失败域，不嵌套计数。
