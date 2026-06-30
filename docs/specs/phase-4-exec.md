# 阶段 4 SPEC —— exec/ 执行内核（单 node 执行层）

> **状态**：最终版（待分发实现）
> **依据**：[TASK.md](../TASK.md) §2 §3 §7 · [PLAN.md](../PLAN.md) · [phase-3-events.md](phase-3-events.md) §10
> **范围**：单 node 的执行内核（agent / script / set 三种叶子 kind）；**不做编排**（foreach 分批 / 单指针推进 / 路由归 phase 5）。
> **后端**：仅 `claude -p` 子进程路线（一期）；**不迁移 AgentHarness 代码**，重写。
> **执行任务**：见文末 §14「TASK4 完整描述」

---

## 0. 设计目标

phase 4 回答唯一一个问题：**「给定一个 node + context，怎么把它跑出来、吐出事件流？」**

| 模块 | 解决什么 | 核心交付 |
|---|---|---|
| `Executor` 接口 | 后端无关的执行契约 | `async exec(node, ctx) -> AsyncIterator[Event]` |
| `ClaudeExecutor` | claude -p 子进程路线 | spawn claude → 翻译 stream-json → yield Event |
| `ClaudeTranslator` | claude 协议 → Orca Event（纯函数） | `stream-json 一行 → list[Event]` |
| `ScriptExecutor` / `SetExecutor` | 确定 kind 的执行 | shell / Jinja2 求值 |
| `RunContext` | 节点间数据传递契约 | 累积 map + inputs + run_id |

**这一阶段不解决**（明确边界，见 §5）：
- 编排（单指针推进 / parallel 组并行 / foreach / 路由 / 循环控制）—— phase 5
- 整 workflow 端到端跑 —— phase 5
- WebSocket / CLI 渲染 —— phase 7 / 6
- HMIL / PreToolUse hook —— phase 8
- `--bg` / attach / session resume —— 未来

**验证粒度**：单 node `await executor.exec(node, ctx)` 能跑出正确事件流；**不追求跑完整 workflow**。

---

## 1. 双层抽象（可扩展性根基，TASK.md §2）

```
Layer 1: Executor 接口（后端无关，单一契约）
  async def exec(node, ctx) -> AsyncIterator[Event]

Layer 2: 每个实现 = 共享基础设施 + 协议特化
  ClaudeExecutor  = CLIRunner（通用子进程）+ ClaudeTranslator（claude 协议）
  未来 CodexExecutor = CLIRunner + CodexTranslator
  ScriptExecutor  = subprocess（无 translator，shell 命令本身即协议）
  SetExecutor     = Jinja2 求值（无子进程）
```

行业共识：claude / codex / opencode 全是「headless 子进程 + stdout JSON 事件流」范式。CLIRunner 是这一大类的**共享子进程基础设施**；Translator 是**每个 backend 的纯函数协议适配器**。加新 CLI backend = 共享 CLIRunner + 换一个 Translator（OCP）。

> **接口签名已拍板**：`AsyncIterator[Event]`（非回调侧通道）。理由：契合「事件唯一真相源」——executor 产出的 Event 流直接被上层 `bus.emit(..., session_id=...)` 消费，无需中间回调层；流式语义更纯粹。代价：retry/interrupt 逻辑自己写（本阶段只做超时 + 错误映射，retry 归 phase 5 编排层）。

---

## 2. claude -p 调用协议（客观事实，重写的事实依据）

> 以下基于 AgentHarness 录制的真实 fixture（`claude_code_version 2.1.150`）实测，以及 Conductor / AgentHarness 的调用约定。**这些是协议契约，不是要迁移的框架资产。**

### 2.1 argv 构造

```
claude -p --output-format stream-json --include-partial-messages --verbose \
        --permission-mode auto --bare [--model <m>] [--allowed-tools "<t1 t2 ...>"] \
        [--append-system-prompt "<agent md>"]
```

- prompt 通过 **stdin** 传递（`prompt_channel="stdin"`），**不进 argv**。
- **不使用** `--dangerously-skip-permissions`（TASK.md §7：危险 + 托管环境失败点）。
- **不使用** `--allowedTools`（驼峰），用 `--allowed-tools`（kebab），**单 flag + 空格 join**（不是 variadic，variadic 会吞位置参数）。
- `--model` 仅当 `AgentNode.model` 显式指定时追加。
- `--append-system-prompt` 仅当加载了 `agents/<name>.md` 时追加。
- flags 全部来自 phase 3 已定义的 `CliProfile.flags`（builtin/claude.py 已含 `-p --output-format stream-json --include-partial-messages --verbose --permission-mode auto --bare`）。

### 2.2 stdin / stdout / 子进程

- `asyncio.create_subprocess_exec`（**非** `_shell`），三 PIPE。`cli_path` 多 token（如 `"ccr code"`）须 `shlex.split`。
- **stdin pump**：写 UTF-8 prompt bytes → `drain()` → `close()` → `await wait_closed()`。claude 靠 EOF 知道输入结束。
- **stdout 逐行 readline**（`asyncio.StreamReader.readline()` 按 `\n` 分割），每行 decode UTF-8 + `rstrip("\n")`，空行跳过。每行喂 translator。
- stderr 分块读 + 累积，用于错误诊断（非事件流）。

### 2.3 stream-json 行格式（5 种顶层 type）

每行一个独立 JSON，顶层 `type` 分派。**真实示例见 fixture，下面是契约字段**：

