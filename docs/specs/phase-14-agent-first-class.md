# 阶段 14 SPEC —— Agent 一等化（agent 池 + 文件夹化 + 统一解析层）+ Route 输出变换

> **状态**：v2（spec-review-adversarial 对抗审闭环：2 P0 + 5 P1 + C8/C10/C11/C15 已修订；conditional-pass → ready-for-impl）
> **依据**：plan `ethereal-baking-toucan.md` · [phase-2-compile.md](phase-2-compile.md)（_load_prompts 约定加载）· [phase-4-exec.md](phase-4-exec.md)（render_prompt / _load_agent_md）· [phase-5-run.md](phase-5-run.md)（orchestrator Route / _evaluate_outputs）· [phase-10-mcp.md](phase-10-mcp.md)（MCP server）
> **范围**：① `AgentNode` 显式 `agent` 引用 + 文件夹化（`<name>/agent.md` + 资源子目录）+ frontmatter 元数据；② 统一解析层 `AgentResolver`（替代 `_load_prompts` + `_load_agent_md` 双加载债）；③ `Route.output` 终点输出变换；④ MCP `list_agents` / `get_agent` 池查询；⑤ 旧约定 deprecation。
> **不是**：子 workflow（仅预留 resolver 扩展点，不实现）；包分发 / registry（phase-15）；workspace-instruction（phase-15）；`run_agent` MCP 工具（phase-15）；后端协议改动（并行进程负责，本阶段不碰）。
> **commit 规范**：`feat(compile):` / `feat(schema):` / `feat(run):` 前缀，分支 `phase14-agent-first-class`

---

## 0. 阶段目标 + 铁律

phase 14 回答：**「agent 怎么从『内嵌 prompt 字符串』升级为『可命名、可复用、可携带资源的一等公民』？现有 compile/render 两处不一致的 agent md 加载（路径基不同 → bug 源）怎么统一？workflow 到 `$end` 时怎么按命中的那条 route 独立变换输出？」**

### 0.1 八条铁律（违反即返工）

1. **单一解析路径**：agent 引用解析只在 compile 层（`AgentResolver`），render / run 层零文件 I/O。删除 `_load_agent_md`（render 兜底，cwd 相对路径）+ `_load_prompts`（compile 旧实现），统一到 `_resolve_agents → AgentResolver.resolve`。
2. **schema 纯数据边界不动摇**：schema 只定义 yaml 契约字段 + 显式标注的 runtime cache 字段（`prompt` / `resources_root` 是 compile 物化的 runtime cache，注释 + 字段命名说清楚）。解析/校验在 compile，模板渲染在 exec，编排在 run —— 依赖单向 `schema ← compile ← run/exec ← iface`。
3. **依赖单向 + 零反向**：新增 `orca/compile/agents.py` 只依赖 `orca.schema` + `pathlib` + `yaml`。`AgentResolver` 是 `Protocol`（typing only），批 2 的 `MultiPoolResolver` / 未来 registry 是同接口的扩展实现，**schema 不变**。
4. **`agent` 与 `prompt` 互斥**：node 同时声明 `prompt` + `agent` → compile validator **error**（fail loud）。旧约定（两者皆 `None` + `name` 匹配 md）→ **deprecation warn**，内部当 `agent = name` 走同一条 resolver 路径。
5. **`Route.output` 只在 `$end` 生效**：route.to != `$end` 且 `output` 非空 → validator **warn**（死代码提示）；命中 `$end` 时 `end_route.output` **优先于** `wf.outputs`；skip 到 `$end`（不经 route 求值）→ fallback `wf.outputs`。
6. **查找顺序 first-wins**：`<workflow_dir>/agents/` → `<cwd>/agents/`（批 2 加 `extra_roots`）。每个目录内：文件夹 `<name>/agent.md` 优先，单文件 `<name>.md` 兼容兜底。
7. **frontmatter 合并优先级**：**node 内联 > agent frontmatter 默认 > schema 默认**（node 显式声明压 agent 默认；agent 默认压 schema 默认）。
8. **fail loud**：agent 引用解析不到 → `ConfigurationError` 聚合（一次性列全所有缺失）；绕过 `load_workflow` 直接 `Workflow(**raw)` → render 期 `node.prompt is None` 防御性 raise（清晰归因："agent prompt 未物化，是否绕过了 load_workflow?"）。

### 0.2 反模式（必须避免）

