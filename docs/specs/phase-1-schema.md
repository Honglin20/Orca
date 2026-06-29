# 阶段 1 SPEC —— schema/ 数据层

> **状态**：最终版（融合 AgentHarness 极简哲学 + Conductor 路由，待分发实现）
> **依据**：[TASK.md](../TASK.md) §1 §6
> **范围**：只定义纯数据结构，零执行逻辑。这是整个架构的地基。
> **执行任务**：见文末 §10「TASK1 完整描述」

---

## 0. 设计目标

schema/ 是 Orca 的**纯数据结构定义层**。它只回答三个第一性问题：

| 问题 | 文件 | 装什么 |
|---|---|---|
| 跑什么？（静态结构）| `workflow.py` | Workflow / Node / Route / 各 kind |
| 产出了什么？（运行时产出）| `event.py` | Event / EventType |
| 现在到哪了？（运行时状态）| `state.py` | RunState / Status |

**铁律**：
- 只有 pydantic 模型，**零逻辑**（无解析、无校验、无持久化）
- 零依赖（除 pydantic），其他所有模块依赖 schema，schema 不依赖任何人
- HumanGate **不在本层**——它属于 `gates/` extension。schema 只在 `event.py` 声明 `"human_decision_requested"` 事件类型（关注点分离）

---

## 1. 设计哲学（融合 AgentHarness + Conductor）

### 三条核心原则

1. **入口/依赖/prompt 用 AgentHarness 的极简哲学**
   - `entry` 显式声明入口 + `after` 表达依赖
   - prompt 按约定从 `agents/<name>.md` 加载（不写则用约定），可选内联
   - 不嵌套 workflow 块，顶层直接 name/entry/nodes
2. **路由用 Conductor 的 routes + Jinja2**
   - 多路 + 表达式（比 on_pass/on_fail 二元强）
   - first-match-wins，无 `when` = 兜底
3. **节点类型只保留必要的 4 种 kind**：`agent` / `script` / `set` / `foreach`

### 融合对照表

| 特性 | AgentHarness | Conductor | Orca 决策 | 理由 |
|---|---|---|---|---|
| 入口声明 | `after=[]` 约定 | `entry:` 显式 | **`entry:` 显式** | 显式优于隐式 |
| 依赖关系 | `after` | `depends_on` | **`after`** | AgentHarness 原名，简洁 |
| 条件路由 | `on_pass`/`on_fail` 二元 | `routes:` + Jinja2 | **`routes:` + Jinja2** | 多路 + 表达式 |
| 并行 | `after` 多源汇聚 | `parallel:` 块 | **`after` 多源汇聚** | DAG 自然，无需新概念 |
| 动态并行 | 无 | `for_each:` 块 | **`kind: foreach`** | NAS 等真需要 |
| prompt | 约定 `<name>.md` | 内联 | **约定优先 + 可选内联** | 长短都方便 |
| 工具 | `None` 全开 | `tools: []` | **默认全开，可选白名单** | headless 管控价值低 |
| 输出结构 | `result_type` | `output:` schema | **可选 `output_schema`** | None=自由文本/有=结构化 |
| script | 无 | `type: script` | **保留** | 确定性命令不烧 token |
| set | 无（用 state）| `type: set` | **保留**（用户确认）| 纯计算存值 |
| workflow 嵌套 | 无 | 有 | **不嵌套** | 顶层直接 |

---

## 2. workflow.py —— 工作流定义

### 2.1 顶层结构

```python
class Workflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    entry: str                          # 起始 node 名（显式，唯一入口）
    inputs: dict[str, InputDef] = {}    # 工作流输入声明（可选）
    nodes: list[AnnotatedNode]          # 所有节点（discriminated union）
    outputs: dict[str, str] = {}        # 最终输出映射 {key: "{{ node.output.field }}"}
```

```python
class InputDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str                           # "string" / "int" / "boolean" / "list" / ...
    required: bool = True
    default: Any = None
    description: str = ""
```

### 2.2 Node：基类 + 4 个 kind（discriminated union）