| 顶层 type | subtype / event.type | 关键字段 | 用途 |
|---|---|---|---|
| `system` | `subtype=init` | `session_id, model, tools, claude_code_version` | 会话初始化 |
| `system` | `subtype=status` | `status="requesting"` | 状态更新 |
| `system` | `subtype=api_retry` | `retry_count, max_retries, wait_seconds, error` | 限流重试信号 |
| `stream_event` | `content_block_delta` + `delta.type=text_delta` | `delta.text` | 文本增量 |
| `stream_event` | `content_block_delta` + `delta.type=thinking_delta` | `delta.thinking` | 思考增量 |
| `stream_event` | `content_block_delta` + `delta.type=input_json_delta` | `delta.partial_json` | 工具参数增量 |
| `assistant` | —（完整 message） | `message.content[]`（`text`/`thinking`/`tool_use`） | 完整回合消息 |
| `user` | —（tool_result） | `message.content[].tool_use_id, content, is_error` | 工具结果 |
| `result` | `subtype=success`/error | `result, total_cost_usd, usage{...}, duration_ms, is_error` | 最终结果 + 用量 |

### 2.4 超时 / 退出码 / 错误判定（互斥有序）

子进程结果判定**有序且互斥**（每类有自己的 error phase，见 §6 错误映射）：
1. `timed_out`（超时） → phase=`timeout`
2. `exit_code != 0` → phase=`spawn`
3. `result.is_error == true` → phase=`stream`
4. 没有非 error 的 `result` 事件（exit 0 但流里无 result） → phase=`result_parse`

**退出码 0 不算成功**，还必须流里有非 error 的 `result` 事件。

### 2.5 超时处理

- `asyncio.wait_for(proc.wait(), timeout)` 超时 → **SIGTERM** → 等 10s grace → 仍存活则 **SIGKILL**。
- **kill 单进程**（非 killpg / 非 setsid），与 AgentHarness 一致。
- 超时后 drain task 用 `asyncio.gather(..., return_exceptions=True)` 收尾。

### 2.6 env 透传

- 以 `dict(os.environ)` 为 base，叠加 profile 声明的 `env_overlay_prefixes`（claude = `("ANTHROPIC_", "CLAUDE_")`）对应的 overlay。
- `cli_path` 走 profile 的 `resolve_cli_path()`（env > default，运行时读）。

### 2.7 结构化输出

claude `-p --output-format stream-json` **不配合 JSON schema**。结构化输出**自己实现两层**：
1. **JSON 提取**（从 `result.result` 文本）：纯 JSON / ```json fence``` / 第一个平衡 `{...}` 或 `[...]` 块。
2. **schema 校验**：`node.output_schema is not None` 时校验提取出的 JSON，失败 fail loud（emit `node_failed` + error 事件，phase=`schema`）。

---

## 3. stream-json → Orca Event 映射（核心契约，已拍板）

> 三条映射决策（已确认）：①text/thinking **增量片段 text@seq**；②input_json_delta **不翻译**（只在完整 assistant 消息发工具调用）；③usage **只在最终 result 发一次**（累积值）。

### 3.1 映射表

| stream-json 输入 | 产出的 Orca Event | data payload |
|---|---|---|
| `content_block_delta` + `text_delta` | `agent_message` | `{text: delta.text}`（增量片段） |
| `content_block_delta` + `thinking_delta` | `agent_thinking` | `{text: delta.thinking}`（增量片段） |
| `content_block_delta` + `input_json_delta` | **（不翻译）** | —（避免拼接 JSON 片段） |
| `assistant` 含 `tool_use` block | `agent_tool_call` | `{tool, args, tool_call_id}`（完整 input） |
| `user` 含 `tool_result` | `agent_tool_result` | `{tool_call_id, result}`（截断到合理长度） |
| `result` + `subtype=success` | 见 §3.3（驱动 node_completed） | `{raw_result, usage?, cost?}` |
| `result` + `is_error=true` | （translator 不 emit success） | 触发 executor 的错误路径 §6 |
| `system` + `subtype=init` | （不强制 emit） | 可选：记录 model/version 到日志 |
| `system` + `subtype=api_retry` | `error`（warning 级，phase=`api_retry`）| `{error_type, message, retry_count, wait_seconds}` |
| 未知 type | （不抛） | translator 返回 `[]`（fail loud 在 executor 层：记 debug 日志） |

### 3.2 session_id 归属（已拍板：executor 内部生成）

每次 `exec()` 调用，ClaudeExecutor 在**入口处生成一个 `session_id`**（`uuid.uuid4().hex`），所有 yield 的 Event 顶层都带这个 session_id。这契合 phase 3 SPEC §3.5「session_id 是 agent 调用身份，retry/for_each/parallel 每次调用一个」。

> 注意：这个 session_id 是 **Orca 的 agent 调用身份**，与 claude 自己流里的 `session_id` 字段（fixture 行 3）**不同**——后者是 claude 内部会话 id，Orca 不复用（避免外部 id 注入 Orca 身份层）。

### 3.3 result 事件 → node_completed / agent_usage

`result` 事件是单个 stream-json 行里信息量最大的。Translator 要做两件事（返回多个 Event）：

1. **agent_usage**（仅当 `usage_tracking=True` 且 fixture 含 usage）：
   ```
   {input_tokens, output_tokens, cache_tokens, cost_usd}
   ```
   - `cache_tokens` = `usage.cache_read_input_tokens`
   - `cost_usd` = 顶层 `total_cost_usd`
   - **只发一次**（result 事件唯一），避免重复计数。reducer 对 agent_usage 是 no-op（phase 3 已定），usage 聚合归 phase 5 orchestrator。