- ❌ render 层读 agent 文件（`_load_agent_md`）—— 双加载 + cwd 相对路径 bug（现状 `Path("agents")/f"{name}.md"` 相对 CWD，与 compile 期 `yaml_path.parent/agents` 不一致）。render 只渲染已物化的 `node.prompt`。
- ❌ 把 resources 设计成 prompt 一部分 —— resources（scripts/refs）是给 agent 的 Bash 工具用的路径，不是 prompt 文本内容。
- ❌ agent 池配置进 workflow yaml —— 池是环境级配置（批 2 `pools.toml` / env），workflow 应跨环境可移植。
- ❌ 实现 workflow resolver / 子 workflow 嵌套执行 —— YAGNI，本阶段只锁 `AgentResolver` 接口形态预留扩展点，不产出 `WorkflowResolver`。
- ❌ `Route.output` 改 drive_loop 主结构 —— 只改 `_next_node_after`（返回命中 route）+ `_evaluate_outputs`（加 `end_route` 参数），drive_loop 只多记一个 `end_route` 变量。
- ❌ MCP `run_agent` —— 单 agent 执行模型（无 tape/route/lifecycle）与 workflow 编排语义不同，推 phase-15。

### 0.3 与既有契约的关系（零/小冲突）

| 既有契约 | phase 14 处理 |
|---|---|
| `AgentNode.prompt: str\|None`（约定加载 `agents/<name>.md`）| ✅ 保留为内联入口；新增 `agent` 字段与之互斥 |
| `compile/parser.py:_load_prompts`（line 44-62）| ❌ 删除，替换为 `_resolve_agents(wf, resolver)` |
| `exec/render.py:_load_agent_md`（line 107-125）| ❌ 删除，render 不再做文件 I/O |
| `Workflow.outputs: dict[str,str]`（workflow 级输出模板）| ✅ 保留，作为 `Route.output` 的 fallback |
| `Route(to, when)`（line 32-39）| ✅ 加 `output: dict[str,str]\|None`，向后兼容 |
| `TerminateNode.outputs`（terminate 自带 outputs）| ✅ 零交互：terminate 走 `WorkflowTerminated.outputs`（orchestrator.py:664-670），**不经** `_evaluate_outputs`，与 `Route.output` 不同时触发 |
| `render_prompt` + `[User Guidance]` 段（render.py:82-104）| ✅ 零改动（prompt 已 compile 物化，guidance 段不变）|
| `router.resolve(routes, output, ctx) -> str`（line 58）| ⚠️ 改返回 `Route`（命中那条；`target = route.to`），更新 caller |
| `RunContext.with_guidance` frozen 派生 pattern（context.py:83-94）| ✅ 复用模式（批 2 `with_instructions`）|
| `ORCA_CHART_SOCK` env 注入模式（executor `_build_spawn_config`）| ✅ 复用（新增 `ORCA_AGENT_RESOURCES`）|
| MCP `start_workflow`/`get_task_status`/`resolve_gate`/`cancel_task` | ✅ 零改动，新增 `list_agents`/`get_agent` |

### 0.4 `resources_root` 归属裁定（决策记录）

两个候选：
- **方案 A**：`AgentNode.resources_root: str | None`，compile 期物化填入（与 `prompt` 物化同模式）。
- **方案 B**：compile 产 `dict[node_name → AgentHandle]` 旁路映射挂 orchestrator，schema 保持「无 runtime cache」纯粹。

**裁定方案 A**。理由：
1. `prompt` 已是 compile 物化的 runtime cache 先例（用户写 `agent: x` 时，compile 把 md 内容物化进 `node.prompt`）。`resources_root` 是同一模式的延伸，不是新破坏。
2. 方案 B 要 `make_executor(node, agent_handles, ...)` 签名加映射穿透 → orchestrator → factory → ClaudeExecutor 全链改，复杂度高，且 orchestrator 持有 handle 映射又回到「挂哪」问题。
3. 单一数据流：compile 物化进 node → executor 直读 `node.resources_root`，无旁路。

**约束**：`resources_root` 在 schema 注释明确标注「runtime cache，compile 期填，非 yaml 契约（用户写无效/忽略）」。序列化为绝对路径字符串（pydantic 可序列化，resume 重建 Workflow 可恢复）。

**承诺（防滚雪球）**：schema 的 runtime cache 字段**仅** `prompt` 与 `resources_root` 两个（均为 compile 物化的字符串缓存）。未来若出现第三个需 compile 期物化的派生数据（如 agent capability 摘要），走方案 B（旁路映射挂 orchestrator），**不再向 schema Node 加 runtime cache 字段**——守住「schema 纯数据」边界的可持续性。

