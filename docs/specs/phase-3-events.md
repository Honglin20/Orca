# 阶段 3 SPEC —— events/ 事件层 + profiles/ 命令替换层

> **状态**：最终版（待分发实现）
> **依据**：[TASK.md](../TASK.md) §3 · [PLAN.md](../PLAN.md) · [REFERENCES.md](../REFERENCES.md)
> **范围**：① events/（EventBus + Tape，唯一真相源）② profiles/（CLI 命令替换抽象 + capability 静态校验）
> **执行任务**：见文末 §13「TASK3 完整描述」

---

## 0. 设计目标（两层 + 一个校验闭环，共同对抗一个敌人）

phase 3 做两件事，并补一个校验闭环，共同对抗同一个敌人——**AgentHarness 的"多真相源 + 双路径"灾难**（详见 §1 诊断）：

| 模块 | 解决什么 | 核心交付 |
|---|---|---|
| `events/` | 消灭 streaming/replay 双路径 | EventBus + Tape（唯一真相源）|
| `profiles/` | 让 `claude -p` 可无痛替换为 ccr/codex | CliProfile + ProviderCapabilities + 注册表 |
| `profiles/validate.py` → `compile/` | capability 静态校验闭环 | `validate_workflow_profiles` 接进 compile validator |

**为什么把 profiles/ 放 phase 3 而不是 phase 4**：profiles 的能力声明（ProviderCapabilities）是静态校验输入。phase 2 compile 只做了**结构/图校验**（name/entry/引用/环/Jinja2 浅校验），**未做 capability 校验**（phase 2 时 profiles 尚不存在）。phase 3 落地 profiles 后，**本阶段补上 `validate_workflow_profiles` 并接进 compile**，使 `orca validate` 在 spawn 前就 fail loud 拒绝不兼容组合（如 `output_schema: {...}` + 不支持结构化输出的 backend）。

> **修正**：旧版 §4.8 把 capability 校验写成「phase 2 已做」——失实。grep 证实 `orca/compile/` 全无 profiles 引用。**它是 phase 3 的交付项**（见 §4.9）。

---

## 1. AgentHarness 事件层诊断（必须避免的 5 个反模式）

这是 phase 3 的"反面教材"。调研证实了根因：

> AgentHarness 把 Bus 设计成**有损的内存缓冲**（FIFO 在 2000 条驱逐，`bus.py:290`），持久化是**事后用 6 个 sidecar 文件补丁**（`+events`/`+snapshot`/`+outline`/`+charts`/`+iters+<node>+<n>`/主记录），导致每个 sidecar 都是事件流的"物化视图"但**写在不同的地方**，必然漂移。前端要合并 N 个异步来源（4 种 replay 策略 + 3 个 seq guard + 双 store + 双 WS），复杂度超线性增长。

**5 个必须避免的反模式**（每个配一条设计规则）：

| 反模式 | AgentHarness 证据 | Orca 设计规则 |
|---|---|---|
| ① 事件流有损（FIFO 驱逐）| `bus.py:290` buffer_size=2000 | **Tape append-only 永不驱逐**；有界内存在订阅者队列，不在日志 |
| ② 物化视图作为独立写目标 | 5 个 sidecar suffix（`run_store.py:56-64`）| **一个 Tape；所有视图是读时投影（纯 reducer）** |
| ③ reducer 非幂等 | `dedup.ts:14-21` 注释"append-unsafe reducers would double the content" | **reducer 是事件的纯 fold，重放两次 = 重放一次** |
| ④ streaming/replay/persistence 三条路径 | 4 种 replay 策略 + `_rebuild_bus_from_events` | **一条读路径**：`state = reduce(tape[0..N], rootReducer)` |
| ⑤ 双 store / 双 WS / 双激活管线 | global zustand vs scoped vanilla + `dummyWorkflowStore` 镜像 | **每样只有一个**：一个 store map（按 runId key）、一个 WS、一个 activate |

**自检规则**：如果 Orca 的设计需要 `_rebuild_bus_from_events`、`dummyWorkflowStore`、`decideStrategy`、dedup set 这类东西，**设计就是错的**。

---

## 2. 借鉴的成熟模式（4 个项目收敛的设计规则）

| 来源 | 借鉴 | 来源证据 |
|---|---|---|
| **Temporal** | event log = state；replay 必须确定性复现；不一致 = 硬错误（NDE）| event-history 文档 |
| **OpenHands** | EventStream = 唯一真相；state = fold over events；Action/Observation + source 词汇；FIFO Lock 单写有序 fan-out | events 架构文档 |
| **dagu #1835** | 写时分配单调 seq；per-consumer cursor；复合内容 dedup key；sink 失败隔离；**故意不用 SQLite，用文件** | GitHub issue #1835 |
| **Conductor（正面）** | 每事件 `write + flush` 一行 JSONL；run_id 传播；`_make_json_safe` 纯 JSON | `event_log.py:161-174` |
| **Conductor（反面）** | 内存 `_event_history` list 与 tape 并存 → 重启时要 `replay_events_from_jsonl` + `prepend_workflow_started` 修补 | `server.py:81,188,508` |

**12 条设计规则**（distilled，每条对应一个实现约束）：