2. **最终 result 文本**：translator 把 `result.result` 通过返回值契约交给 executor（见 §4.2），executor 据此构造 `node_completed` 的 output（经结构化提取 §2.7）。translator **不直接 emit node_completed**——那是 executor 的职责（因为 node_completed 需要结构化提取 + schema 校验，超出纯函数 translator 范围）。

> **契约**：translator 是纯函数 `(line, session_id) -> list[Event]`，但 result 的最终文本提取需要 executor 协作。方案：translator 在遇到 `result` 行时，把 `result.result` 挂到返回的某个 `agent_usage`/自定义事件 data 里（如 `data["_raw_result"]`），executor 读它做结构化提取。**或者**（更干净）：CLIRunner 维护一个 `on_result` 钩子，translator 解析 result 行时回调它。**SPEC 采用后者**（见 §4.3），保持 translator 纯函数 + executor 持有状态。

---

## 4. exec/ 模块设计

### 4.1 文件结构

```
orca/exec/
├── __init__.py            # 导出 Executor, make_executor, ClaudeExecutor, ScriptExecutor, SetExecutor, RunContext, ExecError
├── context.py             # RunContext（节点间数据传递契约）
├── interface.py           # Executor 抽象基类（ABC）
├── factory.py             # make_executor(node) → Executor（按 node.kind / executor 分派）
├── error.py               # ExecError（含 phase / error_type / message）+ 错误→事件映射
├── runner.py              # CLIRunner（通用 asyncio 子进程：spawn/stdin/stdout/timeout/kill）
├── render.py              # prompt + command 渲染（Jinja2，共享给 agent/script/set）
├── claude/
│   ├── __init__.py
│   ├── translator.py      # ClaudeTranslator：stream-json 行 → list[Event]（纯函数）
│   ├── result_extractor.py# result 文本 → JSON 提取 + schema 校验（纯函数）
│   └── executor.py        # ClaudeExecutor（CLIRunner + Translator + 结构化提取）
├── script.py              # ScriptExecutor（subprocess 跑 shell）
└── set_node.py            # SetExecutor（Jinja2 求值存值）
```

### 4.2 Executor 接口（Layer 1，后端无关）

```python
# orca/exec/interface.py
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from orca.schema import Event, AgentNode, ScriptNode, SetNode, Node
from orca.exec.context import RunContext

class Executor(ABC):
    """执行单个 node，产出事件流。后端无关契约。

    产出的事件流由上层（phase 5 orchestrator）逐个 bus.emit(..., session_id=...)。
    executor 不直接写 tape（依赖单向：exec→events 是消费关系，写 tape 归 orchestrator）。
    """
    @abstractmethod
    async def exec(self, node: Node, ctx: RunContext) -> AsyncIterator[Event]:
        """执行 node，yield 事件。事件流必须包含完整生命周期：
        node_started → (流式事件...) → node_completed | node_failed。
        session_id 由 executor 内部生成（每个 Event 顶层带）。"""
```

**事件生命周期契约**（所有 Executor 必须遵守）：

```
node_started(node, session_id)
  ├── agent_message / agent_thinking（agent kind，流式）
  ├── agent_tool_call / agent_tool_result（agent kind，每回合）
  ├── agent_usage（agent kind，仅 result 时一次）
  └── foreach_*/set/script 各自的中间事件（无）
node_completed(node, session_id, data={output, elapsed})
  —— 或 ——
node_failed(node, session_id, data={error_type, message, phase})
```

> **session_id 必须在所有事件顶层**（Event.session_id 字段，phase 1 已定义）。executor 生成一次，全程复用。

### 4.3 ClaudeExecutor（Layer 2，claude -p 路线）

```python
# orca/exec/claude/executor.py
class ClaudeExecutor(Executor):
    def __init__(self, profile: CliProfile): ...

    async def exec(self, node: AgentNode, ctx: RunContext) -> AsyncIterator[Event]:
        session_id = uuid4().hex
        yield Event(type="node_started", node=node.name, session_id=session_id, ...)

        # 1. 渲染 prompt（render.py：node.prompt 或 agents/<name>.md + Jinja2 ctx）
        prompt = render_prompt(node, ctx)

        # 2. 构造 spawn config（argv / env / mcp flag，读 profile）
        cfg = build_spawn_config(node, self.profile, prompt)

        # 3. CLIRunner 跑子进程，逐行喂 translator
        runner = CLIRunner(cfg)
        result_holder = {"result_text": None, "usage": None}

        async def on_result(raw_result: str, usage: dict, cost: float):
            result_holder["result_text"] = raw_result
            result_holder["usage"] = usage
            result_holder["cost"] = cost

        try:
            async for line in runner.stream():
                for event in self.profile.translator(line, session_id):
                    yield event  # agent_message/thinking/tool_call/tool_result/agent_usage
                # CLIRunner 内部检测到 result 行时回调 on_result
            # 4. 判定（§2.4 有序）：timeout / exit / stream_error / no_result / success
            if runner.timed_out:
                raise ExecError(phase="timeout", ...)
            if runner.exit_code != 0:
                raise ExecError(phase="spawn", ...)
            if result_holder["result_text"] is None:
                raise ExecError(phase="result_parse", message="claude exit 0 but no result event")

            # 5. 结构化提取（§2.7）
            output = extract_and_validate(result_holder["result_text"], node.output_schema)

            yield Event(type="node_completed", node=node.name, session_id=session_id,
                        data={"output": output, "elapsed": runner.elapsed})

        except ExecError as e:
            yield Event(type="node_failed", node=node.name, session_id=session_id,
                        data={"error_type": e.error_type, "message": e.message, "phase": e.phase})
            yield Event(type="error", node=node.name, session_id=session_id,
                        data={"error_type": e.error_type, "message": e.message, "phase": e.phase})
```