### 0.5 与并行进程的边界（共存避让）

当前工作树有并行进程改后端协议（`profiles.resolve_flags` / `executor_cmds.py` / `config.py` / `dialog.py` / `exec/validator.py`）。本阶段改动与之**几乎不重叠**：

- **完全不碰**：`orca/profiles/*`、`orca/iface/cli/executor_cmds.py`、`orca/iface/cli/config.py`、`orca/gates/dialog.py`、`orca/exec/validator.py`。
- **共存（同文件不同区域，零行冲突）**：`orca/exec/claude/executor.py` —— 并行进程改 `_build_spawn_config` 的 `flags=profile.resolve_flags()` 一行；本阶段在同函数的 env overlay 处加 `ORCA_AGENT_RESOURCES`，不同行。
- **完全独立**：`orca/compile/agents.py`（新）、`orca/schema/workflow.py`、`orca/compile/parser.py`、`orca/compile/validator.py`、`orca/exec/render.py`、`orca/run/orchestrator.py`、`orca/run/router.py`、`orca/iface/mcp/server.py`。

---

## 1. 整体架构（解析层 + 数据流）

```
┌─ load_workflow(path, resolver?) ─────────────────────────────────────┐
│  yaml.safe_load → Workflow(**raw)（结构校验）                          │
│  ↓                                                                     │
│  _resolve_agents(wf, resolver):                                        │
│    for node in wf.nodes where node.agent is not None:                  │
│      handle = resolver.resolve(node.agent, context)                    │
│      node.prompt = handle.prompt          # 物化 prompt                │
│      node.resources_root = str(handle.resources_root)  # 物化资源路径   │
│      合并 handle.meta → node（node 内联优先）                           │
│  ↓                                                                     │
│  validate_workflow(wf)（含 agent/prompt 互斥 + Route.output 校验）      │
└────────────────────────────────────────────────────────────────────────┘
                          ↓ wf（所有 agent node 已物化）
┌─ Orchestrator drive_loop ─────────────────────────────────────────────┐
│  ClaudeExecutor.exec(node, ctx): render_prompt(node) → 只渲染字符串    │
│    spawn 时 env overlay 注入 ORCA_AGENT_RESOURCES=node.resources_root  │
│  Route 求值：_next_node_after → (target, route)  # route 含 output     │
│  命中 $end：_evaluate_outputs(outputs_acc, end_route=route)            │
│    end_route.output 非空 → 用它渲染；否则 fallback wf.outputs           │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 数据契约（schema 改动 —— `orca/schema/workflow.py`）

### 2.1 `AgentNode`（line 108-125）新增字段

```python
class AgentNode(Node):
    kind: Literal["agent"] = "agent"
    prompt: str | None = None        # 内联短 prompt；None + agent=None → 旧约定（deprecation）
    agent: str | None = None         # 【新】agent 池引用名（如 "analyzer"）；与 prompt 互斥
    resources_root: str | None = None  # 【新·runtime cache】compile 期填，agent 资源目录绝对路径；非 yaml 契约
    tools: list[str] | None = None
    executor: str = "claude"
    model: str | None = None
    output_schema: dict | None = None
    retry: RetryPolicy | None = None
    validator: ValidatorConfig | None = None
```

- `agent` 与 `prompt` 互斥校验在 compile validator（schema 层不做跨字段约束，沿用现有「结构校验在 compile」铁律）。
- `resources_root` 是 runtime cache：注释明示「compile 期物化，用户 yaml 写无效」；pydantic `extra="forbid"` 不阻止它作为声明字段，但用户在 yaml 写 `resources_root:` 会被 compile 忽略（resolver 总是覆盖）。

### 2.2 `Route`（line 32-39）新增字段

```python
class Route(BaseModel):
    when: str | None = None
    to: str                           # 目标 node 名 / "$end"
    output: dict[str, str] | None = None  # 【新】route 到 $end 时的输出变换模板（Jinja2）
```

### 2.3 `AgentMeta`（frontmatter 契约，定义在 compile 层 —— 见 §3）

不放 schema（meta 是 agent 文件的契约，不是 workflow yaml 的契约）。`AgentMeta` 定义在 `orca/compile/agents.py`：

```python
@dataclass(frozen=True)
class AgentMeta:
    description: str = ""             # MCP list_agents / 未来 show 用
    model: str | None = None          # agent 级默认模型（node.model 覆盖）
    tools: list[str] | None = None    # agent 级默认工具白名单（node.tools 覆盖）
    executor: str | None = None       # agent 级默认后端（node.executor 覆盖）
    # 预留扩展点（phase-15+）：workspace_instructions / capabilities / version
