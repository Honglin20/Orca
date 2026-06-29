# 计划：阶段 1 schema/ 数据层

> SDD 事前计划。本计划只列「做什么 / 怎么做 / 怎么测」，不写实现代码。
> **冲突裁决**：SPEC §10 工作流第 3 步要求「等监工确认计划后再实现」，
> 但本次 `/goal` 指令明确要求「执行 TASK1，完整达到目标，通过所有验收标准」，
> 并指示「不要停下来问用户」。故按 goal 指令一气贯穿：写计划 → 实现 → 自检 → release note。

## 目标
实现 `orca/schema/` 纯数据结构层（3 文件 + `__init__`），**零执行逻辑、零依赖（除 pydantic v2）**。
为整个 Orca 架构打地基。

## 依据
- SPEC：[`docs/specs/phase-1-schema.md`](../specs/phase-1-schema.md)（逐字实现，契约不是建议）
- 架构铁律：[`docs/TASK.md`](../TASK.md) §1（`model` 最底层零依赖）

## 文件清单

### 1. `orca/schema/workflow.py`
- `InputDef`：type / required(=True) / default(=None) / description(="") —— `extra=forbid`
- `Route`：when(str|None=None) / to(str) —— `extra=forbid`
- `Node`（基类）：name / after(list[str]=[]) / routes(list[Route]=[]) —— `extra=forbid`
- `AgentNode(Node)`：kind=Literal["agent"]="agent" / prompt(str|None=None) / tools(list[str]|None=None) / executor(str="claude") / model(str|None=None) / output_schema(dict|None=None)
- `ScriptNode(Node)`：kind=Literal["script"] / command(str) / parse_json(bool=False) / timeout(float|None=None)
- `SetNode(Node)`：kind=Literal["set"] / values(dict[str,str])
- `ForeachNode(Node)`：kind=Literal["foreach"] / source(str) / item_var(str="item") / index_var(str="_index") / body(AgentNode|ScriptNode) / max_concurrent(int=10) / failure_mode(Literal[...]="fail_fast")
- `ForeachBody = Annotated[Union[AgentNode, ScriptNode], Field(discriminator="kind")]`（body 的判别联合，见「偏离」#3）
- `AnnotatedNode = Annotated[Union[AgentNode, ScriptNode, SetNode, ForeachNode], Field(discriminator="kind")]`
- `Workflow`：name(str) / description(str="") / entry(str) / inputs(dict[str,InputDef]={}) / nodes(list[AnnotatedNode]) / outputs(dict[str,str]={}) —— `extra=forbid`

### 2. `orca/schema/event.py`
- `EventType = Literal[...]` —— 逐字取 SPEC §3.2 代码块（实际 **21** 个值，见「偏离」#2）
- `Event`：seq(int) / type(EventType) / timestamp(float) / node(str|None=None) / data(dict={}) —— `extra=forbid`

### 3. `orca/schema/state.py`
- `Status = Literal["pending","running","done","failed","skipped"]`
- `UsageSummary`：input_tokens/output_tokens/cache_tokens(int=0) / cost_usd(float=0.0) / node_breakdown(dict[str,"UsageSummary"]={}, **递归 → model_rebuild()**) —— `extra=forbid`
- `RunState`：run_id(str) / workflow_name(str) / status(Literal["pending","running","completed","failed"]="pending") / current_node(str|None=None) / node_status(dict[str,Status]={}) / context(dict[str,Any]={}) / usage(UsageSummary|None=None) —— `extra=forbid`

### 4. `orca/schema/__init__.py`
- `__all__` 按 SPEC §5：Workflow / InputDef / Node / Route / AgentNode / ScriptNode / SetNode / ForeachNode / AnnotatedNode / Event / EventType / RunState / Status / UsageSummary

### 5. `orca/__init__.py`
- 顶层包 init（最小，声明 `__version__`）。SPEC §5 未画此文件，但 `import orca.schema` 需要它存在。

### 6. `pyproject.toml`
- build-system：hatchling
- project：name=orca, requires-python=">=3.10", dependencies=["pydantic>=2.0"]
- dependency-groups.dev：pytest, pyyaml（仅测试用，**不进核心依赖**，保 schema 零依赖铁律）
- `[tool.pytest.ini_options]` testpaths=["tests"]
- `[tool.hatch.build.targets.wheel] packages=["orca"]`

## 测试清单（`tests/schema/`，不写 `__init__.py`，靠已安装包 import）
- `test_workflow.py`：
  - 各 kind 直接构造（AgentNode/ScriptNode/SetNode/ForeachNode）
  - discriminator 分派：`Workflow(nodes=[{"name","kind":"agent"}])` → isinstance AgentNode；script/set/foreach 同理
  - `kind="nonexistent"` 被拒；缺 kind 被拒
  - 每个 kind `extra="forbid"`：多余字段报错
  - Route 构造 + `to` 必填校验
  - foreach body 从 dict 分派（agent/script）
  - **端到端**：`examples/{nas,parallel_research,batch_assess}.yaml` 经 yaml.safe_load → Workflow(**data) 成功（验收 7.4）
- `test_event.py`：
  - Event 正常构造；`type="nonexistent"` 被 Literal 拒
  - 用 `typing.get_args(EventType)` 遍历 **全部** type 逐个构造（覆盖意图，不硬编码数量）
- `test_state.py`：
  - RunState 构造；UsageSummary 构造；递归 node_breakdown 嵌套构造；node_status 取 Status Literal

## examples（逐字复制 SPEC §6.1/§6.2/§6.3）
- `examples/nas.yaml`（§6.1）/ `examples/parallel_research.yaml`（§6.2）/ `examples/batch_assess.yaml`（§6.3）

## 偏离 SPEC 处（逐条理由）
1. **`Node.name` 改为可选 `name: str = ""`**：§2.2 画的是 `name: str`（必填），但 §6.3 的 foreach `body` 无 name，而验收 7.4 要求该 yaml 能解析。§2.4 明文「name 全局唯一/存在性 = compile/ 层校验」，§0 明文 schema「零校验」，故 schema 层让 name 可选与架构一致；compile/ 后续强制顶层 node 非空唯一。不破坏任何其他验收项。
2. **EventType 实际 21 个（非 prose 的 25）**：以 §3.2 的 Literal 代码块（真契约）为准逐字实现 21 个；测试用 `get_args` 遍历而非硬编码 25，prose 计数有误属 SPEC 笔误。
3. **foreach `body` 用判别联合 `ForeachBody`**：SPEC 写 `body: AgentNode | ScriptNode`（裸 Union）。改为 `Annotated[Union[...], Field(discriminator="kind")]`，与 `AnnotatedNode` 同机制（DRY + 确定性分派），不增字段、不改语义，仅明确分派方式。

## 风险/疑问
- pydantic v2 判别联合 + `extra="forbid"`：成员各自 `kind` 为唯一 Literal，分派后整体验证，多余字段报错（验收 7.2 依赖此行为）—— 已确认 pydantic 2.12 行为符合。
- `UsageSummary` 自引用：用字符串前向引用 + 显式 `model_rebuild()`，避免运行时 warning（fail loud / clean）。