**关键约束**：
- `translator` 来自 `profile.translator`（phase 3 是 dummy，本阶段替换为真实现）。
- **node_completed 的 output 走结构化提取**：`output_schema=None` → output = 原始 result 文本（自由文本）；非 None → JSON 提取 + schema 校验，失败 raise ExecError(phase="schema")。
- **错误映射在 executor 层**（§6），translator 不抛业务错。

### 4.4 CLIRunner（通用子进程基础设施）

```python
# orca/exec/runner.py
@dataclass
class SpawnConfig:
    cli_path: str           # profile.resolve_cli_path()（未 shlex 拆分的原始串）
    flags: tuple[str, ...]  # profile.flags
    extra_args: list[str]   # 动态：--model / --allowed-tools / --append-system-prompt
    mcp_flag_args: list[str]# --mcp-config <path>（mcp 落地后，本阶段为空）
    prompt: str             # 渲染后的 prompt（prompt_channel=stdin 时投递）
    prompt_channel: Literal["stdin", "argv"]
    env_overlay: dict[str, str]
    timeout: float | None

@dataclass
class CliRunResult:
    exit_code: int          # -1 if 未知
    stderr: str             # 累积 stderr（错误诊断）
    timed_out: bool
    elapsed: float          # 墙钟秒

class CLIRunner:
    def __init__(self, cfg: SpawnConfig, on_result: Callable | None = None): ...
    async def stream(self) -> AsyncIterator[str]:
        """yield stdout 每一行（已 decode + rstrip）。spawn → stdin pump → readline 循环。
        遇到 result 行（json.loads 后 type==result）回调 on_result。
        超时/退出码记入 CliRunResult。"""
    @property
    def timed_out(self) -> bool: ...
    @property
    def exit_code(self) -> int: ...
    @property
    def elapsed(self) -> float: ...
```

**关键约束**：
- `stream()` 是 async generator，逐行 yield stdout（translator 喂它）。
- **result 行检测在 CLIRunner**（不是 translator）：`json.loads(line)` 后若 `type=="result"`，回调 `on_result(raw_result, usage, cost)`。translator 仍翻译该行的 agent_usage 事件，但 result 文本通过 on_result 交给 executor（避免 translator 持状态）。
- **不 fsync**，靠 OS buffer；崩溃最多丢最后一行（与 phase 3 tape 一致的崩溃语义）。

### 4.5 ClaudeTranslator（纯函数，可测性根基）

```python
# orca/exec/claude/translator.py
def claude_translator(line: str, session_id: str) -> list[Event]:
    """stream-json 一行 → list[Event]。纯函数，无副作用，无状态。

    按顶层 type 分派（§3.1）：
      - stream_event + text_delta → [agent_message(text=delta.text, session_id)]
      - stream_event + thinking_delta → [agent_thinking(...)]
      - stream_event + input_json_delta → []  # 不翻译
      - assistant 含 tool_use → [agent_tool_call(...)]
      - user 含 tool_result → [agent_tool_result(...)]
      - result + success → [agent_usage(...)]  # usage 事件；result 文本走 on_result
      - system/api_retry → [error(warning)]   # 可见但不阻断
      - 未知 → []  # debug log
    """
```

**纯函数性是 phase 4 可测性的根基**：用录制的真实 stream-json 行做输入，断言产出 Event，**不 spawn claude**。

### 4.6 ScriptExecutor / SetExecutor

```python
# orca/exec/script.py
class ScriptExecutor(Executor):
    async def exec(self, node: ScriptNode, ctx: RunContext) -> AsyncIterator[Event]:
        session_id = uuid4().hex
        yield node_started
        cmd = render_command(node.command, ctx)  # Jinja2 渲染（render.py 共享）
        proc = await asyncio.create_subprocess_shell(cmd, stdout=PIPE, stderr=PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), node.timeout)
        output = {"stdout": stdout.decode(), "stderr": stderr.decode(), "exit_code": proc.returncode}
        if node.parse_json:
            output["json"] = try_parse_json(output["stdout"])  # 失败 None（不 fail loud，仅降级）
        yield node_completed(data={"output": output})

# orca/exec/set_node.py
class SetExecutor(Executor):
    async def exec(self, node: SetNode, ctx: RunContext) -> AsyncIterator[Event]:
        session_id = uuid4().hex
        yield node_started
        output = {k: render_jinja2(v, ctx) for k, v in node.values.items()}  # Jinja2 求值
        yield node_completed(data={"output": output})
```

**关键约束**：
- ScriptExecutor **非零退出码不 fail loud**（脚本退出码是业务语义，由路由判断，见 nas.yaml evaluator 的 `output.exit_code == 0`）。但 **timeout 必须 fail loud**（emit node_failed + phase=timeout）。
- SetExecutor 纯计算，无失败路径（除非 Jinja2 渲染错，那 fail loud）。
- 三种 Executor 共享 `render.py`（Jinja2 渲染）和 `error.py`（错误映射）。

### 4.7 RunContext（节点间数据传递契约）