```python
class Node(BaseModel):
    """所有 node 共有字段。"""
    model_config = ConfigDict(extra="forbid")
    name: str                           # 唯一标识
    after: list[str] = []               # 静态依赖（默认空=入口候选）
    routes: list[Route] = []            # 条件路由（first-match-wins）

class Route(BaseModel):
    model_config = ConfigDict(extra="forbid")
    when: str | None = None             # Jinja2 表达式；None = 兜底（catch-all）
    to: str                             # 目标 node 名 / "$end"
```

### 2.3 四个 kind

#### agent（LLM agent —— 核心）

```python
class AgentNode(Node):
    kind: Literal["agent"] = "agent"
    prompt: str | None = None           # 内联短 prompt；None=从 agents/<name>.md 加载（约定）
    tools: list[str] | None = None      # None=全开（默认）；[]; [...]=白名单
    executor: str = "claude"            # "claude" / "ccr" / "codex"（未来）
    model: str | None = None            # 模型覆盖
    output_schema: dict | None = None   # None=自由文本（取整段 result）；
                                        # {...}=结构化（用 claude --output-format json_schema）
```

**prompt 约定加载规则**（实现时由 compile/ 做，本阶段只定义字段语义）：
- `prompt` 省略或 None → 从 `agents/<name>.md` 加载（相对 workflow YAML 文件目录）
- `prompt` 非空 → 用内联值（短 prompt 场景）
- 两者互斥：要么内联要么约定，编译时校验文件存在

**输出摘取规则**（实现时由 exec/ 做）：
- `output_schema = None` → output = claude 的整段 result 文本（自由模式）
- `output_schema = {...}` → 用 claude `--output-format json_schema` 逼出 JSON，再校验（结构化模式）
- 下游引用：自由模式 `{{ agent_name.output }}`（整段文本）；结构化模式 `{{ agent_name.output.field }}`

#### script（确定性 shell 命令，不烧 token）

```python
class ScriptNode(Node):
    kind: Literal["script"] = "script"
    command: str                        # shell 命令（支持 Jinja2 渲染）
    parse_json: bool = False            # True=解析 stdout 为 JSON 存入 output
    timeout: float | None = None        # 超时秒
```

**输出**：`{stdout, stderr, exit_code}`。`parse_json=True` 时额外 `output.json = <解析结果>`。

#### set（纯计算存值，不烧 token 不跑命令）

```python
class SetNode(Node):
    kind: Literal["set"] = "set"
    values: dict[str, str]              # {key: Jinja2 表达式}，编译时求值存入 output
```

**用途**：累积状态、算中间变量、存"当前最佳"。输出 = `{key: 求值结果}`，下游 `{{ set_node.output.key }}`。

#### foreach（动态并行：运行时才知道几个分支）

```python
class ForeachNode(Node):
    kind: Literal["foreach"] = "foreach"
    source: str                         # 上游数组字段路径（Jinja2，如 "finder.output.candidates"）
    item_var: str = "item"              # 循环变量名（注入 body 的 prompt 上下文）
    index_var: str = "_index"           # 索引变量名
    body: AgentNode | ScriptNode        # 每个元素跑什么（嵌套 node，不含 set/foreach）
    max_concurrent: int = 10            # 分批大小
    failure_mode: Literal["fail_fast", "continue_on_error", "all_or_nothing"] = "fail_fast"
```

**输出**：`{"outputs": [...], "errors": {...}, "count": N}`（数组列表）。下游 `{{ foreach_node.output.outputs }}`。

### 2.4 Discriminated union 注册

```python
AnnotatedNode = Annotated[
    Union[AgentNode, ScriptNode, SetNode, ForeachNode],
    Field(discriminator="kind"),
]
```

**关键约束**（schema 层定义，compile/ 层校验）：
- 所有 node 的 `name` 全局唯一
- `entry` 必须是某个 node 的 name
- `after` 引用的必须是已定义的 node
- `routes[].to` 引用的必须是已定义的 node 或 `"$end"`
- DAG 无环（routes 回指是允许的，因为是条件边；after 静态边必须无环）

---

## 3. event.py —— 事件契约（唯一真相源的元素）

### 3.1 Event 结构