1. **Tape 是唯一真相源**。无并行内存 list（避免 Conductor `_event_history`）
2. **一条读路径**。streaming = replay = read tape
3. **一行一事件，write + flush，append-only**。崩溃留有效前缀
4. **写时分配单调 seq**。seq 是唯一全局序键（dagu #1835）
5. **per-consumer cursor**。生产者不阻塞消费者；消费者各自记录 last_seq
6. **单点有序 fan-out**（FIFO Lock，OpenHands 风格），非 list 锁（Conductor events.py:71 只锁订阅者列表）
7. **复合内容 dedup key**（非 seq），让重发幂等
8. **消费者是事件的纯函数；分歧是错误不是补丁**（Temporal NDE 概念）
9. **sink 失败隔离**：tape 写失败要 fail loud 但不阻塞/毒化其他消费者
10. **lossy-but-pure JSON**：`_make_json_safe` 风格序列化（bytes→utf-8, Path→str）
11. **最小事件词汇 + provenance**：Action/Observation + source 字段
12. **run_id 传播 + 追加式 resume**：resume = 重开 tape 追加模式，不重建

---

## 3. events/ 模块设计

### 3.1 文件结构

```
orca/events/
├── __init__.py        # 导出 EventBus, Tape, Subscription, replay_state（Event 从 schema re-export）
├── tape.py            # Tape：append-only JSONL 持久化
├── bus.py             # EventBus：持有 Tape + 异步 fan-out
└── replay.py          # replay：从 Tape 重建 RunState（纯 reducer fold）
```

### 3.2 Tape（唯一真相源）

```python
class Tape:
    """append-only JSONL，编排层唯一真相源。永不驱逐，永不重写。"""
    def __init__(self, path: Path, run_id: str, *, resume: bool = False): ...
    def append(self, event: Event) -> int:
        """写一行 JSON + flush。返回分配的 seq（单调递增）。"""
        # seq 由 Tape 分配（写时），调用方不管 seq
    def replay(self, since_seq: int = 0) -> Iterator[Event]:
        """从 since_seq 读到底。一行一事件，容忍末尾残行。"""
    def last_seq(self) -> int: ...
    def close(self) -> None: ...
```

**关键约束**：
- **seq 由 Tape.append 分配**（写时单调自增，dagu #1835 规则 4）。调用方 emit 时只给 payload，不给 seq。
- **append 原子性（Lock 范围）**：`append` 在**一把 `asyncio.Lock` 内完成「seq 分配 + 文件 write + flush」整体**，保证 `seq 序 == 文件行序 == replay 序`（规则 6）。锁不覆盖 fan-out（fan-out 在锁外异步，不阻塞 emitter）。
- **每事件 write + flush**（Conductor `event_log.py:161-174`，不 fsync，靠 OS buffer；崩溃最多丢最后一行）。
- **append-only，永不重写/驱逐**（反模式①）。
- **`_json_safe` 序列化**：bytes/Path/未知类型 → str，保证纯 JSONL。
- **resume = 追加模式重开，且先清残行**（规则 12 + fail loud）：
  - `resume=True` 时以 append 模式打开同 run_id 的 tape；
  - **open 后先扫描末尾**：若最后一行是被崩溃截断的不完整 JSON，**截断至最后一个有效行**（绝不把新行接到残行后面产生坏行）；截断时记 warning（可见，不静默）；
  - `last_seq` 从有效事件重算，新事件从 `last_seq+1` 继续；
  - 不重建、不 synthesize 事件（反 Conductor `prepend_workflow_started`）。

### 3.3 EventBus（持有 Tape + 异步 fan-out）

```python
class EventBus:
    """编排事件总线。emit 第一动作永远是写 Tape（唯一真相），再异步通知订阅者。"""
    def __init__(self, tape: Tape): ...
    def emit(self, type: EventType, data: dict, node: str | None = None,
             session_id: str | None = None) -> Event:
        """1. 构造 Event（seq 由 Tape 分配）2. Tape.append（强制，唯一真相）3. 异步通知订阅者"""
    def subscribe(self) -> "Subscription":
        """返回一个 Subscription，自带 asyncio.Queue + cursor。"""
    def close(self) -> None: ...

class Subscription:
    """订阅者句柄。自带 cursor，生产者不阻塞消费者。"""
    async def events(self) -> AsyncIterator[Event]: ...   # drain queue
    def cancel(self) -> None: ...
```

**关键约束**：
- **EventBus 持有 Tape**（emit 第一动作 = Tape.append）。Tape 不是"可选订阅者"，是 emit 的强制副作用。
- **emit 透传 session_id**：调用方可带 `session_id`（标识本次 agent 调用）；流式事件必须带，reducer/前端按它分组（见 §3.4、§3.5）。
- **异步 fan-out**：emit 把事件 `put_nowait` 到每个订阅者的 asyncio.Queue，**不阻塞**（反模式：Conductor 同步 fan-out，慢订阅者阻塞 emitter）。
- **per-consumer cursor**（规则 5）：每个 Subscription 自带 cursor，慢订阅者不拖累快订阅者。
- **队列满策略**：`put_nowait` 失败 → 丢最老事件 + 记 warning（实时性优先；订阅者靠 replay 补全）。
- **单点有序**：seq 单调由 Tape.append 内部 Lock 保证（§3.2）；fan-out 在锁外。

### 3.4 replay（纯 reducer fold）

```python
def replay_state(tape: Tape, since_seq: int = 0) -> RunState:
    """从 tape 重放事件，fold 出 RunState。纯函数，重放两次结果相同（规则 8）。"""
    state = RunState(run_id=tape.run_id, workflow_name="", status="pending")
    for event in tape.replay(since_seq):
        state = apply_event(state, event)   # 纯 reducer
    return state

def apply_event(state: RunState, event: Event) -> RunState:
    """单一 reducer。幂等：同一事件应用两次 = 一次（规则 3 反模式③）。"""
    match event.type:
        case "workflow_started": ...
        case "node_started": state.node_status[event.node] = "running"; ...
        case "node_completed": state.node_status[event.node] = "done"; state.context[event.node] = event.data["output"]; ...
        # ... 每个 EventType 一个分支
```