```python
# orca/exec/context.py
@dataclass(frozen=True)
class RunContext:
    """执行单个 node 时的上下文。由 phase 5 orchestrator 构造传给 executor。

    - inputs: workflow 输入（{{ inputs.iterations }}）
    - outputs: 已完成 node 的输出累积（{{ optimizer.output.structure }}）
    - run_id: 当前 run id（透传到事件 / 日志）
    """
    inputs: dict[str, Any]
    outputs: dict[str, Any]   # {node_name: node_output}
    run_id: str
```

**Jinja2 渲染规则**（render.py）：
- `{{ node_name.output.field }}` → 从 `ctx.outputs[node_name]` 取
- `{{ inputs.x }}` → 从 `ctx.inputs` 取
- `{{ item }}` / `{{ _index }}`（foreach body）→ foreach 注入（phase 5，本阶段 foreach 不做）
- 渲染失败 fail loud（raise ExecError → emit node_failed）

---

## 5. 不做的事（明确边界）

- ❌ **编排**（单指针推进 / parallel 组并行 asyncio.gather / foreach 分批 / 路由 first-match-wins / 循环控制）—— phase 5
- ❌ **retry**（跨 node 重试 / EvalJudge 评判循环）—— phase 5（本阶段只做单次执行 + 超时错误映射）
- ❌ **interrupt**（中途打断）—— 本阶段不做（capabilities.interrupt=True 但实现留后）
- ❌ **checkpoint_resume**（`--resume` / `--bg`）—— 未来扩展
- ❌ **HMIL / PreToolUse hook** —— phase 8（本阶段 `--permission-mode auto` 跑通即可）
- ❌ **MCP 配置**（`--mcp-config`）—— mcp_flag_args 本阶段为空，留 phase 9
- ❌ **WebSocket / CLI 渲染** —— phase 7 / 6
- ❌ **跨工具**（codex / opencode / ccr executor）—— 本阶段只 claude；profile 系统已支持，加 backend = 换 translator
- ❌ **SDK 路线**（claude SDK 直连）—— 本阶段不碰；record 在 REFERENCES.md（CliProfile 当前纯 CLI 语义，接 SDK 是未来架构决策）
- ❌ **迁移 AgentHarness 代码** —— 重写（CLAUDE.md 重写原则）

---

## 6. 错误映射（fail loud）

所有失败路径产 `node_failed` + `error` 事件（双重，node_failed 给状态机，error 给诊断）。

| phase | 触发条件 | error_type | 说明 |
|---|---|---|---|
| `timeout` | `proc.wait()` 超时 | `ExecTimeout` | SIGTERM → 10s → SIGKILL |
| `spawn` | `exit_code != 0` | `CliExitNonZero` | 附 stderr 末尾 N 行 |
| `stream` | `result.is_error == true` | `ClaudeStreamError` | claude 自己报错（含 api_error_status） |
| `result_parse` | exit 0 但无 result 事件 | `NoResultEvent` | claude 异常退出但码正常 |
| `schema` | 结构化提取 / schema 校验失败 | `SchemaValidationError` | output_schema 校验不过 |
| `render` | Jinja2 prompt/command 渲染失败 | `RenderError` | 模板引用未定义变量等 |
| `json_decode` | stdout 行非合法 JSON（debug log，不阻断）| — | 静默跳过该行（claude 偶发非 JSON 行） |
| `api_retry` | system/api_retry 事件 | `ApiRetry`（warning） | 可见但不阻断（claude 自退避重试） |

**判定有序且互斥**（§2.4）。`json_decode` 是例外——不 fail loud，debug log + 跳过（claude stream-json 偶有非 JSON 心跳行）。

---

## 7. 验收标准

### 7.0 验收总则（5 条铁律，违反即返工）

1. **依赖单向**：`exec→schema+events+profiles`，exec **不依赖 run/compile**。make_executor 读 profiles，executor 消费 schema Event。判据：import 图无环。
2. **Executor 不写 tape**：exec 产出 `AsyncIterator[Event]`，写 tape + bus.emit 归 phase 5 orchestrator。判据：exec/ 无 `from orca.events.bus import EventBus`（只 import Event 类型）。
3. **Translator 纯函数**：`(line, session_id) -> list[Event]`，无副作用无状态。判据：有纯函数测试（fixture 输入 → 断言输出，不 spawn）。
4. **fail loud**：timeout / 非零退出 / stream error / 无 result / schema 错 / 渲染错 → 全 emit node_failed + error，不静默吞。
5. **session_id 一致性**：单次 exec() 内所有 Event 的 session_id 相同（executor 入口生成，全程复用）。

### 7.1 结构
- [ ] `orca/exec/` 下 interface/context/factory/error/runner/render + claude/{translator,result_extractor,executor} + script + set_node + __init__
- [ ] `from orca.exec import Executor, make_executor, ClaudeExecutor, ScriptExecutor, SetExecutor, RunContext, ExecError`

### 7.2 Executor 接口
- [ ] `Executor` ABC，`async exec(node, ctx) -> AsyncIterator[Event]`
- [ ] 事件生命周期：node_started → 流式 → node_completed|node_failed
- [ ] session_id 单次 exec 内全程一致

