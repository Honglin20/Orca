"""workflow.py —— 工作流静态结构定义（纯数据，零执行逻辑）。

回答「跑什么？」：Workflow / Node（基类 + 4 个 kind）/ Route / InputDef / ParallelGroup。

控制流模型（phase 5 单轨化）：**routes 单指针** + **parallel 组显式并行**。
- routes（node 与 parallel 组的出边）：first-match-wins，每步只去一个 target。
- parallel 组（顶层独立列表）：branches 并行 asyncio.gather，全部完成后按组 routes 推进。

铁律：本模块只有 pydantic 模型，无解析、无校验、无持久化。
- YAML→Workflow 在 compile/ 阶段做
- name 全局唯一 / entry 存在 / routes 引用合法 / parallel 组校验 / 死锁检测 等「结构校验」
  也在 compile/ 层（见 SPEC §2.4：schema 层只定义字段，compile/ 层校验）
- 模板渲染、输出摘取在 exec/ 阶段做
"""

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class InputDef(BaseModel):
    """工作流输入参数声明（Workflow.inputs 的元素）。"""

    model_config = ConfigDict(extra="forbid")

    type: str  # "string" / "int" / "boolean" / "list" / ...
    required: bool = True
    default: Any = None
    description: str = ""


class Route(BaseModel):
    """条件路由项。first-match-wins；`when=None` 表示兜底（catch-all）。"""

    model_config = ConfigDict(extra="forbid")

    when: str | None = None  # Jinja2 表达式；None = 兜底
    to: str  # 目标 node 名 / "$end"


class Node(BaseModel):
    """所有 node 共有字段（基类）。

    `name` 在 schema 层可选：顶层 node 的「非空 + 全局唯一」由 compile/ 层强制
    （SPEC §2.4），foreach 的内嵌 `body` 模板本就无名（SPEC §6.3）。

    控制流只由 ``routes`` 表达（单指针 first-match-wins）；静态依赖双轨已在
    phase 5 单轨化迁移中废除。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = ""  # 唯一标识；可选，compile/ 层强制顶层非空唯一
    routes: list[Route] = []  # 条件路由（first-match-wins）；唯一控制流


class AgentNode(Node):
    """LLM agent 节点（核心 kind）。

    prompt 约定：省略/None → 从 agents/<name>.md 加载（compile/ 层做）；非空 → 内联短 prompt。
    输出摘取：output_schema=None → 自由文本（取整段 result）；非 None → 结构化（exec/ 层做）。
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["agent"] = "agent"
    prompt: str | None = None  # 内联短 prompt；None=约定加载 agents/<name>.md
    tools: list[str] | None = None  # None=全开（默认）；[...]=白名单
    executor: str = "claude"  # "claude" / "ccr" / "codex"（未来）
    model: str | None = None  # 模型覆盖
    output_schema: dict | None = None  # None=自由文本；{...}=结构化 JSON schema


class ScriptNode(Node):
    """确定性 shell 命令节点（不烧 token）。

    输出：{stdout, stderr, exit_code}；parse_json=True 时额外 output.json=<解析结果>。
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["script"] = "script"
    command: str  # shell 命令（支持 Jinja2 渲染）
    parse_json: bool = False  # True=解析 stdout 为 JSON 存入 output
    timeout: float | None = None  # 超时秒


class SetNode(Node):
    """纯计算存值节点（不烧 token、不跑命令）。

    values 为 {key: Jinja2 表达式}，compile/exec 层求值后存入 output。
    用途：累积状态、算中间变量、存「当前最佳」。
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["set"] = "set"
    values: dict[str, str]  # {key: Jinja2 表达式}


# foreach body 的判别联合：仅允许 agent / script（SPEC §2.3「不含 set/foreach」）。
# 用 Field(discriminator="kind") 与 AnnotatedNode 同机制，确定性分派。
ForeachBody = Annotated[
    Union[AgentNode, ScriptNode],
    Field(discriminator="kind"),
]


class ForeachNode(Node):
    """动态并行节点（运行时才知道几个分支）。

    输出：{"outputs": [...], "errors": {...}, "count": N}，下游 {{ node.output.outputs }}。
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["foreach"] = "foreach"
    source: str  # 上游数组字段路径（Jinja2，如 "finder.output.candidates"）
    item_var: str = "item"  # 循环变量名（注入 body 的 prompt 上下文）
    index_var: str = "_index"  # 索引变量名
    body: ForeachBody  # 每个元素跑什么（嵌套 node，不含 set/foreach）
    max_concurrent: int = 10  # 分批大小
    failure_mode: Literal["fail_fast", "continue_on_error", "all_or_nothing"] = "fail_fast"


class ParallelGroup(BaseModel):
    """静态并行组（顶层独立列表项，Workflow.parallel 的元素）。

    branches 是已知 node 名列表（必须在 nodes 里定义），全部并行执行，
    等全部完成后（asyncio.gather）按 routes 推进单指针。

    用于表达 DAG 分叉+合并（diamond）：某 node 的 route.to 指向 parallel 组名，
    组的 branches 并行跑完后，组的 route 推进单指针。

    name 与 node 名共享命名空间（全局唯一，compile/ 层强制）；entry 不能指向
    parallel 组（entry 必须是 node）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str  # 组名（全局唯一，与 node 名共享命名空间）
    branches: list[str]  # 并行分支的 node 名（≥2，必须在 nodes 中已定义）
    failure_mode: Literal["fail_fast", "continue_on_error", "all_or_nothing"] = "fail_fast"
    routes: list[Route] = []  # 组完成后路由（同 node.routes 语义）


# 顶层 node 判别联合：按 kind 字段分派到具体子类。
# 与 ForeachBody 对照：这里包含全部 4 个 kind（顶层 DAG），body 仅允许 agent/script。
AnnotatedNode = Annotated[
    Union[AgentNode, ScriptNode, SetNode, ForeachNode],
    Field(discriminator="kind"),
]


class Workflow(BaseModel):
    """工作流定义（顶层结构）。

    entry 为唯一显式入口（必须是 node 名，不能是 parallel 组名）；nodes 为判别联合；
    parallel 为静态并行组独立列表（表达 DAG 分叉+合并）；outputs 为最终输出映射。
    所有结构合法性（entry 存在且非组、name 唯一含组名、routes 引用合法、parallel 组
    结构、死锁检测）由 compile/ 层校验。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    entry: str  # 起始 node 名（显式，唯一入口；不能是 parallel 组）
    inputs: dict[str, InputDef] = {}  # 工作流输入声明（可选）
    nodes: list[AnnotatedNode]  # 所有节点（discriminated union）
    parallel: list[ParallelGroup] = []  # 静态并行组（顶层独立列表）
    outputs: dict[str, str] = {}  # 最终输出映射 {key: "{{ node.output.field }}"}