**关键约束**：
- **reducer 幂等**（反模式③）：streaming text 是 `text@seq`（last-writer-wins keyed by seq），不是字符串拼接。一旦 reducer 幂等，所有 dedup set / watermark / per-node cursor 都是死代码。
- **streaming = replay = 同一个 fold**（反模式④）：live 消费和 replay 走同一个 `apply_event`。live 只是 tape 还在追加；replay 是读历史 tape。代码路径只有一条。
- **分歧 = 错误**（规则 8）：如果 replay 产出的 state 和 live 不一致，raise，绝不修补（反 Conductor 的 `prepend_workflow_started`）。
- **session_id 与 RunState**：reducer 对同 node 多 session 事件，`node_status`/`context` 取最后写入（last-writer-wins = 该 node 最终状态/输出）；session 级流式细节不进 RunState，留给前端 reducer 按 session_id 分组（phase 6）。session_id 在事件顶层保留，replay 不丢失。

### 3.5 tape 文件布局与 run 命名

**身份层级**（run 内三层身份，勿混）：

| 层级 | 字段 | 形态 | 含义 |
|---|---|---|---|
| Run（workflow 生命周期）| `run_id` | `<slug>-<ts>-<nanoid6>`（见下）| 一次 DAG 执行；目录与历史名 |
| Session（agent 调用）| `session_id` | uuid4 hex（phase 3 自生成）| 一次 agent 调用，独立 context |
| Node（DAG 步骤）| `node` | YAML 名 | 步骤标签 |

`run_id : session_id = 1 : N`。一个 node 的 retry / for_each / parallel 各产生独立 session_id。

**run_id 格式（composite，人类可读 + 唯一）**：

```
run_id = <workflow_slug>-<YYYYMMDDHHMMSS UTC>-<nanoid6>
例：nas-review-20260630143252-a3f2b1
```

- `workflow_slug`：workflow_name slugify（`NAS Review` → `nas-review`），一眼识别「哪个 workflow」
- `ts`：run **启动**时刻 UTC（固定，resume 不变；跨 bg 子进程传播同 Conductor `CONDUCTOR_RUN_ID`）
- `nanoid6`：6 位短随机，保证唯一（同秒同 workflow 不撞）

> **生成归属**：run_id 生成（slugify + ts + nanoid6）由 phase 5（orchestrator/CLI）负责；phase 3 的 Tape 仅接收 run_id 参数，不负责生成。format 在此锁定是为「一开始就定调」。

**phase 3 实际布局（单 tape，事件带 session_id 字段）**：

```
runs/nas-review-20260630143252-a3f2b1/
├── events.jsonl     # 唯一真相源（每行一个 Event JSON；流式事件带 session_id）
└── workflow.yaml    # workflow 定义副本（compile 后写入）
```

**唯一文件**。无 sidecar（反模式②）。charts/outline/snapshot 等都是**读时投影**（前端 reducer 算），不写盘。run_id 自带可读性，`ls runs/` 即历史列表，**无需 manifest**。

**文档化目标（性能成问题时激活，非 phase 3）—— session 分区**：

```
runs/<run_id>/
├── events.jsonl     # 主 tape：orchestrator 生命周期 + 路由（小，replay 快）
├── workflow.yaml
└── sessions/
    └── <session_id>.jsonl   # 该 session 的完整流式事件（懒加载单元）
```

**recoverable**：session_id 已在每个事件顶层，单 tape → 分区是无损迁移（按 session_id 拆行）。比 snapshot 更优——同时解决 replay 速度与 per-session 懒加载。

**与反模式②的区分（防漂移回 AgentHarness）**：
- AgentHarness `+iters+<node>+<n>.json` = **投影分裂**：同一事件写进主记录 + sidecar 两处 → 漂移。
- Orca session 子 tape = **session 分区**：每个事件只写一处；主 tape 是另一批事件（orchestrator 生命周期），非投影 → 不漂移。
- **判据：任一事件是否被写进 >1 个文件？** AgentHarness 是，Orca 否。

---

## 4. profiles/ 模块设计（命令替换层 + capability 校验）

### 4.1 设计决策

**Option 5（hybrid），结构为 Option 3（顶层 profiles/ 模块）**：
- `profiles/` 是顶层模块，`exec/` 与 `compile/` 依赖它（单向：compile → profiles ← schema，exec → profiles）
- workflow YAML 的 `executor: <name>` 字段是**用户选择器**
- profiles 注册表是**引擎解析器**，把 name → binary/flags/env/translator/capabilities
- 加新后端 = 丢一个 profile 文件，零 exec/factory/schema/compile 改动（OCP）

### 4.2 文件结构

```
orca/profiles/
├── __init__.py          # 导出 CliProfile, ProviderCapabilities, get_profile, register, validate_workflow_profiles
├── base.py              # CliProfile dataclass + CliSpawnConfig + 类型别名
├── capabilities.py      # ProviderCapabilities（frozen pydantic，借自 Conductor）
├── registry.py          # 注册表 + load_builtin + load_project + disable-on-failure
├── validate.py          # validate_workflow_profiles：capability 交叉校验（被 compile 调用）
└── builtin/
    ├── claude.py        # PROFILE = CliProfile(name="claude", ...)
    └── ccr.py           # PROFILE = CliProfile(name="ccr", ...)
```

### 4.3 CliProfile（核心抽象）