```python
class Event(BaseModel):
    seq: int                            # 单调递增序号（不变量：全局唯一递增）
    type: EventType                     # 事件类型
    timestamp: float                    # epoch 秒
    node: str | None = None             # 哪个 node 产出；workflow 级为 None
    data: dict = {}                     # 各 type 特定 payload
```

### 3.2 EventType（Literal 联合体）

```python
EventType = Literal[
    # ── workflow 生命周期 ──
    "workflow_started",                 # data: {inputs, node_count, entry}
    "workflow_completed",               # data: {elapsed, outputs}
    "workflow_failed",                  # data: {error_type, message, node}

    # ── node 生命周期 ──
    "node_started",                     # data: {node, iteration?}
    "node_completed",                   # data: {node, elapsed, output}
    "node_failed",                      # data: {node, error_type, message}
    "node_skipped",                     # data: {node, reason}

    # ── agent 流式（claude stream-json 翻译产出）──
    "agent_message",                    # data: {text}
    "agent_thinking",                   # data: {text}
    "agent_tool_call",                  # data: {tool, args, tool_call_id}
    "agent_tool_result",                # data: {tool_call_id, result}
    "agent_usage",                      # data: {input_tokens, output_tokens, cache_tokens, cost_usd}

    # ── 路由 ──
    "route_taken",                      # data: {from, to}

    # ── 并发 ──
    "foreach_started",                  # data: {item_count, max_concurrent}
    "foreach_item_started",             # data: {index, item_key}
    "foreach_item_completed",           # data: {index, output}
    "foreach_completed",                # data: {count, succeeded}

    # ── HMIL（gates extension 产出；核心只认这个事件，不认 gate 实体）──
    "human_decision_requested",         # data: {gate_id, prompt, options?, source, context}
    "human_decision_resolved",          # data: {gate_id, answer}

    # ── 自定义（MCP 工具产出，前端按 data.kind 分发渲染）──
    "custom",                           # data: {kind: "chart"|"table"|"image"|..., ...}

    # ── 错误 ──
    "error",                            # data: {error_type, message, phase?}
]
```

### 3.3 custom 事件渲染约定

`custom` 事件让 MCP 工具（render_chart / render_table 等）产出任意结构化数据，前端按 `data.kind` 分发。**约定**：

| `data.kind` | 含义 | data 其他字段 |
|---|---|---|
| `chart` | 图表 | `spec`: recharts/echarts spec |
| `table` | 表格 | `columns`, `rows` |
| `image` | 图片 | `url` 或 `base64` |
| `markdown` | 富文本 | `content` |
| `json` | 任意 JSON（折叠显示）| `content` |

**扩展新渲染类型 = 前端加 renderer + MCP 工具产出对应 `data.kind`**，零核心改动（OCP）。

### 3.4 EventType 用 Literal 的权衡（有意为之的改进）

用 `Literal` 联合体而非裸字符串（Conductor 用裸字符串散落 38 处），是为了：
- ✅ 类型安全（IDE 提示 + typo 编译期捕获）
- ✅ payload 文档化（每个 type 旁注释 data 字段）
- ❌ 不严格符合 OCP（加新类型要改 Literal 定义）—— 可接受的小代价

---

## 4. state.py —— 运行时状态

### 4.1 定位（关键，必须写明）

**RunState 不是另一份真相**。它是编排器运行时的**内存状态**，是 event tape 的**派生物**——任何时刻都能从 tape replay 重建。这个区分是避免"两份状态不一致"（AgentHarness 的教训）的根本。

```
event tape（持久化）= 唯一真相源
RunState（内存）    = tape 的派生视图，运行时维护，replay 时重建
```

### 4.2 结构

```python
Status = Literal["pending", "running", "done", "failed", "skipped"]

class RunState(BaseModel):
    run_id: str
    workflow_name: str
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    current_node: str | None = None
    node_status: dict[str, Status] = {}     # 每个 node 的状态
    context: dict[str, Any] = {}            # 所有已完成 node 的输出（accumulate）
    usage: UsageSummary | None = None

class UsageSummary(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    cost_usd: float = 0.0
    node_breakdown: dict[str, "UsageSummary"] = {}  # 每 node 的 usage（递归）
```

---

## 5. 文件结构

