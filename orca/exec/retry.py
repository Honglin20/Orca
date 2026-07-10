"""retry.py —— 三层重试边界 + 退避算法（phase-11 SPEC §3 / ADR §4.1）。

回答「哪一层重试什么错？谁不能吞谁？」：transport / protocol / business 三层各管各的
失败域，**下层重试耗尽把 kind 原样上抛**（不重新分类，铁律 4）。

  ┌─ business 层（run/orchestrator.py）── 触发 BUSINESS_AGENT/GATE 且 retryable=True ─┐
  │  决策：用户可见，emit retry_started(layer=business)                                │
  ├─ protocol 层（exec/translator）────── 触发 PROTOCOL_PARSE/SCHEMA 且 retryable=True ─┤
  │  决策：默认不重试（协议漂移需人工）                                                 │
  ├─ transport 层（exec/runner.py）────── 触发 TRANSPORT_* 默认策略自动重试            │
  │  决策：按默认策略，emit retry_started(layer=transport)                              │
  └────────────────────────────────────────────────────────────────────────────────────┘

每层重试耗尽 → 上抛 Error（kind 不变）；上层看到已分类的 kind，不再重试。

**retry_started.data 扩展**（ADR §4.5 合并决策，不新增 RetryAttempted EventType）：
emit 时带 ``layer`` / ``reason`` / ``next_retry_at`` / ``kind`` 字段。

本模块与 ``orca/run/retry.py`` 的边界：run/retry.py 是节点级 transient 失败重试 primitive
（按 ``RetryPolicy.retry_on`` 白名单 + backoff），属 business 层；本模块提供：
  - ``RetryPolicy`` 数据类（kind 维度的重试策略，与 RetryPolicy.retry_on 解耦）
  - ``compute_backoff_delay`` 退避算法（DRY：transport/business 共用）
  - ``emit_retry_started`` / ``emit_retry_exhausted`` 统一 emit 帮手（带 layer/kind/reason）

依赖单向：本模块依赖 ``orca.exec.error_kinds`` + ``orca.exec.result``（Error）；
事件总线经 ``_RetryEventSink`` Protocol 注入（duck typing，避免硬依赖 events.bus）。
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, Protocol

from orca.exec.error_kinds import ErrorKind, _DEFAULT_RETRYABLE
from orca.exec.result import Error

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# SPEC §2.1 退避策略默认参数（kind 维度策略）。
_JITTER_FRACTION = 0.2  # ±20%

Layer = Literal["transport", "protocol", "business"]


class _RetryEventSink(Protocol):
    """exec/retry 写事件流的狭窄能力（duck typing，避免 exec/ 硬依赖 events 包）。

    事件总线（events.bus）结构化满足此 Protocol（emit 方法签名相容）；
    本 Protocol 只暴露 ``emit(type, data, node=None)`` 这一狭窄能力。
    """

    async def emit(
        self,
        type: str,
        data: dict | None = None,
        *,
        node: str | None = None,
        session_id: str | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class RetryPolicy:
    """kind 维度的重试策略（SPEC §2.1，与 ``schema.RetryPolicy.retry_on`` 解耦）。

    ``schema.RetryPolicy.retry_on`` 是用户 yaml 字面量（spawn_error / timeout / ...），
    命中后**强制** ``retryable=True`` 覆盖 ``_DEFAULT_RETRYABLE``（SPEC §4.5）。
    本 ``RetryPolicy`` 是策略表（按 kind 查），两者正交。
    """

    max_attempts: int = 3
    backoff: Literal["constant", "linear", "exponential"] = "exponential"
    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    jitter: bool = True


# 按 kind 的默认策略（SPEC §2.1 max_attempts + 退避）。
_KIND_RETRY_POLICY: dict[ErrorKind, RetryPolicy] = {
    ErrorKind.TRANSPORT_NETWORK: RetryPolicy(
        max_attempts=3, backoff="exponential", initial_delay_seconds=1.0,
    ),
    ErrorKind.TRANSPORT_PROCESS: RetryPolicy(
        max_attempts=2, backoff="constant", initial_delay_seconds=1.0,
    ),
    ErrorKind.TRANSPORT_TIMEOUT: RetryPolicy(max_attempts=0),
    ErrorKind.PROTOCOL_PARSE: RetryPolicy(max_attempts=0),
    ErrorKind.PROTOCOL_MCP: RetryPolicy(max_attempts=0),
    ErrorKind.PROTOCOL_SCHEMA: RetryPolicy(max_attempts=0),
    ErrorKind.BUSINESS_GATE: RetryPolicy(max_attempts=0),
    ErrorKind.BUSINESS_AGENT: RetryPolicy(max_attempts=0),
    ErrorKind.BUSINESS_CONFIG: RetryPolicy(max_attempts=0),
    ErrorKind.BUSINESS_RATE_LIMIT: RetryPolicy(
        max_attempts=5, backoff="exponential", initial_delay_seconds=5.0,
        max_delay_seconds=120.0,
    ),
    ErrorKind.UNKNOWN: RetryPolicy(max_attempts=0),
}


def policy_for_kind(kind: ErrorKind) -> RetryPolicy:
    """按 kind 取默认 RetryPolicy（SPEC §2.1）。"""
    return _KIND_RETRY_POLICY.get(kind, RetryPolicy(max_attempts=0))


def is_retryable(
    error: Error, *, retry_on_keys: "tuple[str, ...] | None" = None
) -> bool:
    """判断错误是否可重试（SPEC §4.5 retry_on 解耦）。

    - ``error.retryable`` 显式非 None → 用之（profile / classifier 显式覆盖）
    - 否则查 ``_DEFAULT_RETRYABLE[kind]``
    - ``retry_on_keys`` 命中（如用户 yaml ``retry_on: [timeout]``）→ 强制 True，
      覆盖默认（SPEC §4.5）
    """
    if retry_on_keys and _kind_matches_retry_on(error.kind, retry_on_keys):
        return True
    if error.retryable is not None:
        return error.retryable
    return _DEFAULT_RETRYABLE.get(error.kind, False)


# SPEC §4.5 retry_on 字面量 → kind 子集映射（命中即强制 retryable=True）。
_RETRY_ON_TO_KINDS: dict[str, set[ErrorKind]] = {
    "spawn_error": {ErrorKind.TRANSPORT_PROCESS},
    "timeout": {ErrorKind.TRANSPORT_TIMEOUT},
    "api_error": {ErrorKind.BUSINESS_AGENT},
    "http_429": {ErrorKind.BUSINESS_RATE_LIMIT},
    "validator_failed": {ErrorKind.PROTOCOL_SCHEMA},
}


def _kind_matches_retry_on(kind: ErrorKind, retry_on_keys: "tuple[str, ...]") -> bool:
    """retry_on 是否命中 kind。"""
    for key in retry_on_keys:
        kinds = _RETRY_ON_TO_KINDS.get(key)
        if kinds and kind in kinds:
            return True
    return False


def compute_backoff_delay(
    policy: RetryPolicy, attempt: int
) -> float:
    """退避算法（SPEC §3 / DRY，transport+business 共用）。

    Args:
        policy: RetryPolicy（含 backoff / initial_delay / max_delay / jitter）。
        attempt: 当前 attempt 编号（1-based）。

    Returns:
        delay 秒数（float）。已应用 max_delay_seconds 上限 + 可选 ±20% jitter。
    """
    if policy.backoff == "constant":
        base = policy.initial_delay_seconds
    elif policy.backoff == "linear":
        base = policy.initial_delay_seconds * attempt
    else:  # "exponential"
        base = policy.initial_delay_seconds * (2 ** (attempt - 1))
    base = min(base, policy.max_delay_seconds)
    if policy.jitter:
        factor = 1.0 + random.uniform(-_JITTER_FRACTION, _JITTER_FRACTION)
        return base * factor
    return base


def layer_for_kind(kind: ErrorKind) -> Layer:
    """kind → layer 派生（重试 emit 用）。"""
    prefix = kind.value.split("_")[0]
    if prefix == "transport":
        return "transport"
    if prefix == "protocol":
        return "protocol"
    if prefix == "business":
        return "business"
    return "business"  # unknown 默认 business（旧 tape 兼容）


async def emit_retry_started(
    bus: "_RetryEventSink | None",
    *,
    node: str,
    attempt: int,
    max_attempts: int,
    error: Error,
    delay_seconds: float,
    layer: Layer | None = None,
    reason: str = "",
) -> None:
    """emit ``retry_started``（带 layer/reason/next_retry_at/kind，ADR §4.5 合并决策）。

    next_retry_at: ISO 时间戳（now + delay_seconds）；不重试时 None。
    """
    next_retry_at: str | None
    if delay_seconds > 0:
        next_retry_dt = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        next_retry_at = next_retry_dt.isoformat()
    else:
        next_retry_at = None

    if bus is None:
        return
    await bus.emit(
        "retry_started",
        {
            "attempt": attempt,
            "max_attempts": max_attempts,
            "error_type": error.kind.value,   # 旧字段：保留写兼容（消费方读 kind 优先）
            "kind": error.kind.value,         # 新字段：唯一分类权威
            "delay_seconds": delay_seconds,
            "node": node,
            "layer": layer or layer_for_kind(error.kind),
            "reason": reason or error.message,
            "next_retry_at": next_retry_at,
        },
        node=node,
    )


async def emit_retry_exhausted(
    bus: "_RetryEventSink | None",
    *,
    node: str,
    attempts: int,
    error: Error,
    layer: Layer | None = None,
) -> None:
    """emit ``retry_exhausted``（同步加 layer，ADR §4.5）。"""
    if bus is None:
        return
    await bus.emit(
        "retry_exhausted",
        {
            "attempts": attempts,
            "last_error_type": error.kind.value,
            "last_kind": error.kind.value,
            "node": node,
            "layer": layer or layer_for_kind(error.kind),
        },
        node=node,
    )


async def execute_with_transport_retry(
    fetch: "Any",
    *,
    bus: "_RetryEventSink | None",
    node: str,
    error: Error,
) -> "Any":
    """transport 层重试 primitive（SPEC §3）。

    仅供 ``exec/runner.py`` 等底层 transport 调用方使用；不在本模块完整实现（避免
    与 ``run/retry.py`` 节点级重试耦合）。此处保留 API 占位，待 phase-11 process-lifecycle
    完整落地时回填。

    当前实现：仅按 ``_KIND_RETRY_POLICY`` 决定是否重试，重试耗尽 re-raise 原始异常。
    """
    policy = policy_for_kind(error.kind)
    if policy.max_attempts <= 0:
        return await fetch()
    last_exc: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await fetch()
        except Exception as e:  # noqa: BLE001 - transport 重试耗尽后上层处理
            last_exc = e
            if attempt >= policy.max_attempts:
                break
            delay = compute_backoff_delay(policy, attempt)
            if bus is not None:
                await emit_retry_started(
                    bus,
                    node=node,
                    attempt=attempt + 1,
                    max_attempts=policy.max_attempts,
                    error=error,
                    delay_seconds=delay,
                    layer="transport",
                    reason=str(e),
                )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