```python
@dataclass(frozen=True)
class CliProfile:
    # 身份
    name: str                                  # "claude" / "ccr" / "codex"
    capabilities: ProviderCapabilities         # 能力声明（借自 Conductor）

    # 如何 spawn
    cli_path_env: str                          # "ORCA_CLAUDE_CLI"（env 覆盖，运行时读）
    default_cli_path: str                      # "claude" 或 "ccr code"（shlex 拆分）
    flags: tuple[str, ...]                     # ("-p", "--output-format", "stream-json", ...)
    prompt_channel: Literal["stdin", "argv"]
    mcp_flag_template: str | None              # "--mcp-config {path}" 或 None

    # 如何配置环境
    env_overlay_prefixes: tuple[str, ...]      # ("ANTHROPIC_", "CLAUDE_")

    # 如何解析
    stream_format: Literal["json", "text"]
    translator: Translator                     # callable: stream-json line → list[Event]
    result_extractor: ResultExtractor          # callable: 解析最终 result

    # prompt 形状
    prompt_paradigm: Literal["minimal"]        # 暂只支持 minimal

    def resolve_cli_path(self) -> str:
        """env > default，运行时读（canary 切换无需重启）。"""
```

### 4.4 ProviderCapabilities（借自 Conductor，frozen pydantic）

```python
class ProviderCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # 能力声明，profiles/validate.py 静态校验用（validate 时拒绝不支持的组合）
    mcp_tools: bool                            # 是否支持 --mcp-config（mcp 配置落地后启用该校验）
    streaming_events: bool                     # 是否产出结构化流事件
    structured_output: Literal["native", "prompt_injection", "none"]
    interrupt: bool                            # 是否支持中途打断
    checkpoint_resume: bool                    # 是否支持 session resume
    usage_tracking: bool                       # 是否产出 token/cost
    concurrent_safe: bool                      # 是否可并行 spawn（foreach/parallel 需要）
```

**关键价值**：让 `orca validate` 静态拒绝 backend 不支持的组合，在 spawn 前就 fail loud（Conductor 的 `get_capabilities` 不实例化就能查）。校验规则见 §4.9（**仅基于 AgentNode 真实字段**：executor / output_schema / foreach body）。

### 4.5 builtin profile 示例

```python
# orca/profiles/builtin/claude.py
PROFILE = CliProfile(
    name="claude",
    capabilities=ProviderCapabilities(
        mcp_tools=True, streaming_events=True, structured_output="native",
        interrupt=True, checkpoint_resume=True, usage_tracking=True, concurrent_safe=True,
    ),
    cli_path_env="ORCA_CLAUDE_CLI",
    default_cli_path="claude",
    flags=("-p", "--output-format", "stream-json", "--include-partial-messages",
           "--verbose", "--permission-mode", "auto", "--bare"),
    prompt_channel="stdin",
    mcp_flag_template="--mcp-config {path}",
    env_overlay_prefixes=("ANTHROPIC_", "CLAUDE_"),
    stream_format="json",
    translator=claude_translator,           # 从 AgentHarness 迁移
    result_extractor=claude_result_extractor,
    prompt_paradigm="minimal",
)
```

### 4.6 命令替换的三种摩擦层级

| 场景 | 操作 | 改代码？ |
|---|---|---|
| **二进制替换**（claude → claude-ds-flash，flags 相同）| `ORCA_CLAUDE_CLI=claude-ds-flash orca run ...` | ❌ 零改动 |
| **新 CLI，claude 兼容 flags**（ccr code）| 丢 `./.orca/profiles/ccr.py` 导出 `PROFILE` | ❌ 零改动（自动发现）|
| **新 CLI，不同流格式**（codex/opencode）| 丢 profile + 注册 translator | 仅 translator 代码 |

### 4.7 注册表（builtin + project 覆盖）

```python
# orca/profiles/registry.py
def load_builtin_profiles() -> None:
    """扫描 orca/profiles/builtin/*.py，导入 PROFILE，register。"""
def load_project_profiles(cwd: Path) -> None:
    """扫描 <cwd>/.orca/profiles/*.py，覆盖 builtin。HARNESS_DISABLE_PROJECT_PROFILES=1 可禁用。"""
def get_profile(name: str) -> CliProfile:
    """查注册表。不存在 → ValueError（含 disable 原因，fail loud）。"""
def register(profile: CliProfile) -> None: ...
def disable_profile(name: str, reason: str) -> None: ...
def available_profiles() -> list[str]: ...
```

**优雅降级**：profile 文件语法错/缺 PROFILE → `disable_profile(name, reason)`，`get_profile` 抛清晰错误而非静默丢。

### 4.8 与 schema/compile/exec 的衔接（已修正）

| 层 | 状态 | 衔接 |
|---|---|---|
| **schema**（已实现）| ✅ phase 1 | `AgentNode.executor: str = "claude"` —— 只是名字选择器 |
| **profiles**（本阶段）| ✅ phase 3 | `get_profile` + `validate_workflow_profiles` 就位 |
| **compile**（本阶段补）| ✅ phase 3 | validator 新增 `_check_profiles`：调 `validate_workflow_profiles`，issue 汇入 `ValidationResult`（§4.9）|
| **exec**（phase 4）| ⏳ phase 4 | `make_executor(node) → get_profile(node.executor) → SubprocessExecutor(profile=...)`。exec 永远不硬编码 binary/flag，只读 profile |

> **修正**：旧版称 compile 校验「phase 2 已做」——失实。phase 2 compile 无 profiles 引用（grep 证实）。capability 校验是 **phase 3 交付项**（profiles 已建，顺手接进 compile）。

### 4.9 profiles/validate.py —— capability 静态校验（被 compile 调用）

**职责**：纯静态校验 workflow 中每个 agent node 的 `executor` profile 存在 + capabilities 与 node 配置兼容。不 spawn、不实例化 backend。**只依赖 `orca.schema` + `orca.profiles.registry`，不依赖 compile**（compile 单向调它）。

