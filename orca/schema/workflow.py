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
    # phase-14：route 到 $end 时的输出变换模板（每 key 独立 Jinja2 渲染）。
    # None = 走 wf.outputs fallback；非空 = 用它替代 wf.outputs 渲染 final output。
    # 仅 to="$end" 时生效（route.to 非 $end 且 output 非空 → compile validator warn 死代码）。
    output: dict[str, str] | None = None


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


class RetryPolicy(BaseModel):
    """节点级重试策略（phase 11 §9.5.2，Orca 自创设计，借鉴 Conductor 思路）。

    触发重试的条件：``node_failed`` 事件的 ``error_type`` 命中 ``retry_on`` 白名单。
    用户 SIGINT 中断（``was_interrupted=true``）**不**进重试白名单判定 —— retry loop
    优先短路退出（见 SPEC §9.5.2 error_type 对齐表 / was_interrupted 短路）。

    Conductor 对照（不冒充参考）：Conductor 为 ``backoff: Literal["fixed","exponential"]``
    + 单 ``delay_seconds`` + ``retry_on: Literal["provider_error","timeout"]``，无
    ``jitter`` / ``max_delay`` / ``linear``。本字段语义不同。
    """

    model_config = ConfigDict(extra="forbid")

    # ge=1：max_attempts=0 会让 retry loop range(1,1) 空跑 → 撞「不可达」分支，错误信息
    # 误导。schema 层 fail loud（"max_attempts 必须 ≥1"）让配置错在加载期暴露。
    max_attempts: int = Field(default=3, ge=1)  # 总尝试次数（含首次）；1 = 等价无 retry
    backoff: Literal["constant", "linear", "exponential"] = "exponential"
    # ge=0：负 delay 会产生负 sleep（asyncio.sleep 负值立即返回，但语义错且掩盖配置错）。
    initial_delay_seconds: float = Field(default=1.0, ge=0.0)
    max_delay_seconds: float = Field(default=60.0, ge=0.0)  # 单次延迟上限（防 exponential 爆炸）
    retry_on: list[
        Literal["spawn_error", "timeout", "api_error", "http_429", "validator_failed"]
    ] = ["spawn_error"]
    jitter: bool = True  # ±20% jitter 防雪崩（同一批 429 同时重试）


class ValidatorConfig(BaseModel):
    """语义输出校验配置（phase 11 §9.6.2，LLM 二次校验 agent output 语义质量）。

    与 ``output_schema``（shape/type 结构化校验，exec/ 层确定）正交：``criteria`` 是自然语言
    描述的语义标准（如「model_class 必须是合法 Python 标识符」「weights_path 必须是绝对路径」），
    由 ``validate_output`` spawn 第二个 claude -p 做判断，返回 ``{passed, issues}``。

    ``max_retries`` 是 validator 自身的重试预算（SPEC §11.6 deviation）：与 ``RetryPolicy`` 的
    transient 失败重试**独立**。``max_retries=N`` 表示校验失败可再跑 N 次（总尝试 = N+1）。
    每次重试把上次的 issues 作为 guidance 拼进 prompt（``ctx.with_guidance``），让 agent 修正。

    ``model`` 可选指定校验用的 LLM 模型（省 token：用 haiku 校验 sonnet 的产出）。
    """

    model_config = ConfigDict(extra="forbid")

    # min_length=1：空 criteria 是配置错（无校验标准 = validator 无意义），加载期 fail loud（铁律 12），
    # 与 RetryPolicy.max_attempts 的 ge=1 同模式。
    criteria: str = Field(min_length=1)  # 自然语言校验标准（喂给 validator claude 的 prompt）
    # ge=0：max_retries=0 表示只校验一次，失败即放弃（不重跑 agent）。
    max_retries: int = Field(default=1, ge=0)  # 校验失败时的 agent 重跑次数（总尝试 = max_retries+1）
    model: str | None = None  # 校验用模型覆盖（None=默认模型；可设 haiku 省 token）