### 7.3 ClaudeExecutor
- [ ] spawn `claude -p --output-format stream-json ...`（flags 来自 profile）
- [ ] prompt 走 stdin（prompt_channel=stdin）
- [ ] argv 动态：`--model`（node.model）/ `--allowed-tools`（node.tools）/ `--append-system-prompt`（agents/<name>.md）
- [ ] 用 fixture（真实 stream-json）驱动：translator 把 fixture 行翻译成正确 Event
- [ ] result 事件触发 on_result → 结构化提取 → node_completed output
- [ ] output_schema=None → output=原始文本；非 None → JSON 提取 + 校验

### 7.4 ClaudeTranslator（纯函数，核心可测点）
- [ ] text_delta → agent_message（增量片段）
- [ ] thinking_delta → agent_thinking（增量片段）
- [ ] input_json_delta → []（不翻译）
- [ ] assistant tool_use → agent_tool_call（完整 input）
- [ ] user tool_result → agent_tool_result
- [ ] result success → agent_usage（含 input/output/cache/cost）
- [ ] result is_error → []（executor 层处理）
- [ ] 未知 type → []（不抛）
- [ ] **所有产出的 Event.session_id == 入参 session_id**

### 7.5 CLIRunner
- [ ] spawn → stdin pump（写 + close）→ readline 循环
- [ ] result 行回调 on_result
- [ ] 超时 → SIGTERM → 10s → SIGKILL（用假子进程测，不 spawn claude）
- [ ] exit_code / timed_out / elapsed 记录

### 7.6 错误映射（fail loud）
- [ ] timeout → node_failed(phase=timeout)
- [ ] exit_code!=0 → node_failed(phase=spawn)
- [ ] is_error → node_failed(phase=stream)
- [ ] 无 result → node_failed(phase=result_parse)
- [ ] schema 校验失败 → node_failed(phase=schema)
- [ ] 渲染失败 → node_failed(phase=render)

### 7.7 ScriptExecutor / SetExecutor
- [ ] script：渲染 command → subprocess → node_completed({stdout,stderr,exit_code,json?})
- [ ] script timeout → node_failed(phase=timeout)；非零退出码**不** fail loud
- [ ] script parse_json 失败 → output.json=None（降级不阻断）
- [ ] set：Jinja2 求值 values → node_completed({key: 求值结果})

### 7.8 make_executor 分派
- [ ] AgentNode → ClaudeExecutor（读 node.executor → get_profile）
- [ ] ScriptNode → ScriptExecutor
- [ ] SetNode → SetExecutor
- [ ] ForeachNode → raise NotImplementedError（归 phase 5）

### 7.9 Jinja2 渲染（render.py，三种 executor 共享）
- [ ] `{{ node.output.field }}` → ctx.outputs[node]
- [ ] `{{ inputs.x }}` → ctx.inputs[x]
- [ ] 渲染失败 → raise ExecError(phase=render)

### 7.10 端到端（不 spawn claude，用 fixture 模拟整流）
- [ ] 用 fixture 行序列驱动 ClaudeExecutor（mock CLIRunner.stream 逐行 yield fixture 行）→ 断言产出完整事件流（node_started → messages → tool_call/result → usage → node_completed）
- [ ] 真 spawn claude：`@pytest.mark.integration`，CI skip，本地有 key 可选跑（可选验证项）

### 7.11 测试
- [ ] `tests/exec/claude/test_translator.py`：纯函数，fixture 行 → 断言 Event（**核心，不 spawn**）
- [ ] `tests/exec/claude/test_result_extractor.py`：JSON 提取（fence/balanced block）+ schema 校验
- [ ] `tests/exec/claude/test_executor.py`：mock CLIRunner → 端到端事件流 + 错误映射
- [ ] `tests/exec/test_runner.py`：假子进程测 spawn/stdin/timeout/kill/exit_code
- [ ] `tests/exec/test_script.py`：subprocess + timeout + parse_json 降级
- [ ] `tests/exec/test_set.py`：Jinja2 求值
- [ ] `tests/exec/test_render.py`：Jinja2 渲染 + 失败路径
- [ ] `tests/exec/test_factory.py`：分派正确
- [ ] `tests/exec/claude/conftest.py`：真实 stream-json fixture（从 AgentHarness 录制，**只读不改**）
- [ ] 全部通过（含 phase 1+2+3 不回归，**196 基线 + phase 4 新增**）

---

## 8. 与前序阶段的衔接

| 前序 | 衔接点 |
|---|---|
| phase 1 schema | 消费 `Event/EventType/AgentNode/ScriptNode/SetNode/RunState`；Event.session_id 已定义 |
| phase 3 events | executor 产出 `AsyncIterator[Event]`，**不直接 emit**——上层 orchestrator 拿迭代器逐个 `bus.emit(..., session_id=...)` |
| phase 3 profiles | `make_executor(node) → get_profile(node.executor) → ClaudeExecutor(profile=...)`；**exec 永远不硬编码 binary/flag**，只读 profile；本阶段替换 phase 3 的 dummy translator 为真实现 |

---

## 9. 给后续阶段的接口契约

| 后续 | phase 4 提供 |
|---|---|
| phase 5 run | `make_executor(node) -> Executor`；`async executor.exec(node, ctx) -> AsyncIterator[Event]`；`RunContext(inputs, outputs, run_id)`。orchestrator 拿迭代器逐个 emit + 写 tape；单指针推进/parallel 组/路由/foreach 在 orchestrator |
| phase 6 cli | 事件流经 bus → Rich 渲染（agent_message/tool_call 等） |
| phase 8 gates | PreToolUse hook 拦 claude 工具调用（本阶段 `--permission-mode auto` 跑通，hook 是 phase 8 加） |

---

## 10. 关键决策备忘（写进 SPEC 防 drift）