```python
# orca/profiles/validate.py
from typing import Literal
from dataclasses import dataclass

@dataclass(frozen=True)
class ProfileIssue:
    node: str
    severity: Literal["error", "warning"]
    message: str

def validate_workflow_profiles(wf: Workflow) -> list[ProfileIssue]:
    """对每个 agent / foreach-body agent node：
       ① get_profile(executor) 存在；② capabilities 与 node 配置交叉校验。
       返回 issue 列表（compile 汇总进 ValidationResult）。"""
```

**校验规则**（**仅基于 AgentNode 真实字段**；mcp_servers 当前不在 schema，mcp_tools 检查待 mcp 配置落地后启用）：

| # | 条件 | 严重度 | 说明 |
|---|---|---|---|
| 1 | `get_profile(node.executor)` 抛 ValueError | error | 未知 executor（含被 disable 的，附 disable 原因）|
| 2 | `node.output_schema is not None` 且 `cap.structured_output == "none"` | error | backend 不支持结构化输出，却声明了 schema |
| 3 | foreach `body` 是 AgentNode 且其 executor `cap.concurrent_safe == False` | error | foreach 并发执行 body，backend 不可并行 spawn |
| 4 | `cap.streaming_events == False` | warning | 该 backend 不产出结构化流事件（前端 live 观测降级）|

**compile 集成**（`orca/compile/validator.py`）：

```python
def _check_profiles(wf: Workflow, result: ValidationResult) -> None:
    """⑨ capability 校验：profiles/validate 产出 issue → 汇入 result。"""
    from orca.profiles import validate_workflow_profiles   # 单向依赖 compile → profiles
    for issue in validate_workflow_profiles(wf):
        msg = f"node '{issue.node}': {issue.message}"
        if issue.severity == "error":
            result.add_error(msg)
        else:
            result.add_warning(msg)
```

`validate_workflow` 在现有 8 项后追加 `_check_profiles(wf, result)`（第 ⑨ 项），仍走 `raise_if_errors` 统一裁决（聚合 fail loud，一次报全）。

**依赖方向核对**：`compile → profiles → schema`，无环；profiles/validate 不 import compile。

---

## 5. 不做的事（明确边界）

- ❌ **不做 streaming text 的逐 token 拼接 reducer**——phase 4 的 translator 负责。phase 3 只定义 Event 契约 + Tape/Bus/replay 基础设施。
- ❌ **不做 WebSocket / HTTP 服务**——那是 phase 7（iface/web）。phase 3 只提供 in-process 的 EventBus + 订阅者 Queue。
- ❌ **不做 `--bg` / attach / 终端映射**——未来扩展，记录在 REFERENCES.md。
- ❌ **不做 snapshot 优化**——第一期全量 replay。snapshot/session 分区留到性能成问题（§3.5 文档化目标）。
- ❌ **不做 events/ 的 sidecar 投影写盘**——反模式②。charts/outline 都是读时投影。
- ❌ **不生成 run_id**——Tape 仅接收 run_id 参数；生成归 phase 5（§3.5）。
- ❌ **translator 用 dummy 占位**（真 translator phase 4 迁移）；dummy 须类型匹配 `Event`（含 session_id 字段）。

---

## 6. 验收标准

### 6.0 验收总则（5 条铁律，违反即返工）

1. **唯一真相源**：任一事件只写一处（Tape）；无并行内存 list、无 sidecar、无投影写盘。判据：grep 代码无第二份事件存储。
2. **幂等性**：reducer 对同一事件应用 N 次 = 1 次（streaming text 用 `text@seq`，不拼接）。判据：有测试覆盖「应用两次 = 一次」。
3. **一条读路径**：streaming = replay = 同一个 `apply_event`。判据：无第二份 live/replay 分支代码。
4. **fail loud**：未知 executor / 不兼容 capability / tape 残行截断 / 损坏 profile → 全部显式报错或 warning，不静默吞。
5. **依赖单向**：`events→schema`、`profiles→schema`、`compile→profiles`、`exec→profiles`；无反向、无环。判据：import 关系图无环。

### 6.1 events/ 结构
- [ ] `orca/events/` 下 tape.py / bus.py / replay.py + __init__.py
- [ ] `from orca.events import EventBus, Tape, replay_state, Subscription` 能 import

### 6.2 Tape 验收
- [ ] `Tape(path).append(Event(...))` 写一行 JSON + flush
- [ ] seq 单调递增（写时分配，调用方不传 seq）
- [ ] **seq 序 == 文件行序**（并发 append 下 Lock 覆盖「seq 分配 + write + flush」整体）
- [ ] `tape.replay()` 逐行读回，与写入顺序一致
- [ ] 末尾残行（崩溃场景）被容忍，不抛异常
- [ ] **resume 处理残行**：崩溃截断的半行被截断（不接坏行），追加从最后有效行继续；截断记 warning
- [ ] resume：同 run_id 重开 = 追加模式，从 last_seq+1 继续
- [ ] bytes/Path 类型经 `_json_safe` 正确序列化

### 6.3 EventBus 验收
- [ ] `bus.emit("node_started", {...})` 构造 Event + 写 Tape + 通知订阅者
- [ ] **session_id 透传**：`emit(..., session_id=s)` 写入事件顶层 session_id
- [ ] 异步：慢订阅者（sleep 的）不阻塞 emit
- [ ] per-consumer cursor：订阅者 A 落后时，订阅者 B 不受影响
- [ ] 队列满：丢最老事件 + warning，不抛异常

### 6.4 replay 验收（反幂等的核心检验）
- [ ] `replay_state(tape)` 产出正确 RunState
- [ ] **幂等性**：reducer 对同一事件应用两次 = 一次（streaming text 用 `text@seq` 不拼接）
- [ ] 一条读路径：live 和 replay 走同一个 apply_event
- [ ] session_id 透传：replay 保留事件 session_id；同 node 不同 session 的事件可区分（retry 场景）