class AgentNode(Node):
    """LLM agent 节点（核心 kind）。

    prompt 来源（phase-14 三态，compile validator 强制 + deprecation）：
      - ``prompt`` 非空 → 内联短 prompt（直接渲染）
      - ``agent`` 非空 → 引用 agent 池（``agents/<agent>/agent.md`` 或 ``agents/<agent>.md``），
        compile 期由 ``AgentResolver`` 物化进 ``prompt`` + ``resources_root``。
        与 ``prompt`` **互斥**（同时非空 → compile validator error）。
      - 两者皆 None（旧约定）→ ``name`` 匹配 ``agents/<name>.md``，**deprecation warn**，
        内部当 ``agent=name`` 走 resolver。foreach body 无 name，body 双 None → error。
    ``resources_root`` 是 compile 物化的 **runtime cache**（与 ``prompt`` 同模式：用户写 ``agent``
    时 compile 填 prompt + resources_root）。非 yaml 契约字段（用户写无效，resolver 总覆盖）。
    输出摘取：output_schema=None → 自由文本（取整段 result）；非 None → 结构化（exec/ 层做）。
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["agent"] = "agent"
    prompt: str | None = None  # 内联短 prompt；None=约定/引用加载（compile/ 层物化）
    agent: str | None = None  # 【phase-14】agent 池引用名（如 "analyzer"）；与 prompt 互斥
    # 【phase-14 · runtime cache】compile 期由 resolver 物化填入，agent 资源目录绝对路径。
    # 非 yaml 契约（用户写无效）；spawn 时 executor 注入 env ORCA_AGENT_RESOURCES 给 agent Bash 工具。
    resources_root: str | None = None
    tools: list[str] | None = None  # None=全开（默认）；[...]=白名单
    executor: str = "claude"  # "claude" / "ccr" / "codex"（未来）
    model: str | None = None  # 模型覆盖
    output_schema: dict | None = None  # None=自由文本；{...}=结构化 JSON schema
    retry: RetryPolicy | None = None  # None=不重试（向后兼容）；见 RetryPolicy
    validator: ValidatorConfig | None = None  # None=不校验（向后兼容）；见 ValidatorConfig


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


class WaitNode(Node):
    """等待节点（phase 11 §9.7）：``asyncio.sleep`` 一段时长，可被 Ctrl+G 打断。

    典型用途：API rate-limit 退避、轮询间隔、人工节奏控制。``duration`` 支持 Jinja2
    渲染（如 ``"{{ inputs.wait_time }}s"``），单位 ``s``/``m``/``h``/``d`` 或纯数字（秒）。

    ``interruptible=True``（默认）：WaitExecutor 注册 wait handle，用户 Ctrl+G 时
    ``bus.notify_all_waits()`` 立即打断，emit ``wait_completed{interrupted=True}``。
    ``interruptible=False``：必须等满，Ctrl+G 等下一 node 边界生效（与中断系统既有契约一致）。
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["wait"] = "wait"
    duration: str  # Jinja2 渲染："30s"/"5m"/"2h"/"1d"/"30"（纯数字 = 秒）
    reason: str = ""  # 人类可读说明（写入 tape 便于调试）
    interruptible: bool = True


class TerminateNode(Node):
    """显式工作流终止节点（业务级成功/失败退出点）。

    触达即终止，**不评估 routes**（compile 层强制 routes 为空，语义冲突 fail loud）。
      - ``status="success"`` → orchestrator emit ``workflow_completed``，data.outputs
        用本节点的 ``outputs``（渲染后）替代 ``workflow.outputs``。
      - ``status="failed"`` → orchestrator emit ``workflow_failed``，error_type
        =``WorkflowTerminated``，message=渲染后的 ``reason``，node=本节点名。

    与默认 ``route.to="$end"`` 的区别：那个只能 success 终止；terminate 能显式 failed。
    典型用途：分类器走不到任何 handler 时显式 reject（业务兜底，非错误处理）。

    约束（compile 层校验，违反 → ConfigurationError）：
      - ``routes`` 必须空（terminate 不评估路由）
      - 不能作为 ``workflow.entry``（必须先经业务节点才有意义）
      - 不能出现在 ``ParallelGroup.branches`` 或 ``ForeachNode.body`` 里（同 Conductor 限制：
        terminate 表达「整个 workflow 的终止」，组内/循环内终止语义不清）
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["terminate"] = "terminate"
    status: Literal["success", "failed"]
    reason: str = ""  # Jinja2 渲染，写入 workflow_failed.data.message（status=failed 时）
    outputs: dict[str, str] = {}  # status=success 时替代 workflow.outputs（每 key 独立 Jinja2 渲染）


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
# 与 ForeachBody 对照：这里包含全部 6 个 kind（顶层 DAG），body 仅允许 agent/script
# （terminate 在 foreach body 内无意义，故不进 ForeachBody 联合）。
AnnotatedNode = Annotated[
    Union[AgentNode, ScriptNode, SetNode, ForeachNode, WaitNode, TerminateNode],
    Field(discriminator="kind"),
]


class Workflow(BaseModel):
    """工作流定义（顶层结构）。

    entry 为唯一显式入口（必须是 node 名，不能是 parallel 组名）；nodes 为判别联合；
    parallel 为静态并行组独立列表（表达 DAG 分叉+合并）；outputs 为最终输出映射。
    所有结构合法性（entry 存在且非组、name 唯一含组名、routes 引用合法、parallel 组
    结构、死锁检测）由 compile/ 层校验。

    execute phase（``nodes``）的 agent **不配 ask_user/gate 工具**（compile validator 强制，
    铁律 7：execute phase 永不中断）。setup phase 已在 in-session v5 §6.1 删除——主 session
    经 ``orca next --output`` 直接产 output，旧 ``setup:`` 段由 ``extra="forbid"`` 拒绝（fail loud）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    entry: str  # 起始 node 名（显式，唯一入口；不能是 parallel 组）
    inputs: dict[str, InputDef] = {}  # 工作流输入声明（可选）
    nodes: list[AnnotatedNode]  # execute phase 节点（discriminated union）
    parallel: list[ParallelGroup] = []  # 静态并行组（顶层独立列表）
    outputs: dict[str, str] = {}  # 最终输出映射 {key: "{{ node.output.field }}"}
