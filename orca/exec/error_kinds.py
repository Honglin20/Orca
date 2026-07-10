"""error_kinds.py —— ErrorKind 枚举（11 值）+ 默认重试策略表 + layer 派生 + 旧值反向映射。

权威分类轴（ADR §4.1 决策 1.4 / phase-11 SPEC §2）：

  - 3 transport：TRANSPORT_NETWORK / TRANSPORT_PROCESS / TRANSPORT_TIMEOUT
  - 3 protocol： PROTOCOL_PARSE / PROTOCOL_MCP / PROTOCOL_SCHEMA
  - 4 business：BUSINESS_GATE / BUSINESS_AGENT / BUSINESS_CONFIG / BUSINESS_RATE_LIMIT
  - 1 unknown : UNKNOWN

共 11 值（phase-11 SPEC v2.1 标题订正："11 分类"指枚举值数；v1 标题"五分类"已废弃）。

本模块是**纯数据**模块：
  - ``ErrorKind`` 枚举（str mixin 便于序列化为 ``kind`` 字段值）
  - ``_DEFAULT_RETRYABLE``：kind 默认 ``retryable`` 策略表（SPEC §2.1）
  - ``_KIND_LAYER_PREFIX``：kind 前缀 → layer 派生表（Error.layer_from_kind 用，ADR §4.1 决策 1.3）
  - ``_LEGACY_ERROR_TYPE_TO_KIND``：旧 ``error_type`` 字符串 → kind 的反向映射（SPEC §4.6，
    旧 tape 重放兼容期使用；``from_failed_data`` 读 ``error_type`` 时经此表翻译）
  - ``_DEFAULT_KIND_FOR_PHASE``：ExecError.phase → 默认 kind（ADR §4.1.1 映射表，构造器用）

依赖单向：本模块不依赖 orca 其他子模块（纯枚举 + 表数据）。
"""

from __future__ import annotations

from enum import Enum


class ErrorKind(str, Enum):
    """错误分类轴（唯一）。``str`` mixin 让 ``ErrorKind.X.value`` 直接 JSON 序列化。

    命名约定：``<LAYER>_<SUBTYPE>`` —— 前缀隐含 layer（transport/protocol/business），
    ``Error.layer_from_kind()`` 据此派生（ADR §4.1 决策 1.3）。
    """

    # —— transport 层：底层连接 / 进程问题 ——
    TRANSPORT_NETWORK = "transport_network"     # TCP 断 / DNS 失败 / 连接被拒
    TRANSPORT_PROCESS = "transport_process"     # 子进程崩 / OOM-killed / 异常退出码
    TRANSPORT_TIMEOUT = "transport_timeout"     # proc.wait 超时（含 stall/hard timeout）

    # —— protocol 层：协议契约违例 ——
    PROTOCOL_PARSE = "protocol_parse"           # stream-json 解析失败 / 字段缺失
    PROTOCOL_MCP = "protocol_mcp"               # MCP EPIPE / stdio 断 / client disconnect
    PROTOCOL_SCHEMA = "protocol_schema"         # backend schema 校验失败 / validator 失败

    # —— business 层：业务逻辑失败 ——
    BUSINESS_GATE = "business_gate"             # HumanGate reject / InterruptHandler abort
    BUSINESS_AGENT = "business_agent"           # agent 自报失败 / 输出校验不过
    BUSINESS_CONFIG = "business_config"         # workflow yaml 错 / 路由死锁 / render 失败
    BUSINESS_RATE_LIMIT = "business_rate_limit" # backend 限流（可重试，需退避）

    # —— 系统级 ——
    UNKNOWN = "unknown"                         # 未分类；raw 必须保留（铁律 6）


# SPEC §2.1 默认重试策略表（kind → retryable）。
# max_attempts / 退避算法在 retry.py 内实现；此处只管 retryable 默认。
_DEFAULT_RETRYABLE: dict[ErrorKind, bool] = {
    ErrorKind.TRANSPORT_NETWORK: True,
    ErrorKind.TRANSPORT_PROCESS: False,   # OOM 子规则由 classifier 显式覆盖
    ErrorKind.TRANSPORT_TIMEOUT: False,
    ErrorKind.PROTOCOL_PARSE: False,
    ErrorKind.PROTOCOL_MCP: False,
    ErrorKind.PROTOCOL_SCHEMA: False,
    ErrorKind.BUSINESS_GATE: False,
    ErrorKind.BUSINESS_AGENT: False,
    ErrorKind.BUSINESS_CONFIG: False,
    ErrorKind.BUSINESS_RATE_LIMIT: True,
    ErrorKind.UNKNOWN: False,
}