```
orca/schema/
├── __init__.py        # 导出所有公共类型
├── workflow.py        # Workflow / Node / Route / InputDef / 4 个 kind 子类
├── event.py           # Event / EventType
└── state.py           # RunState / Status / UsageSummary
```

`__init__.py` 导出：
```python
__all__ = [
    "Workflow", "InputDef", "Node", "Route",
    "AgentNode", "ScriptNode", "SetNode", "ForeachNode", "AnnotatedNode",
    "Event", "EventType",
    "RunState", "Status", "UsageSummary",
]
```

---

## 6. 终态：完整 workflow YAML 示例

### 6.1 NAS（神经架构搜索，融合所有特性）

```yaml
# nas.yaml
name: nas
description: 迭代式神经结构搜索
entry: optimizer

inputs:
  iterations:
    type: int
    default: 3
    description: 优化迭代轮数

nodes:
  # ── agent：长 prompt 从约定文件 agents/optimizer.md 加载，结构化输出 ──
  - name: optimizer
    kind: agent
    output_schema:
      type: object
      properties:
        structure: {type: string}
        candidates: {type: array, items: {type: string}}
      required: [structure]
    routes:
      - when: "output.structure is defined"
        to: trainer
      - to: $end

  # ── agent：训练，短 prompt 内联，白名单工具 ──
  - name: trainer
    kind: agent
    after: [optimizer]
    prompt: |
      训练结构 {{ optimizer.output.structure }}，
      用 Bash 跑 train.py，读 metrics.json。
    tools: [Bash, Read]
    routes:
      - to: evaluator

  # ── script：评估（确定性，不烧 token）──
  - name: evaluator
    kind: script
    after: [trainer]
    command: "python eval.py --model {{ trainer.output.model_path }}"
    routes:
      - when: "output.exit_code == 0"
        to: reviewer
      - to: optimizer                 # 评估失败 → 回 optimizer 重设计（循环）

  # ── agent：评判，多路路由 ──
  - name: reviewer
    kind: agent
    after: [evaluator]
    prompt: "准确率 {{ evaluator.output.stdout }}，继续优化还是完成？"
    output_schema:
      type: object
      properties: {decision: {type: string, enum: [continue, done]}}
      required: [decision]
    routes:
      - when: "output.decision == 'continue'"
        to: optimizer                 # 回环循环
      - when: "output.decision == 'done'"
        to: $end

  # ── set：记录最佳（纯计算存值）──
  - name: record_best
    kind: set
    after: [reviewer]
    values:
      best_structure: "{{ optimizer.output.structure }}"
      best_accuracy: "{{ evaluator.output.stdout }}"
    routes:
      - to: $end

outputs:
  best_structure: "{{ record_best.output.best_structure }}"
  final_accuracy: "{{ record_best.output.best_accuracy }}"
```

### 6.2 静态并行（after 汇聚，无需新概念）

```yaml
name: parallel_research
entry: researcher_a

nodes:
  - name: researcher_a                 # 入口
    kind: agent                        # prompt 省略 → 从 agents/researcher_a.md 加载
  - name: researcher_b                 # 同层入口（a/b 自动并行）
    kind: agent                        # prompt 省略 → 从 agents/researcher_b.md 加载
  - name: synthesizer
    kind: agent
    after: [researcher_a, researcher_b]   # 汇聚点，等两者都完成
    prompt: |
      综合：{{ researcher_a.output }} / {{ researcher_b.output }}
```

### 6.3 foreach 动态并行（运行时数量）

```yaml
name: batch_assess
entry: finder

nodes:
  - name: finder
    kind: agent
    prompt: "找出候选结构，输出 JSON {candidates: [...]}"
    output_schema:
      type: object
      properties: {candidates: {type: array, items: {type: string}}}
      required: [candidates]

  - name: assessor
    kind: foreach
    after: [finder]
    source: finder.output.candidates   # 运行时取数组
    item_var: candidate
    max_concurrent: 3
    body:
      kind: agent
      prompt: "评估这个结构：{{ candidate }}"
    failure_mode: continue_on_error

  - name: picker
    kind: agent
    after: [assessor]
    prompt: "从评估结果选最佳：{{ assessor.output.outputs }}"
```

---

## 7. 验收标准

阶段 1 完成的硬性标志（全部必须通过）：