1. **Executor 接口 = `AsyncIterator[Event]`**（非回调），契合事件唯一真相源
2. **text/thinking 增量片段 text@seq**（驱动 agent_message/thinking，靠 phase 3 reducer 幂等）
3. **input_json_delta 不翻译**（工具调用只在完整 assistant 消息发，避免 JSON 片段拼接）
4. **usage 只在 result 发一次**（累积值，避免重复计数）
5. **session_id executor 内部生成**（每次 exec 一个 uuid4，与 claude 流里的 session_id 区分）
6. **result 文本走 on_result 钩子**（保持 translator 纯函数；executor 持状态做结构化提取）
7. **node_completed 的 output 走结构化提取**（output_schema 决定自由文本 vs JSON 校验）
8. **三种叶子 node 都做**（agent/script/set），foreach 归 phase 5
9. **Translator 纯函数是可测性根基**（fixture 驱动，不 spawn）
10. **executor 不写 tape**（产出迭代器，写 tape 归 orchestrator；依赖单向）
11. **只 claude -p 路线**，不碰 SDK（SDK 是未来架构决策，CliProfile 当前纯 CLI 语义）
12. **重写不迁移**（采纳 AgentHarness 的客观协议事实，不抄其框架代码）
13. **错误判定有序互斥**（timeout/spawn/stream/result_parse/schema/render，json_decode 例外不阻断）

---

## 11. 迁移资产说明（不迁移，重写）

| 协议事实 | 来源 | 处理 |
|---|---|---|
| claude -p argv 协议 | AgentHarness `cli_profiles/claude.py` | 🟢 采纳事实（CLAUDE_FLAGS），重写实现 |
| stream-json 行格式 | AgentHarness fixture（claude_code_version 2.1.150）| 🟢 采纳协议（fixture 只读用作测试输入），重写 translator |
| stdin pump / readline / timeout SIGTERM→SIGKILL | AgentHarness `_cli_subprocess.py` | 🟢 采纳子进程模式，重写 CLIRunner |
| JSON 提取（fence/balanced block）| AgentHarness `_result_extractor.py` | 🟢 采纳提取算法，重写 |
| Executor ABC / capabilities | Conductor `providers/base.py` | 🔵 借鉴设计（AsyncIterator 差异化，不抄回调式） |
| `_make_json_safe` | Conductor `event_log.py` | 🔵 phase 3 已实现于 tape.py，本阶段不重复 |

**fixture 只读不改**：AgentHarness 的 `sample_with_bash.jsonl` 作为 phase 4 测试输入（真实 stream-json），拷贝到 `tests/exec/claude/conftest.py` 或 fixtures 目录，**不修改原文件**。

---

## 12. 测试策略

| 层 | 方法 | spawn claude？ |
|---|---|---|
| Translator（纯函数）| fixture 行 → 断言 Event | ❌ 不 spawn |
| result_extractor（纯函数）| 文本 → 断言 JSON | ❌ 不 spawn |
| render（纯函数）| Jinja2 → 断言输出 | ❌ 不 spawn |
| CLIRunner | 假子进程（mock create_subprocess_exec）测 stdin/timeout/kill/exit | ❌ 不 spawn |
| ClaudeExecutor | mock CLIRunner.stream（逐行 yield fixture 行）→ 断言事件流 | ❌ 不 spawn |
| ScriptExecutor | 真 subprocess 跑 `echo`/`exit 1` 等无害命令 | ❌ 不 spawn claude |
| 真 claude | `@pytest.mark.integration`，CI skip，本地可选 | ✅ spawn（可选） |

**核心可测性**：translator / extractor / render 都是纯函数，phase 4 主体测试**不需要 API key、不需要 spawn claude、CI 确定且快**。真 spawn 是可选的烟雾测试。

---

## 13. 开发计划（与 `docs/plans/2026-06-30-phase4-exec.md` 对齐）

phase 4 拆为 **5 个可独立验收的步骤**（依赖顺序：A → B → C → D → E）：

| 步骤 | 模块 | 关键产出 | 依赖 |
|---|---|---|---|
| **A** | 契约层 | interface.py / context.py / error.py / factory（骨架） | schema + profiles（已就位）|
| **B** | 共享基础设施 | render.py（Jinja2）/ CLIRunner（假子进程可测） | A |
| **C** | ClaudeExecutor 核心 | claude/{translator, result_extractor, executor} + fixture | A + B |
| **D** | script / set | ScriptExecutor / SetExecutor | A + B |
| **E** | 集成 | make_executor 真分派 + 端到端（fixture 驱动）+ integration 标记 | C + D |

详见开发计划文档：[`docs/plans/2026-06-30-phase4-exec.md`](../plans/2026-06-30-phase4-exec.md)

---

## 14. TASK4 完整描述（可直接给新 session）

> 以下是给实现 session 的完整任务描述。复制它作为新 session 的第一条消息。

---

你是一名资深 Python 工程师，在 **Orca** 项目里实现【阶段 4：exec/ 执行内核（单 node 执行层）】。