### 6.5 profiles/ 结构
- [ ] `orca/profiles/` 下 base.py / capabilities.py / registry.py / validate.py / builtin/{claude,ccr}.py + __init__.py
- [ ] `get_profile("claude")` / `get_profile("ccr")` 返回 CliProfile
- [ ] `get_profile("nonexistent")` 抛 ValueError（fail loud）
- [ ] `ORCA_CLAUDE_CLI=claude-ds-flash` 时 `profile.resolve_cli_path()` 返回 `claude-ds-flash`

### 6.6 profiles/ 校验（registry + capabilities + validate）
- [ ] ProviderCapabilities frozen + extra="forbid"（构造后不可变、未知字段拒绝）
- [ ] load_builtin_profiles 自动发现 builtin/*.py
- [ ] project 覆盖：`./.orca/profiles/<name>.py` 覆盖 builtin
- [ ] 损坏 profile 文件 → disable_profile + get_profile 抛清晰错误
- [ ] **`validate_workflow_profiles`** 四条规则：未知 executor(error) / output_schema+none(error) / foreach+非并发(error) / 无流事件(warning)

### 6.7 compile 集成验收
- [ ] `validate_workflow` 追加 `_check_profiles`（第 ⑨ 项），issue 正确汇入 ValidationResult
- [ ] capability error 阻止 workflow（随 ConfigurationError 抛出）；warning 不阻止
- [ ] 与 phase 2 的 8 项校验**共存不回归**（聚合一次报全）

### 6.8 端到端
- [ ] 写脚本：emit 10 个事件 → tape 有 10 行 → replay 重建正确 state
- [ ] emit 100 个事件到慢订阅者 → 不阻塞 emitter；订阅者最终收到全部（或丢老事件 + warning）
- [ ] session_id 透传：同 node emit 多个 session_id 的流式事件 → tape 保留 → replay 能按 session_id 分组
- [ ] `validate_workflow` 对带 `executor: ccr` + `output_schema` 的 workflow 报 error（capability 闭环）

### 6.9 测试
- [ ] `tests/events/test_tape.py`：append/replay/seq 单调/seq==行序/resume 残行截断/json_safe/残行容忍
- [ ] `tests/events/test_bus.py`：emit/订阅/异步不阻塞/per-cursor/队列满/session_id 透传
- [ ] `tests/events/test_replay.py`：reducer 幂等性（核心）+ 各 EventType 分支 + **同 node 多 session_id 分组**
- [ ] `tests/profiles/test_registry.py`：builtin/project 覆盖/disable/env 覆盖
- [ ] `tests/profiles/test_capabilities.py`：frozen/extra-forbid/字段约束
- [ ] `tests/profiles/test_validate.py`：四条规则各覆盖（unknown executor / output_schema+none / foreach+非并发 / 无流事件 warning）
- [ ] `tests/compile/test_validate_profiles.py`：compile `_check_profiles` 集成 + 与 phase 2 不回归
- [ ] 全部通过（含 phase 1+2 的不回归）

---

## 7. 实现要点

1. **seq 由 Tape 分配**（写时），不在 schema 的 Event 里由调用方填。emit 接收 `(type, data, node, session_id?)`，内部构造 Event。
2. **Tape.append 在 asyncio.Lock 内完成「seq 分配 + write + flush」整体**（保证 seq 序 == 文件行序）；resume open 先截断末尾残行再追加（截断记 warning）。
3. **每事件 write + flush**（不 fsync，靠 OS buffer；崩溃最多丢最后一行）。
4. **reducer 幂等是硬约束**：streaming text 存成 `{seq: text}` 或 `text@seq`，绝不字符串拼接。测试必须覆盖"应用两次 = 一次"。
5. **profiles 的 translator/result_extractor 是 callable**，第一期从 AgentHarness 迁移 claude_translator（但 phase 3 只放占位/dummy，真 translator 在 phase 4 落地）。dummy 须类型匹配含 session_id 的 Event。
6. **profiles/ 不依赖 exec/run/compile**（依赖方向：compile→profiles、exec→profiles，不反向）。profiles/validate 只依赖 schema + registry。
7. **zero logic in schema**（phase 1 已定）：profiles/ 的 ProviderCapabilities 是新数据类型，放 profiles/ 不放 schema/。
8. **run_id 目录与命名**：`runs/<run_id>/events.jsonl`。run_id 用 composite 格式 `<workflow_slug>-<YYYYMMDDHHMMSS UTC>-<nanoid6>`（见 §3.5），**非 uuid4 hex**——run_id 本身即历史名，`ls runs/` 可读，无需 manifest。**phase 3 不生成 run_id**（Tape 接收参数）。
9. **compile `_check_profiles` 单向依赖 profiles**：`from orca.profiles import validate_workflow_profiles`，issue 汇入现有 ValidationResult，走 raise_if_errors 聚合裁决。

---

## 8. 迁移资产（从 AgentHarness）

| 资产 | 来源 | 处理 |
|---|---|---|
| `_cli_subprocess.py` 的 argv builder / stdin pump | AgentHarness | phase 4 迁移（不在本阶段）|
| `translator/stream_json.py` | AgentHarness | phase 4 迁移（本阶段 profiles 用 dummy translator 占位）|
| `cli_profile.py` + `cli_profiles/{claude,ccr}.py` | AgentHarness | **本阶段迁移**到 `orca/profiles/`，重构为 CliProfile + ProviderCapabilities |
| `_make_json_safe` | Conductor `event_log.py:43-55` | **本阶段实现**到 tape.py |

---

## 9. 与前序阶段的衔接

| 前序 | 衔接点 |
|---|---|
| phase 1 schema | Event/EventType/RunState 已定义（Event 含 session_id）；reducer 用它们 |
| phase 2 compile | compile validator 追加 `_check_profiles`（⑨）；其余 8 项不变 |

## 10. 给后续阶段的接口契约

| 后续 | phase 3 提供 |
|---|---|
| phase 4 exec | translator 产出 Event → `bus.emit(..., session_id=...)`；profile 给 executor 用 |
| phase 5 run | Orchestrator 生成 run_id（composite）+ emit workflow/node/session 事件；用 `replay_state` 重建 |
| phase 7 web | 订阅 Subscription.queue → WS 推；GET /api/state 读 tape（不另存内存 list）；按 session_id 懒加载 session 细节 |

---

## 11. 关键决策备忘（写进 SPEC 防 drift）

1. **Tape 是唯一真相源**，无并行内存 list（反 Conductor `_event_history`）
2. **streaming = replay = read tape**（一条读路径）
3. **seq 写时分配**（Tape.append 内部，调用方不管）；Lock 覆盖 seq+write+flush 整体
4. **reducer 幂等**（streaming text 用 `text@seq`，不拼接）—— 这是消灭 dedup 层的根本
5. **profiles/ 放 phase 3**（config-shape，给 compile 静态校验 + phase 4 消费）
6. **命令替换三摩擦层**：env 覆盖 / project profile 文件 / + translator
7. **第一期全量 replay**，snapshot/session 分区留未来（recoverable）
8. **不写 sidecar**（charts/outline 是读时投影）
9. **session_id 是 agent 调用身份**（顶层字段，与 node 平级）：retry/for_each/parallel 每次调用一个 session_id；attempt 派生不入库。取代原 `iteration: int`——count 不适配 for_each/parallel，且独立 context 要的是 identity 不是序号
10. **run_id 用 composite**（`<slug>-<ts>-<nanoid6>`），即历史名，单 tape 无 manifest；phase 3 不生成（归 phase 5）
11. **session 分区是文档化目标**（recoverable，非 phase 3）；与反模式②（投影分裂）本质不同——判据：事件不重复写
12. **resume 先清残行**（截断末尾不完整行 + warning，不接坏行）
13. **capability 校验是 phase 3 交付**（profiles/validate.py + compile `_check_profiles`），非 phase 2；规则仅基于 AgentNode 真实字段

---

## 12. 开发计划（与 `docs/plans/2026-06-30-phase3-events-profiles.md` 对齐）

phase 3 拆为 **3 个可独立验收的步骤**（依赖顺序：A → B → C，B 内部可并行于 A 的收尾）：

| 步骤 | 模块 | 关键产出 | 依赖 |
|---|---|---|---|
| **A** | events/ | Tape（含 resume 清残行 + Lock 整体）/ EventBus（含 session_id 透传）/ replay（幂等） | schema（已就位）|
| **B** | profiles/（registry + capabilities + builtin） | CliProfile / ProviderCapabilities / get_profile / builtin profiles | schema |
| **C** | profiles/validate.py + compile `_check_profiles` | capability 校验闭环 | A 无关、依赖 B |

详见开发计划文档：[`docs/plans/2026-06-30-phase3-events-profiles.md`](../plans/2026-06-30-phase3-events-profiles.md)

---

## 13. TASK3 完整描述（可直接给新 session）

> 以下是给实现 session 的完整任务描述。复制它作为新 session 的第一条消息。

---

你是一名资深 Python 工程师，在 **Orca** 项目里实现【阶段 3：events/ 事件层 + profiles/ 命令替换层 + capability 校验闭环】。

## 必读文档（按顺序读，不要跳过）
1. `CLAUDE.md` —— 协作规则（"代码质量底线""自我 review"）
2. `docs/PLAN.md` —— 整体开发计划
3. `docs/TASK.md` —— 全局架构决策（§3 事件层契约、§6 Session 身份）
4. `docs/REFERENCES.md` —— 参考项目 + 设计规则
5. `docs/specs/phase-1-schema.md` —— Event（含 session_id）/EventType/RunState 定义（你依赖）
6. `docs/specs/phase-2-compile.md` —— compile 层（你要追加 `_check_profiles`）
7. **`docs/specs/phase-3-events.md`** —— **你的任务 SPEC**（逐字实现，契约不是建议）
8. **`docs/plans/2026-06-30-phase3-events-profiles.md`** —— **开发计划**（步骤 A/B/C + 验收）
9. `orca/schema/` + `orca/compile/` —— 已实现的前序阶段

## 你的任务（三个步骤，A→B→C）

### 步骤 A：events/（唯一真相源）
- `orca/events/tape.py` —— Tape：append-only JSONL；seq 写时分配；**Lock 覆盖 seq+write+flush 整体**；每事件 write+flush；`_json_safe`；**resume 先截断末尾残行（+warning）再追加**
- `orca/events/bus.py` —— EventBus：持有 Tape；emit 强制写 + **透传 session_id** + 异步 fan-out（asyncio.Queue，per-consumer cursor，队列满丢老+warning）
- `orca/events/replay.py` —— replay_state：纯 reducer fold；**幂等是硬约束**（streaming text 用 text@seq 不拼接）
- `orca/events/__init__.py` —— 导出

### 步骤 B：profiles/（命令替换层）
- `orca/profiles/base.py` —— CliProfile dataclass（frozen）
- `orca/profiles/capabilities.py` —— ProviderCapabilities（frozen pydantic，extra="forbid"）
- `orca/profiles/registry.py` —— 注册表 + load_builtin + load_project + disable-on-failure
- `orca/profiles/builtin/claude.py` —— claude profile（flags `-p --output-format stream-json --include-partial-messages --verbose --permission-mode auto --bare`）
- `orca/profiles/builtin/ccr.py` —— ccr profile（`default_cli_path="ccr code"`）
- `orca/profiles/__init__.py` —— 导出

### 步骤 C：profiles/validate.py + compile 集成（capability 校验闭环）
- `orca/profiles/validate.py` —— `validate_workflow_profiles(wf) -> list[ProfileIssue]`，四条规则（§4.9）：未知 executor(error) / output_schema+structured_output=none(error) / foreach body + 非 concurrent_safe(error) / streaming_events=False(warning)。**仅基于 AgentNode 真实字段**
- `orca/compile/validator.py` —— 追加 `_check_profiles`（第 ⑨ 项），调 `validate_workflow_profiles`，issue 汇入 ValidationResult，走 raise_if_errors

## 强制约束（违反即返工，详见 SPEC §6.0 §1 §11）

### 验收总则（5 条铁律）
1. **唯一真相源**：事件只写 Tape 一处，无第二份存储
2. **幂等性**：reducer 应用同一事件 N 次 = 1 次（有测试）
3. **一条读路径**：streaming = replay = 同一 apply_event
4. **fail loud**：未知 executor / 不兼容 capability / 残行截断 / 损坏 profile 全显式报错
5. **依赖单向无环**：events→schema、profiles→schema、compile→profiles、exec→profiles

### events/ 铁律
6. **seq 由 Tape.append 写时分配**（emit 不传 seq）；Lock 覆盖 seq+write+flush 整体
7. **Tape append-only 永不驱逐**；有界内存在订阅者 Queue
8. **异步 fan-out + session_id 透传**：慢订阅者不阻塞 emit（测试覆盖）
9. **resume 先清残行**（截断 + warning），不重建/不 synthesize
10. **分歧 = 错误**：replay 与 live 不一致 raise

### profiles/ 铁律
11. CliProfile frozen dataclass；ProviderCapabilities frozen pydantic extra="forbid"
12. `resolve_cli_path()` 运行时读 env > default
13. project profile 覆盖 builtin；损坏文件 disable + fail loud
14. profiles/ 不依赖 exec/run/compile（compile→profiles 单向）

### validate 铁律
15. 规则**仅基于 AgentNode 真实字段**（executor/output_schema/foreach body）；不自创字段
16. compile `_check_profiles` 与 phase 2 的 8 项共存，聚合一次报全

## 不做的事（明确边界）
- ❌ WebSocket/HTTP（phase 7）
- ❌ `--bg`/attach/终端映射（未来）
- ❌ snapshot/session 分区优化（文档化目标，非本阶段）
- ❌ 写 sidecar（charts/outline 是读时投影）
- ❌ translator 用 dummy 占位（真 translator phase 4）
- ❌ 生成 run_id（归 phase 5，Tape 接收参数）

## 产出要求
1. events/ 4 文件 + profiles/ 7 文件（含 validate.py）
2. `tests/events/test_tape.py` —— append/replay/seq 单调/seq==行序/resume 残行截断/json_safe/残行容忍
3. `tests/events/test_bus.py` —— emit/异步不阻塞/per-cursor/队列满/session_id 透传
4. `tests/events/test_replay.py` —— **reducer 幂等性（核心）** + 各 EventType 分支 + 同 node 多 session_id 分组
5. `tests/profiles/test_registry.py` —— builtin/project 覆盖/disable/env 覆盖
6. `tests/profiles/test_capabilities.py` —— frozen/extra-forbid/字段约束
7. `tests/profiles/test_validate.py` —— 四条规则各覆盖
8. `tests/compile/test_validate_profiles.py` —— compile `_check_profiles` 集成 + phase 2 不回归
9. `pyproject.toml` 更新依赖（保持最小）

## 验收标准（SPEC §6，全部必须通过）
- [ ] events/ 结构 + import
- [ ] Tape：append/replay/seq 单调/seq==行序/resume 残行截断/json_safe/残行容忍
- [ ] EventBus：emit/异步不阻塞/per-cursor/队列满丢老+warning/session_id 透传
- [ ] replay：reducer 幂等（**应用两次=一次**）+ 各 EventType 分支 + 多 session_id 分组
- [ ] profiles/ 结构 + get_profile/resolve_cli_path/env 覆盖/project 覆盖/disable
- [ ] validate：四条规则
- [ ] compile `_check_profiles` 集成 + phase 2 不回归
- [ ] 端到端：emit 10 事件 → tape 10 行 → replay 重建正确 state；ccr+output_schema 被 validate 拒绝
- [ ] 全部测试通过（含 phase 1+2 不回归）

## 工作流程（SDD）
1. 先读 9 份必读文档
2. 按 `docs/plans/2026-06-30-phase3-events-profiles.md` 执行步骤 A→B→C，每步实现后跑对应测试 + 自检 §6.0 五条铁律
3. 实现完成后，**自我 review**（分发 review agent 检查：reducer 幂等性、Tape 唯一真相源 + resume 清残行、Lock 覆盖 seq+write、异步不阻塞、session_id 透传、profiles 依赖方向、validate 规则只基于真实字段）
4. 在 `docs/releases/2026-06-30-phase3-events-profiles.md` 写 release note
5. 更新 `docs/status/CURRENT.md` + CHANGELOG

**不要**：实现 exec/run/web —— 后续阶段。
**不要**：自作主张加 SPEC 没有的字段或机制。SPEC §1 列了 5 个反模式，你的设计如果需要 dedup set / watermark / sidecar / 双路径中的任何一个，设计就是错的。