### 7.1 结构验收
- [ ] `orca/schema/` 下 3 个文件 + `__init__.py`，全部 pydantic 模型，零逻辑
- [ ] `from orca.schema import Workflow, AgentNode, ScriptNode, SetNode, ForeachNode, Event, RunState` 能 import
- [ ] 零依赖（除 pydantic v2）

### 7.2 discriminated union 验收
- [ ] `Workflow(nodes=[{"name":"a","kind":"agent"}])` 能从 dict 构造，自动分派到 AgentNode
- [ ] `Workflow(nodes=[{"name":"a","kind":"script","command":"ls"}])` 分派到 ScriptNode
- [ ] `kind="nonexistent"` 被拒（discriminator 校验）
- [ ] 每个 kind 子类 `extra="forbid"`：`AgentNode(name="a", prompt="x", wrong_field=1)` 报错

### 7.3 EventType 验收
- [ ] `Event(type="workflow_started", seq=1, timestamp=0)` 能构造
- [ ] `Event(type="nonexistent", seq=1, timestamp=0)` 被 Literal 拒绝
- [ ] 所有 21 个 event type 都能构造

### 7.4 端到端验收
- [ ] §6.1 的 nas.yaml 能被解析成 Workflow 对象（用 yaml.safe_load + Workflow(**data)，临时脚本验证即可，正式 parser 在 compile/ 阶段）
- [ ] §6.2 parallel_research.yaml 能解析
- [ ] §6.3 batch_assess.yaml 能解析

### 7.5 测试验收
- [ ] `tests/schema/test_workflow.py`：各 kind 构造、discriminator 分派、extra 报错、Route 校验
- [ ] `tests/schema/test_event.py`：Event 构造、EventType Literal 约束
- [ ] `tests/schema/test_state.py`：RunState/UsageSummary 构造
- [ ] 全部测试通过（pytest）

### 7.6 文件验收
- [ ] `examples/nas.yaml`（用 §6.1）
- [ ] `examples/parallel_research.yaml`（用 §6.2）
- [ ] `examples/batch_assess.yaml`（用 §6.3）
- [ ] `pyproject.toml`：uv + hatchling，依赖 `pydantic>=2.0`

---

## 8. 与 Conductor/AgentHarness 的最终对比（验证决策）

| 维度 | Conductor | AgentHarness | Orca（本 SPEC） |
|---|---|---|---|
| Node 模型 | 一个 AgentDef 塞所有字段 | Agent 类 + after | 基类 + kind 特化（discriminated union）|
| 路由 | routes + 双引擎 | on_pass/on_fail | routes + 单一 Jinja2 |
| 并行 | parallel: 块 | after 汇聚 | after 汇聚（无新概念）|
| foreach | for_each: 块 | 无（回指循环）| kind: foreach |
| script | type: script | 无 | kind: script |
| set | type: set | 无（用 state）| kind: set |
| 事件类型 | 裸字符串 38 处 | — | Literal 联合体 21 个 |
| extra="forbid" | 全用 | — | 全用 |
| prompt | 内联 | 约定 `<name>.md` | 约定优先 + 可选内联 |

---

## 9. 实现要点（给实现 session）

1. 用 **pydantic v2**（非 dataclass），因为要 discriminated union + `extra="forbid"`
2. `AnnotatedNode` 用 `Annotated[Union[...], Field(discriminator="kind")]`
3. 每个子类的 `kind` 字段用 `Literal["agent"]` 这种**字面量默认值**，既是 discriminator 又是默认值
4. `EventType` 用 `Literal[...]`，不用 Enum（Literal 更兼容 pydantic + IDE）
5. `UsageSummary.node_breakdown` 用 `dict[str, UsageSummary]`，pydantic 支持递归（用 `"UsageSummary"` 字符串前向引用）
6. **零逻辑**：不实现 parse（yaml→Workflow 在 compile/）、不实现 validate（在 compile/）、不实现 persist（在 events/）
7. 每个模型加 docstring 说明用途

---

## 10. TASK1 完整描述（可直接给新 session）

> 以下是给实现 session 的完整任务描述。复制它作为新 session 的第一条消息。

---

