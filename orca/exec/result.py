"""result.py —— Result / Error 跨壳信封（phase-11 SPEC §1.1 / ADR §4.1 信封层）。

回答「跨壳返回（MCP tool / Web API / exec→run / run→iface）怎么统一表达？」：
``Result{ok, data?, error?, _hint?}``。

Error dataclass 是 ExecError 的**信封投影**（ADR §4.1 决策 1.1 / §9.1）：
  - 投影规则：``Error.from_exec_error(exc)`` 纯函数，补齐 ``retryable``（查 kind 默认策略表）
    + ``cause_id``（reducer 注入）
  - 字段集：``{kind, message, raw, retryable, cause_id}``（**无 layer**，ADR v2 删，决策 1.3）
  - ``layer_from_kind()`` 派生方法（kind 前缀隐含 layer）

**``_hint`` 跨边界重写**（ADR §4.1 / SPEC §1.2）：同一 Error 穿过多个边界时，每个边界
用 ``result.with_hint(new_hint)`` 重写 ``_hint``（每个面向当层消费者）。``Error`` 本体不变。

依赖单向：本模块依赖 ``orca.exec.error_kinds``（ErrorKind + 表）+ ``orca.exec.error``
（ExecError，TYPE_CHECKING only）；不依赖 schema/events/run/iface。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Optional

from orca.exec.error_kinds import (
    ErrorKind,
    _DEFAULT_RETRYABLE,
    _KIND_LAYER_PREFIX,
)

if TYPE_CHECKING:
    from orca.exec.error import ExecError


@dataclass(frozen=True)
class Error:
    """跨壳错误信封（ADR §4.1 信封层投影）。

    不变量：
      - ``kind`` 必填（唯一分类轴）
      - ``UNKNOWN`` 必须带 ``raw``（铁律 6 fail loud，``__post_init__`` validator）
      - ``ok ↔ error`` 互斥由 ``Result`` 构造器守（``Result.err`` 强制 ``error is not None``）

    v2 删 ``layer`` 字段（ADR §4.1 决策 1.3）：kind 前缀隐含 layer，``layer_from_kind()``
    派生。``retryable`` 保留（显式覆盖 kind 默认策略有合理用例）。
    """

    kind: ErrorKind
    message: str
    raw: Optional[dict] = None
    retryable: Optional[bool] = None      # None = 按 kind 默认策略；显式覆盖
    cause_id: Optional[str] = None        # 关联 tape 事件 seq，reducer 注入

    def __post_init__(self) -> None:
        """fail loud：UNKNOWN 必须带 raw（铁律 6）。"""
        if self.kind == ErrorKind.UNKNOWN and self.raw is None:
            raise ValueError(
                "UNKNOWN 错误必须保留 raw payload（铁律 6 fail loud）"
            )

    @classmethod
    def from_exec_error(
        cls, exc: "ExecError", *, cause_id: Optional[str] = None
    ) -> "Error":
        """ExecError 的信封投影（ADR §4.1 决策 1.1）。

        补齐 retryable（查 kind 默认策略表）+ cause_id（reducer 注入）。
        字段映射：ExecError {kind, message, phase, node, raw}
                  → Error {kind, message, raw, retryable, cause_id}。
        phase / node 不进信封（信封层只关心分类轴 + 诊断 raw）。
        """
        return cls(
            kind=exc.kind,
            message=exc.message,
            raw=exc.raw,
            retryable=_DEFAULT_RETRYABLE.get(exc.kind, False),
            cause_id=cause_id,
        )

    def layer_from_kind(self) -> Literal["transport", "protocol", "business", "unknown"]:
        """layer 是 kind 的派生投影（ADR §4.1 决策 1.3，v2 删 layer 字段）。

        用 ``.value.split``（Python 3.10 兼容；``str(ErrorKind.X)`` 返 ``"ErrorKind.X"``
        会切错）。取枚举值字符串的第一个下划线前段，查 ``_KIND_LAYER_PREFIX``。
        """
        return _KIND_LAYER_PREFIX.get(  # type: ignore[return-value]
            self.kind.value.split("_")[0], "unknown"
        )


@dataclass(frozen=True)
class Result:
    """跨壳返回信封（SPEC §1.1）。

    不变量：``ok=True`` 时 ``error=None``；``ok=False`` 时 ``error != None`` 且 ``data=None``。
    由 ``ok_`` / ``err`` 工厂守，``__post_init__`` validator 兜底。
    """

    ok: bool
    data: Optional[Any] = None
    error: Optional[Error] = None
    _hint: Optional[str] = None

    def __post_init__(self) -> None:
        """守 ok↔error 互斥不变量（fail loud）。"""
        if self.ok and self.error is not None:
            raise ValueError("Result(ok=True) 不能带 error")
        if not self.ok and self.error is None:
            raise ValueError("Result(ok=False) 必须带 error")

    @classmethod
    def ok_(cls, data: Any, hint: Optional[str] = None) -> "Result":
        """成功工厂（``ok_`` 避开内建 ``bool.ok`` 命名冲突）。"""
        return cls(ok=True, data=data, _hint=hint)

    @classmethod
    def err(cls, error: Error, hint: Optional[str] = None) -> "Result":
        """失败工厂。"""
        return cls(ok=False, error=error, _hint=hint)

    def with_hint(self, hint: Optional[str]) -> "Result":
        """跨边界重写 ``_hint`` 的唯一 API（ADR §4.1 / SPEC §1.2）。

        Error 本体跨层不变，每个边界用本方法重写 _hint。消费方不许各自 dataclasses.replace
        / 重建（DRY + 接口统一性）。
        """
        return replace(self, _hint=hint)