```

frontmatter 格式（YAML 头 + markdown body，`---` 分隔）：

```markdown
---
description: 神经架构搜索优化器，提出下一个待训练结构
model: deepseek-v4-flash
tools: [Bash, Read, Write]
---
# optimizer

你是 NAS 优化器。根据已知信息提出下一个模型结构……
```

无 frontmatter 时，整个文件当 prompt body（向后兼容现有 8 个无头 md）。

---

## 3. 解析层（新增 `orca/compile/agents.py`）

### 3.1 接口与数据类

```python
class AgentResolver(Protocol):
    """agent 引用 → AgentHandle 解析器接口（compile 层）。

    批 1：LocalPoolResolver（本地多目录查找）。
    批 2：MultiPoolResolver（name@source 拆分 + pools.toml）。
    未来：RegistryResolver（name@registry#ref + 拉取/缓存/SHA）。
    所有实现遵守同一契约：resolve(name, context) → AgentHandle | raise。
    """
    def resolve(self, name: str, *, context: "ResolveContext") -> "AgentHandle": ...

@dataclass(frozen=True)
class ResolveContext:
    workflow_dir: Path          # yaml 所在目录（局部 pool 基准）
    cwd: Path                   # 当前工作目录（cwd pool 基准）
    extra_roots: list[Path]     # 【批 2】配置的额外 pool root；批 1 恒为 []

@dataclass(frozen=True)
class AgentHandle:
    prompt: str                 # 物化后的 prompt（frontmatter 之后的 body，未 Jinja2 渲染）
    meta: AgentMeta             # frontmatter 解析出的元数据
    resources_root: Path        # 资源目录绝对路径（文件夹 agent = 目录本身；单文件 = md 所在目录）
    is_folder: bool             # 【C4】agent 形态：True=文件夹（<name>/agent.md，可含资源子目录）；False=单文件
    source: str                 # 解析来源（如 "local:examples/agents/analyzer/agent.md"），错误归因用
```

### 3.2 `LocalPoolResolver`（批 1 默认实现）

查找算法（first-wins，**聚合缺失**到一次 `ConfigurationError`）：

```
for base in [context.workflow_dir / "agents", context.cwd / "agents", *context.extra_roots]:
    folder = base / name / "agent.md"      # 文件夹形态（优先）
    single  = base / f"{name}.md"           # 单文件形态（兼容兜底）
    if folder.is_file(): return resolve_folder(folder)
    if single.is_file():  return resolve_single(single)
raise AgentNotFound(name, searched=[...])   # 列出所有搜过的路径
```

- `resolve_folder(path)`：解析 frontmatter（若有）+ body；`resources_root = path.parent`（agent 文件夹根，含 scripts/refs 子目录）。
- `resolve_single(path)`：同 frontmatter 解析；`resources_root = path.parent`（单文件时资源与 md 同目录，可放 `<name>.md` + `<name>_scripts/` 共存，但不强制）。

### 3.3 frontmatter 解析（精确算法，C6）

仅当**第 1 行** `strip() == "---"` 时进入 frontmatter 解析；随后找**第一个**其后再独占整行（`line.strip() == "---"`）的行作为 frontmatter 结束。两点之间为 YAML 头，之后为 body：

```python
lines = text.splitlines()
if lines and lines[0].strip() == "---":
    # 找 closing ---
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            frontmatter_yaml = "\n".join(lines[1:i])
            body = "\n".join(lines[i+1:])
            break
    else:
        # 首行 --- 但无闭合 --- → fail loud（frontmatter 未闭合）
        raise ConfigurationError([f"{source}: frontmatter 首行 '---' 无闭合 '---'"], [])
else:
    frontmatter_yaml = None
    body = text  # 整文件当 body（向后兼容无头 md）