## 必读文档（按顺序读，不要跳过）
1. `CLAUDE.md` —— 协作规则 + **重写原则（不迁移 AgentHarness 代码）**
2. `docs/PLAN.md` + `docs/TASK.md` —— 整体计划 + §2 双层抽象 + §7 权限模型
3. `docs/specs/phase-1-schema.md` —— Event/AgentNode/ScriptNode/SetNode 定义（你消费）
4. `docs/specs/phase-3-events.md` §3.4 §10 —— reducer 幂等（text@seq）+ §10 接口契约
5. **`docs/specs/phase-4-exec.md`** —— **你的任务 SPEC**（逐字实现，契约不是建议）
6. **`docs/plans/2026-06-30-phase4-exec.md`** —— **开发计划**（步骤 A-E + 验收）
7. `orca/schema/` + `orca/events/replay.py` + `orca/profiles/` —— 已实现前序阶段

## 你的任务（5 个步骤，A→E）

### 步骤 A：契约层
- `orca/exec/interface.py` —— Executor ABC（`async exec(node, ctx) -> AsyncIterator[Event]`）
- `orca/exec/context.py` —— RunContext（inputs/outputs/run_id，frozen dataclass）
- `orca/exec/error.py` —— ExecError（phase/error_type/message）+ 错误映射表
- `orca/exec/factory.py` —— make_executor 骨架（先 raise NotImplementedError，E 步填）

### 步骤 B：共享基础设施
- `orca/exec/render.py` —— Jinja2 渲染（`{{ node.output }}` / `{{ inputs.x }}`），失败 raise ExecError
- `orca/exec/runner.py` —— CLIRunner（asyncio subprocess + stdin pump + readline + timeout SIGTERM→SIGKILL + on_result 钩子）

### 步骤 C：ClaudeExecutor（核心）
- `orca/exec/claude/translator.py` —— claude_translator 纯函数（按 §3.1 映射表，**含 fixture 测试**）
- `orca/exec/claude/result_extractor.py` —— JSON 提取（fence/balanced）+ schema 校验
- `orca/exec/claude/executor.py` —— ClaudeExecutor（CLIRunner + translator + 结构化提取 + 错误映射）
- `tests/exec/claude/conftest.py` —— 从 AgentHarness 录制 fixture（只读不改）

### 步骤 D：script / set
- `orca/exec/script.py` —— ScriptExecutor（subprocess + timeout + parse_json 降级）
- `orca/exec/set_node.py` —— SetExecutor（Jinja2 求值）

### 步骤 E：集成
- `orca/exec/factory.py` —— make_executor 真分派（agent→ClaudeExecutor / script→ScriptExecutor / set→SetExecutor / foreach→NotImplementedError）
- 端到端测试（mock CLIRunner → fixture 行流 → 完整事件流断言）
- integration 标记（真 spawn claude，CI skip）

## 强制约束（违反即返工，详见 SPEC §7.0）

### 5 条铁律
1. **依赖单向**：exec→schema+events+profiles，不依赖 run/compile
2. **executor 不写 tape**：产出 AsyncIterator，写 tape 归 phase 5 orchestrator
3. **Translator 纯函数**：(line, session_id)→list[Event]，fixture 驱动测试，不 spawn
4. **fail loud**：6 类错误全 emit node_failed + error
5. **session_id 一致性**：单次 exec 内全程一致

### 协议映射铁律
6. text/thinking **增量片段**（agent_message/thinking），不拼接
7. input_json_delta **不翻译**（工具调用只在完整 assistant 消息发）
8. usage **只在 result 发一次**（累积值）
9. result 文本走 **on_result 钩子**（translator 保持纯函数）
10. session_id **executor 内部生成**（uuid4，与 claude 流里的 session_id 区分）

## 不做的事（明确边界）
- ❌ 编排（单指针推进/parallel 组/foreach/路由/循环）—— phase 5
- ❌ retry / interrupt / checkpoint_resume —— 后续
- ❌ MCP 配置 / HMIL hook —— phase 9/8
- ❌ SDK 路线 / codex 等其他 backend —— 本阶段只 claude -p
- ❌ 迁移 AgentHarness 代码 —— 重写

## 验收标准（SPEC §7，全部必须通过）
- [ ] exec/ 结构 + import
- [ ] Executor 接口：AsyncIterator[Event] + 事件生命周期 + session_id 一致
- [ ] ClaudeTranslator 纯函数：fixture 驱动，9 种输入映射（§7.4）
- [ ] CLIRunner：假子进程测 stdin/timeout/kill/exit（§7.5）
- [ ] ClaudeExecutor：mock CLIRunner → 完整事件流 + 6 类错误映射（§7.3 §7.6）
- [ ] ScriptExecutor：timeout fail loud / 非零退出不 fail loud / parse_json 降级（§7.7）
- [ ] SetExecutor：Jinja2 求值（§7.7）
- [ ] make_executor 分派（§7.8）
- [ ] Jinja2 渲染共享层（§7.9）
- [ ] 端到端 fixture 驱动（§7.10）
- [ ] 全部测试通过（196 基线 + phase 4 新增，零回归）

## 工作流程（SDD）
1. 先读 7 份必读文档
2. 按 `docs/plans/2026-06-30-phase4-exec.md` 执行步骤 A→E，每步实现后跑对应测试 + 自检 §7.0 五条铁律
3. 实现完成后，**自我 review**（分发 review agent 检查：依赖单向、executor 不写 tape、translator 纯函数、错误映射完整、session_id 一致、不迁移只重写）
4. 在 `docs/releases/2026-06-30-phase4-exec.md` 写 release note
5. 更新 `docs/status/CURRENT.md` + CHANGELOG

**不要**：实现 run/web/gates/mcp —— 后续阶段。
**不要**：自作主张加 SPEC 没有的机制。SPEC §5 列了不做的事，你的设计如果需要编排/retry/interrupt/MCP，scope 越界了。
