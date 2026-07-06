"""error.py —— ExecError（字段集 {kind, message, phase, node, raw}）+ 错误→事件映射辅助。

phase-11 SPEC v2.1 §4.1 / ADR §4.1 决策 1.1：``kind`` 是**唯一分类轴**，驱动重试决策 /
dispatch / Event payload。``phase`` / ``node`` / ``raw`` 皆只读诊断（铁律：任何层不得据它们
重新分类）。

字段集（v2.1，闭环审视 I9）：
  - ``kind: ErrorKind`` —— 唯一分类轴（必填）
  - ``message: str`` —— 人读
  - ``phase: str`` —— executor 视角诊断子字段（timeout/stream/... 9 类 + 编排层新增 2 类）
  - ``node: str | None`` —— 失败 node（adapter 注入）
  - ``raw: dict | None`` —— 原始 payload（backend 返回 / stderr）

不带（ADR §4.1 决策 1.1）：
  - ``layer``（kind 前缀派生，见 result.Error.layer_from_kind）
  - ``retryable``（重试决策查 kind 默认策略表，不是 exception 携带）
  - ``cause_id``（持久化层概念，Event emit 时 reducer 注入）

**与旧 ``error_type`` 字段的关系**（迁移期）：``error_type`` 不再是字段，降级为**只读派生属性**
（返回 legacy 诊断字符串，仅供过渡期读取；写只 ``kind``）。旧 tape 的 ``data["error_type"]``
值经 ``_LEGACY_ERROR_TYPE_TO_KIND`` 反向映射为 kind（SPEC §4.6）。

依赖单向：本模块依赖 ``orca.exec.error_kinds``（ErrorKind + 表），不依赖 schema/events/run。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orca.exec.error_kinds import (
    ErrorKind,
    _DEFAULT_KIND_FOR_PHASE,
    _LEGACY_ERROR_TYPE_TO_KIND,
)

if TYPE_CHECKING:
    pass


# ADR §4.1.1 phase → 默认 kind 映射（1:1，stream 默认 PROTOCOL_PARSE）。
# 直接复用 error_kinds._DEFAULT_KIND_FOR_PHASE；本模块内不重复定义。


class ExecError(Exception):
    """executor 失败的统一异常（SPEC phase-11 §4.1 / ADR §4.1 决策 1.1）。

    字段集 = ``{kind, message, phase, node, raw}``。**``kind`` 是唯一驱动分类/重试/dispatch
    的字段**；``phase`` / ``node`` / ``raw`` 皆只读诊断（违反即返工）。

    构造器：``ExecError(phase, message, *, kind=None, node=None, raw=None)``。
    ``kind=None`` 时按 ``_DEFAULT_KIND_FOR_PHASE[phase]`` 派生默认（保守默认；classifier
    在边界处可据 raw 精分，如 stream 1:N）。

    executor 捕获后 emit ``node_failed``（给状态机）+ ``error``（给诊断）双事件。
    """

    def __init__(
        self,
        phase: str,
        message: str,
        *,
        kind: ErrorKind | str | None = None,
        node: str | None = None,
        raw: dict | None = None,
    ) -> None:
        self.phase = phase
        self.message = message
        # kind 是唯一分类轴：显式传入优先，否则按 phase 默认表派生（保守默认）。
        # 接受 str（"transport_network"）/ ErrorKind；统一存为 ErrorKind。
        self.kind = _coerce_kind(kind) if kind is not None else _DEFAULT_KIND_FOR_PHASE.get(
            phase, ErrorKind.UNKNOWN
        )
        self.node = node  # 失败 node 名（adapter 注入；None = executor 内部异常未关联 node）
        self.raw = raw    # 原始 payload（backend 返回 / stderr / exception dict）
        super().__init__(f"[{self.phase}] {self.message}")

    # ── 派生属性（迁移期诊断；ADR §4.1.2 / SPEC §4.2 保留 ``phase_to_error_type`` 作诊断）──

    @property
    def error_type(self) -> str:
        """【派生·迁移期诊断】返回 legacy ``error_type`` 字符串。

        - 写路径（emit 事件 data）**只写 ``kind``**，不写 ``error_type``（ADR §4.1.2）。
        - 本属性仅供过渡期读取（如旧测试 / 诊断 log）；新代码读 ``e.kind.value``。
        - 派生规则：
            * 若 ``self.raw`` 含 ``error_type`` 字段（来自 ``from_failed_data`` 透传），
              返回该值（最忠实的 legacy 诊断）；
            * 否则按 ``phase_to_error_type(self.phase)`` 派生。
        """
        if isinstance(self.raw, dict):
            legacy = self.raw.get("error_type")
            if isinstance(legacy, str) and legacy:
                return legacy
        return phase_to_error_type(self.phase)

    @classmethod
    def from_failed_data(cls, err_data: dict, *, node: str | None = None) -> ExecError:
        """从 ``node_failed`` 事件的 data 构造 ExecError（DRY 单点，读兼容期）。

        读兼容期顺序（SPEC §4.3 / ADR §4.1.2）：
          1. ``data.get("kind")`` 优先（新 tape）
          2. 缺失则 ``data.get("error_type")`` 经 ``_LEGACY_ERROR_TYPE_TO_KIND`` 反向映射为 kind
             （旧 tape，1:N 处默认 PROTOCOL_PARSE 并在 raw 注释「legacy」）
          3. 都无 → ``UNKNOWN``（raw 必须保留）

        Args:
            err_data: ``node_failed`` 事件的 data dict。缺字段用合理 default
                （phase="node_failed"，message="executor 产出 node_failed（无消息）"）。
            node: 失败 node 名（adapter / retry loop 注入；None = 未关联）。
        """
        kind_value = err_data.get("kind")
        legacy_error_type = err_data.get("error_type")
        raw = dict(err_data) if err_data else None

        if kind_value:
            kind = _coerce_kind(kind_value)
        elif legacy_error_type:
            # 旧 tape：error_type → kind 反向映射。ClaudeStreamError 1:N 默认 PROTOCOL_PARSE，
            # 在 raw 标注释（SPEC §4.6）。
            kind = _LEGACY_ERROR_TYPE_TO_KIND.get(legacy_error_type, ErrorKind.UNKNOWN)
            if legacy_error_type == "ClaudeStreamError":
                if raw is not None:
                    raw["_legacy_note"] = (
                        "ClaudeStreamError 1:N 不可精确还原；默认 PROTOCOL_PARSE"
                    )
        else:
            kind = ErrorKind.UNKNOWN

        return cls(
            phase=err_data.get("phase", "node_failed"),
            message=err_data.get("message", "executor 产出 node_failed（无消息）"),
            kind=kind,
            node=node,
            raw=raw,
        )


def _coerce_kind(value: "ErrorKind | str") -> ErrorKind:
    """接受 ErrorKind 实例 / 字符串值（如 ``"transport_network"``），统一返回 ErrorKind。

    字符串值若不在枚举内 → ``UNKNOWN``（容错，不 ValueError；kind 漂移是 bug，但 raise
    路径不应崩）。**fail loud 由调用方在边界处显式校验**（classifier 内）。
    """
    if isinstance(value, ErrorKind):
        return value
    try:
        return ErrorKind(value)
    except ValueError:
        return ErrorKind.UNKNOWN


# ── phase → error_type 诊断映射表（保留作诊断映射，不改名；SPEC §4.2 / ADR §4.1.2）────
# 新代码读 ``e.kind``；本表仅供：
#   - 旧测试断言 ``e.error_type`` 的派生读取（``ExecError.error_type`` property）
#   - 诊断 log 输出 legacy 名字
# 不再参与跨层分类（kind 是唯一分类轴）。
_PHASE_TO_ERROR_TYPE: dict[str, str] = {
    "timeout": "ExecTimeout",
    "spawn": "CliExitNonZero",
    "stream": "ClaudeStreamError",
    "result_parse": "NoResultEvent",
    "schema": "SchemaValidationError",
    "render": "RenderError",
    "config": "ConfigError",
    "validator": "validator_failed",
    "interrupted": "Interrupted",
    # phase-11 §4.4 编排层新增 phase（诊断用，phase_to_error_type 容错不 ValueError）：
    "max_iterations": "MaxIterations",
    "route_deadlock": "NoRouteMatch",
    "node_failed": "NodeLifecycleViolation",
}


def phase_to_error_type(phase: str) -> str:
    """phase → legacy ``error_type`` 诊断字符串（ADR §4.1.2 / SPEC §4.2，保留作诊断映射）。

    未知 phase 返 ``"Unknown"``（容错，不 ValueError）——本函数已退为诊断映射，raise 路径
    上有 phase_to_kind 兜底；这里 fail loud 反而让新增 phase 漏补表时 raise 路径崩。
    新代码读 ``e.kind.value``。
    """
    return _PHASE_TO_ERROR_TYPE.get(phase, "Unknown")