meta = AgentMeta(**yaml.safe_load(frontmatter_yaml)) if frontmatter_yaml else AgentMeta()
```

- **body 内的 `---`（markdown 水平线）不再识别** —— 只看首行 + 第一个闭合行，避免 body 水平线误判为 frontmatter 边界。
- frontmatter YAML 损坏 → fail loud（`ConfigurationError` 指明文件 + 行 + yaml 错误）。
- frontmatter 含未知字段 → fail loud（`AgentMeta` 用 `dataclass` 严格构造，未知 kwarg → `TypeError`，包装成 `ConfigurationError`，防拼写错误静默忽略）。

### 3.4 为批 2 / 子 workflow 预留（不实现，锁接口形态）

- **批 2 包分发**：`node.agent` 字符串从 `"analyzer"` 扩展为 `"analyzer@mxint-tools"`。`MultiPoolResolver.resolve` 内部按 `@` 拆 source：无 `@` → LocalPoolResolver 行为；有 `@` → 查 pool registry。**schema 完全不变**（`agent: str` 仍是字符串）。
- **子 workflow（未来）**：`AgentResolver` 与未来 `WorkflowResolver` 共享 `resolve(ref, context) → handle` 接口形态。本阶段不定义 `WorkflowResolver`（YAGNI），但 `ResolveContext` 设计成可复用（含 workflow_dir/cwd/extra_roots，workflow 引用同样需要）。

---

## 4. compile 统一（`orca/compile/parser.py` + `orca/exec/render.py`）

### 4.1 `load_workflow` 注入 resolver + deprecation warn 通道（C1）

```python
def load_workflow(path: str | Path, resolver: AgentResolver | None = None) -> Workflow:
    yaml_path = Path(path)
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    wf = Workflow(**raw)  # 结构校验
    if resolver is None:
        resolver = LocalPoolResolver()  # 默认本地 pool
    context = ResolveContext(workflow_dir=yaml_path.parent, cwd=Path.cwd(), extra_roots=[])
    _resolve_agents(wf, resolver, context)  # 替代 _load_prompts（内部发 deprecation warn）
    validate_workflow(wf)  # 语义校验（含互斥 + Route.output）
    return wf
```

**C1 裁定（warn 通道）**：deprecation warning 用 Python 标准库 `warnings.warn(message, DeprecationWarning, stacklevel=...)`，由 `_resolve_agents` 在检测到旧约定（`prompt=None` + `agent=None` + name 匹配 md 命中）时发出。**不改 `load_workflow` 返回签名**（仍返回 `Workflow`，避免 ~8 个 caller 破坏：CLI run/validate/list/resume + MCP start_workflow + RunManager + 测试）。

- 展示通道：CLI `validate` / `run` 命令用 `warnings.catch_warnings(record=True)` 包住 `load_workflow` 调用，捕获 `DeprecationWarning` → `typer.echo(..., err=True)`（§8.3 / C15）。
- 测试：pytest `recwarn` fixture 直接断言（无需 catch_warnings）。
- **不选对抗审建议的「`load_workflow` 返回 `(Workflow, list[str])`」**：破坏 ~8 caller、改动面大；Python warnings 是 deprecation 的标准机制、零签名破坏、与 pyproject 已有 `filterwarnings` 配置一致。surface 此选择，理由是轻量 + 标准。

### 4.2 `_resolve_agents` 替代 `_load_prompts`

遍历 `wf.nodes`（**含 foreach body 内嵌 AgentNode** —— 递归进 `ForeachNode.body`；现状 `_load_prompts` 不覆盖 body，是 bug，本阶段修），对每个 AgentNode：

1. 判定引用来源（§7 迁移优先级）：
   - `node.agent` 非空 → `name = node.agent`（新路径）
   - `node.prompt` 非空 → 跳过（内联，无需 resolver）
   - 两者皆 None → `name = node.name`（旧约定，**发 DeprecationWarning**；内部走 resolver）
2. `handle = resolver.resolve(name, context)`
3. 物化：`node.prompt = handle.prompt`；`node.resources_root = str(handle.resources_root.resolve())`
4. 合并 `handle.meta` → node（**node 内联字段优先**）：
   - `model`：`node.model is None` → 用 `meta.model`；否则保留 node.model
   - `executor`：`node.executor == "claude"`（schema 默认值）→ 用 `meta.executor`；否则保留（**注意**：无法区分"用户显式写 claude"与"默认值"，故 executor 合并较弱，文档建议 agent 级 executor 在 frontmatter 声明 + node 不写 executor）
   - `tools`（**C3 None 消歧**）：`tools` 的 `None` = 未声明（用 `meta.tools` 或全开默认）；显式 `[]` = 禁工具。合并规则：`node.tools is None and meta.tools is not None → meta.tools`；否则保留 node.tools（含 `[]` 禁工具语义）
5. 聚合所有 `AgentNotFound` → 一次 `ConfigurationError`（列全缺失名 + 搜过路径）。

**C9 foreach body 强约束**：foreach body 内嵌的 AgentNode **无 name**（body 是内联 node 定义，不强制 name 唯一），旧约定 name-fallback 不适用。故 body 的 AgentNode 必须显式 `agent:` 或内联 `prompt:`；body 双 None → compile validator **error**（非 warn，fail loud，因无法 fallback）。

### 4.3 删除 `exec/render.py:_load_agent_md`（line 107-125）

`render_prompt`（line 82-104）简化为：

```python
def render_prompt(node, ctx: RunContext) -> str:
    if not node.prompt:  # C7：None 或空串 "" 都防（空 prompt 给 claude 行为未定义）
        raise ExecError(phase="render", message=(
            f"agent {node.name!r} 的 prompt 未物化或为空（node.prompt={node.prompt!r}）。"
            "是否绕过了 load_workflow 直接构造 Workflow？agent 引用必须在 compile 期解析。"))
    base = render_template(node.prompt, ctx)
    guidance_section = ctx.guidance_prompt_section()
    return base + guidance_section if guidance_section else base