你是一名资深 Python 工程师，在一个名为 **Orca** 的项目里实现【阶段 1：schema/ 数据层】。

## 必读文档（按顺序读，不要跳过）
1. `CLAUDE.md` —— 协作规则（必读，尤其是"代码质量底线"和"自我 review"两节）
2. `docs/TASK.md` —— 全局架构决策（理解 Orca 是什么）
3. `docs/specs/phase-1-schema.md` —— **你的任务 SPEC**（逐字实现，这是契约不是建议）

## 你的任务
实现 `orca/schema/` 目录下的纯数据结构，**零执行逻辑**：
- `orca/schema/workflow.py` —— Workflow / Node（基类 + 4 个 kind 子类）/ Route / InputDef
- `orca/schema/event.py` —— Event / EventType
- `orca/schema/state.py` —— RunState / Status / UsageSummary
- `orca/schema/__init__.py` —— 导出所有公共类型

## 4 个 node kind（字段见 SPEC §2.3）
- `agent`（LLM agent，核心）
- `script`（确定性 shell 命令）
- `set`（纯计算存值）
- `foreach`（动态并行）

## 强制约束（违反即返工）
1. 用 **pydantic v2**（不是 dataclass）
2. 所有模型 `model_config = ConfigDict(extra="forbid")`
3. Node 用 `Annotated[Union[...], Field(discriminator="kind")]`（discriminated union）
4. 每个子类的 `kind` 字段用 `Literal["agent"]` 这种字面量默认值
5. `EventType` 用 `Literal[...]`，不用 Enum
6. **零依赖**（除 pydantic），**零逻辑**（无解析、无校验、无持久化）
7. HumanGate **不在本层**（它在 gates/ extension）。schema 只在 event.py 声明 `"human_decision_requested"` 事件类型
8. 不实现 compile/（解析器）、run/（编排）、exec/（执行）—— 那是后续阶段。本阶段只做数据结构

## 产出要求
1. 上述 4 个 schema 文件
2. `tests/schema/test_workflow.py` —— 各 kind 构造、discriminator 分派、extra 报错、Route 校验
3. `tests/schema/test_event.py` —— Event 构造、EventType Literal 约束（含 21 个 type 全覆盖）
4. `tests/schema/test_state.py` —— RunState/UsageSummary 构造（含递归 node_breakdown）
5. `examples/nas.yaml`（用 SPEC §6.1）
6. `examples/parallel_research.yaml`（用 SPEC §6.2）
7. `examples/batch_assess.yaml`（用 SPEC §6.3）
8. `pyproject.toml` —— uv + hatchling，依赖 `pydantic>=2.0`，加 pytest 依赖

## 验收标准（SPEC §7，全部必须通过）
- [ ] `from orca.schema import Workflow, AgentNode, ScriptNode, SetNode, ForeachNode, Event, RunState` 能 import
- [ ] discriminated union 分派正确（agent/script/set/foreach）
- [ ] `kind="nonexistent"` 被拒
- [ ] 每个 kind `extra="forbid"`：多余字段报错
- [ ] `Event(type="nonexistent")` 被 Literal 拒绝；21 个 type 全部能构造
- [ ] 3 个 examples/*.yaml 都能被解析成 Workflow 对象（临时脚本验证）
- [ ] 所有测试通过

## 工作流程（SDD）
1. 先读 3 份必读文档
2. 在 `docs/plans/2026-06-29-phase1-schema.md` 写简短实施计划（文件清单 + 每个文件的关键字段 + 测试清单），**不要写代码**
3. 等我确认计划后，再实现
4. 实现完成后，**自我 review**（CLAUDE.md 要求）：分发 review agent 检查依赖铁律/职责越界/DRY/fail loud/测试覆盖
5. 在 `docs/releases/2026-06-29-phase1-schema.md` 写 release note（实际做了什么、偏离 SPEC 处、验证结果、commit SHA）
6. 在 `docs/status/CURRENT.md` 更新状态；`docs/status/CHANGELOG.md` 加索引（1-2 句话 + commit）

**不要**：自作主张加 SPEC 里没有的字段或 node kind。SPEC 是契约，有疑问先问。
**不要**：实现 compile/run/exec —— 那是后续阶段。