# kind 前缀 → layer 派生（ADR §4.1 决策 1.3，v2 删 Error.layer 字段）。
# ``Error.layer_from_kind()`` 经 ``self.kind.value.split("_")[0]`` 取前缀查本表。
_KIND_LAYER_PREFIX: dict[str, str] = {
    "transport": "transport",
    "protocol": "protocol",
    "business": "business",
    "unknown": "unknown",
}


# SPEC §4.6 旧 ``error_type`` 字符串 → ErrorKind 反向映射（读兼容期使用）。
# 旧 tape 的 ``data["error_type"]`` 经 ``from_failed_data`` 读到时，按本表翻译为 kind。
# ClaudeStreamError 1:N 不可精确还原（可能 BUSINESS_AGENT），默认 PROTOCOL_PARSE，raw 注释。
_LEGACY_ERROR_TYPE_TO_KIND: dict[str, ErrorKind] = {
    "ExecTimeout": ErrorKind.TRANSPORT_TIMEOUT,
    "CliExitNonZero": ErrorKind.TRANSPORT_PROCESS,
    "ClaudeStreamError": ErrorKind.PROTOCOL_PARSE,
    "NoResultEvent": ErrorKind.PROTOCOL_PARSE,
    "SchemaValidationError": ErrorKind.PROTOCOL_SCHEMA,
    "ConfigError": ErrorKind.BUSINESS_CONFIG,
    "RenderError": ErrorKind.BUSINESS_CONFIG,
    "validator_failed": ErrorKind.PROTOCOL_SCHEMA,
    "Interrupted": ErrorKind.BUSINESS_GATE,
    "NodeLifecycleViolation": ErrorKind.PROTOCOL_PARSE,
    "RetryLoopInvariant": ErrorKind.UNKNOWN,
}


# ADR §4.1.1 ``ExecError.phase`` → 默认 ``ErrorKind`` 映射表。
# 1:1 phase 默认 kind；stream 是 1:N，默认 PROTOCOL_PARSE（保守默认；classifier 据.raw 精分）。
# ExecError 构造器在未显式传 kind 时查本表。
_DEFAULT_KIND_FOR_PHASE: dict[str, ErrorKind] = {
    "timeout": ErrorKind.TRANSPORT_TIMEOUT,
    "spawn": ErrorKind.TRANSPORT_PROCESS,
    "stream": ErrorKind.PROTOCOL_PARSE,        # 默认；classifier 据 raw 精分为 BUSINESS_AGENT
    "result_parse": ErrorKind.PROTOCOL_PARSE,
    "schema": ErrorKind.PROTOCOL_SCHEMA,
    "render": ErrorKind.BUSINESS_CONFIG,
    "config": ErrorKind.BUSINESS_CONFIG,
    "validator": ErrorKind.PROTOCOL_SCHEMA,    # ADR v2：validator 归 PROTOCOL_SCHEMA
    "interrupted": ErrorKind.BUSINESS_GATE,
    # phase-11 §4.4 编排层新增 phase（仅诊断，不参与跨层分类）：
    "max_iterations": ErrorKind.BUSINESS_CONFIG,
    "route_deadlock": ErrorKind.BUSINESS_CONFIG,
    "node_failed": ErrorKind.UNKNOWN,          # 兜底 phase；classifier 据上下文精分
}


def default_kind_for_phase(phase: str) -> ErrorKind:
    """phase → 默认 ErrorKind（ADR §4.1.1）。

    未知 phase 回退 ``UNKNOWN``（容错，不 ValueError）——phase 是诊断字段，
    新增 phase 漏补表不应让 raise 路径崩溃。
    """
    return _DEFAULT_KIND_FOR_PHASE.get(phase, ErrorKind.UNKNOWN)