```

消除双加载债 + cwd 相对路径 bug。render 层从此零文件 I/O。

---

## 5. Route 输出变换（`orca/run/router.py` + `orca/run/orchestrator.py`）

### 5.1 `router.resolve` 改返回命中 Route

```python
# 现状：def resolve(routes, output, ctx) -> str
# 改为：
def resolve(routes: list[Route], output: Any, ctx: RunContext) -> Route:
    """first-match-wins；返回命中的 Route 对象（target = route.to）。"""
```

实现要点：router.py 内部三处 `return route.to`（line 95 兜底 / line 104 when 命中）改为 `return route`。skip 容错（line 99-102）不变（仍继续找兜底），只是最终返回兜底 route 对象。更新所有 caller（`orchestrator._next_node_after` line 767 + 3 个测试文件直调）—— caller 把 `target = result` 改为 `route = result; target = route.to`。

### 5.2 `_next_node_after`（orchestrator.py:756-773）返回 `(target, route)`

```python
async def _next_node_after(self, current, outputs_acc, raw_output) -> tuple[str, Route]:
    routes = self._routes_of(current)
    ctx_for_route = self._make_ctx(outputs_acc)
    route = resolve(routes, raw_output, ctx_for_route)  # 返回 Route 对象
    target = route.to
    await self.bus.emit("route_taken", {"from": current, "to": target})
    return target, route
```

更新 2 个 caller（**C2 统一语义**：两条路径都经 `_next_node_after` 拿 route，无特殊分支）：
- **drive loop（line 673）**：`current, route = await self._next_node_after(current, outputs_acc, raw_output)`；若 `current == "$end"` → `end_route = route`。
- **skip 路径（line 641）**：`current, route = await self._next_node_after(current, outputs_acc, None)`。`router.resolve` 在 `output is None`（skip）时启用 skip 容错（router.py:92-104），落到 `when=None` 兜底 route 并**返回该 route 对象**（含 output）。故 skip 到 `$end` 时 `end_route = 兜底 route`（若兜底 route 带 output 则生效，否则 fallback `wf.outputs`）——**与 drive loop 同一语义**，无双分支。

### 5.3 `_evaluate_outputs`（orchestrator.py:948-961）加 `end_route`

```python
def _evaluate_outputs(self, outputs_acc, *, end_route: Route | None = None) -> dict[str, Any]:
    templates = (end_route.output if (end_route and end_route.output) else self.wf.outputs)
    if not templates:
        return {}
    ctx = self._make_ctx(outputs_acc)
    return {k: render_template(v, ctx) for k, v in templates.items()}
```

drive loop（line 675）改：`return self._evaluate_outputs(outputs_acc, end_route=end_route)`。

---

## 6. MCP 池暴露（`orca/iface/mcp/server.py`）

新增 2 个纯读工具（注册到 `_register_tools`，line 190-219）：

```python
async def tool_list_agents(self, workflow_yaml: str | None = None) -> dict:
    """列出可用 agent。默认扫 <cwd>/agents/；传 workflow_yaml 则同时扫其同目录 agents/。
    返回 {agents: [{name, description, has_resources, source}]}。
    has_resources = 该 agent 是文件夹形态（含资源子目录）。"""

async def tool_get_agent(self, name: str, workflow_yaml: str | None = None) -> dict:
    """返回单个 agent 详情：prompt（截断前 500 字）+ meta（model/tools/executor）+ resources（文件列表）。
    agent 不存在 → {error: "..."}（不 raise，MCP 友好）。"""
```

- 复用 `LocalPoolResolver` 扫描（不重复实现查找逻辑，DRY）。
- `run_agent` 推 phase-15（单 agent 执行模型语义不同）。

---

## 7. 迁移 + deprecation（`orca/compile/validator.py`）

### 7.1 引用来源优先级（compile validator `_check_agent_prompt_exclusive`）

| `node.prompt` | `node.agent` | 判定 |
|---|---|---|
| 非空 | 非空 | **error**（互斥违反，fail loud）|
| 非空 | None | 内联 prompt，零改动 |
| None | 非空 | 新路径，resolver 解析 `agent` 名 |
| None | None（**顶层 node**）| **旧约定**（`name` 匹配 md）→ **warn**，内部当 `agent = name` 走 resolver |
| None | None（**foreach body 内嵌**）| **error**（C9：body 无 name，无法 fallback，必须显式 `agent:` 或内联 `prompt:`）|

### 7.2 deprecation warn 文案 + 通道（C1）

由 `_resolve_agents` 发出（Python 标准库）：

```python
import warnings
warnings.warn(
    f"agent '{name}' 使用旧约定（prompt 省略 + name 匹配 agents/<name>.md）。"
    f"请改为显式引用：在 node 上设 `agent: {name}`。旧约定将在未来版本移除。",
    DeprecationWarning,
    stacklevel=3,  # 指向 load_workflow 的调用者（CLI / 测试）
)
```

- **通道 = Python `warnings.warn(DeprecationWarning)`**（C1 裁定，§4.1）。不改 `load_workflow` 返回签名。
- 批 1 只 warn（不破坏现有 examples）；未来某版（phase-15+）改 error。
- `examples/` 现有示例在「整理 examples」阶段逐步迁移到 `agent:` 显式形式（迁移后 warn 消失）。

### 7.3 validator 扩展（`orca/compile/validator.py`）

- `_check_agent_prompt_exclusive`：互斥 error + 旧约定 warn（上表）。
- `_iter_templates`（line 387-455 附近）扩展：`Route.output` 每 key 加入 Jinja2 浅校验（与 `wf.outputs` 同形，valid_roots = 现有 root set）。
- `_check_route_output_only_at_end`（新）：`route.to != "$end"` 且 `route.output` 非空 → warn（死代码提示，非 error）。

---

## 8. 验证

### 8.1 单测矩阵（`tests/compile/test_agents.py` 新增 + 既有 test_compile/test_validator 扩展）

| 测试 | 覆盖铁律 |
|---|---|
| `LocalPoolResolver` 文件夹优先于单文件 | #6 |
| 查找顺序 workflow_dir > cwd > extra_roots | #6 |
| frontmatter 解析（无头/有头/坏头 fail loud/未知字段 fail loud）| #8 |
| 合并优先级 node > meta > 默认（model/tools/executor 各一组）| #7 |
| `AgentNotFound` 聚合（多缺失一次列全）| #8 |
| `_resolve_agents` 物化 prompt + resources_root + 合并 meta | #1 |
| validator 互斥 error / 旧约定 warn / Route.output Jinja2 / 非 $end warn | #4 #5 |
| `render_prompt` 在 `node.prompt is None` 时防御性 raise | #8 |
| `router.resolve` 返回 Route（target = route.to）| #5 |
| `_evaluate_outputs(end_route=...)` 用 route.output / fallback wf.outputs / skip 到 $end fallback | #5 |

### 8.2 opencode e2e（`tests/e2e_phase14/`，**真跑 opencode + deepseek-v4-flash，不 mock**）

| E2E | 场景 | 断言（可机器验证）|
|---|---|---|
| E2E-1 | 旧约定 workflow（`prompt=None` + name 匹配 md）跑通 | tape 含 `workflow_completed` + 运行期捕获到 `DeprecationWarning`（`recwarn` / CLI stderr 含"旧约定"）|
| E2E-2 | 新 `agent: analyzer` 显式引用 | tape 含 `workflow_completed` + 无 DeprecationWarning |
| E2E-3a（单测）| 文件夹化 agent spawn env 注入 | 断言 `ClaudeExecutor._build_spawn_config` 产出的 env overlay 含 `ORCA_AGENT_RESOURCES=<abs path>`（mock spawn，不真调 opencode，确定性）|
| E2E-3b（e2e）| 文件夹化 agent（`analyzer/agent.md` + `scripts/flag.txt`）+ frontmatter（model/tools），prompt 显式 instruct `cat $ORCA_AGENT_RESOURCES/scripts/flag.txt 并把内容作为 output` | tape 的 agent output 含 `flag.txt` 内容（容忍 LLM 重跑，断言子串）+ frontmatter `model` 生效（spawn argv 含 `--model deepseek-v4-flash`）|
| E2E-4 | Route output 分类器（**确定性**：`set` node 产固定 flag，两条 route `when` 二分到 `$end`，各带不同 output 模板）| 两个 set 值各跑一次，final output 分别按命中 route 的 output 渲染（不走 LLM，纯 set→route）|
| E2E-5 | Route output fallback（route 到 `$end` 无 output，靠 `wf.outputs`）| final output 走 `wf.outputs` 模板 |
| E2E-6 | MCP `list_agents` / `get_agent`（bound method 直调 + 真扫 `agents/`）| list 返回含文件夹 agent（`has_resources=True`）+ 单文件 agent（`has_resources=False`，C4 不误报）；get 返回 prompt 截断 + meta |

**C10/C11 修订**：E2E-3 拆成单测（env 注入，确定性）+ e2e（agent 真读资源，容忍 LLM 重跑断言子串）；E2E-4 改 `set` node 驱动的确定性路由（不依赖 LLM 输出决定命中哪条 route）。所有 agent 类 e2e 真跑 opencode（`integration` marker 或 e2e 目录），**禁止 fake/mock executor**。

### 8.3 回归 + CLI validate 展示 warnings（C15）

- 既有 `tests/compile/`、`tests/exec/test_render.py`、`tests/run/`、`tests/e2e_phase12/`、`tests/e2e_phase13/` 全过（baseline 1224）。
- `orca validate examples/<旧约定>.yaml`：`commands.py:261-274` 的 `validate` 命令用 `warnings.catch_warnings(record=True)` 包住 `load_workflow`，捕获 `DeprecationWarning` → `typer.echo(..., err=True)` 展示，**然后** echo "✓ 校验通过"（warn 不阻断，exit 0）。error 仍 exit 非 0。

---

## 9. 关键决策备忘

- **D1 resources_root 归属**：方案 A（进 AgentNode 作 runtime cache），理由见 §0.4。
- **D2 入口文件名**：`agent.md`（非 `index.md`）—— 语义明确（不是 web 首页索引），与目录名解耦。
- **D3 单 md 长期兼容**：不强制迁文件夹（文件夹对有资源的 agent 才有价值；纯 prompt agent 单 md 合法）。
- **D4 `router.resolve` 改签名**：返回 `Route` 而非新增函数 —— 单一路径，避免 resolve/resolve_with_route 双入口。
- **D5 MCP `run_agent` 推后**：单 agent 执行模型（无 tape/route/lifecycle）与 workflow 编排语义不同，phase-15 与 workspace-instruction 一起做（单 agent run 是 workspace-instruction 主要消费场景）。

---

## 10. 风险

- **R1 删 `_load_agent_md` 的破坏性**：绕过 `load_workflow` 的程序化 `Workflow(**raw)` 会在 render 期崩。缓解：§4.3 防御性 raise（`if not node.prompt`）+ 清晰归因。对抗审核实：`tests/schema/test_workflow.py:236,244` 的 `Workflow(**raw)` 构造是 **schema 结构测试**（不走 render），**不撞** render 期 raise；`MCP start_workflow` 经 `RunManager → load_workflow`（run_manager.py:151），正常路径。实现时 grep `Workflow(**` 确认无其他 render 路径消费者。
- **R2 `router.resolve` 改签名漏更新 caller**：编译期不报，运行期语义错。缓解：grep 所有 `resolve(` caller（orchestrator + 测试），逐一更新 + 单测覆盖。
- **R3 frontmatter 解析健壮性**：现有 8 个 md 无 frontmatter，解析须把「无头」当合法（整文件 body）。坏头 fail loud 指明文件行。
- **R4 与并行进程的 executor.py 共存**：并行进程改 `_build_spawn_config` 的 flags 行，本阶段在同函数加 env overlay。需基于含并行改动的工作树改（不回退它们的 flags 行）。若并行进程后续大改 _build_spawn_config 结构，需 rebase。
- **R5 foreach body 内嵌 agent 引用**：`_resolve_agents` 必须递归进 `ForeachNode.body`，否则 foreach 里的 agent 引用不物化 → render 崩。单测覆盖。
- **R6 schema 纯数据张力**：`resources_root` 进 AgentNode 是务实妥协（D1）。若 review 强烈反对，回退方案 B（旁路映射），代价是 make_executor 签名穿透。SPEC 裁定 A，留 B 作 fallback。
