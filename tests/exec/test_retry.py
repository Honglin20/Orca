"""tests/exec/test_retry.py —— 三层重试边界 + 退避算法 + retry_started emit（SPEC §3）。

聚焦新 ``orca/exec/retry.py``（三层抽象 + emit 帮手）。节点级 transient retry 由
``tests/run/test_retry.py`` 覆盖（business 层）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from orca.exec.error_kinds import ErrorKind
from orca.exec.result import Error
from orca.exec.retry import (
    RetryPolicy,
    compute_backoff_delay,
    emit_retry_exhausted,
    emit_retry_started,
    is_retryable,
    layer_for_kind,
    policy_for_kind,
)


# ── compute_backoff_delay ────────────────────────────────────────────────────


def test_backoff_constant():
    p = RetryPolicy(backoff="constant", initial_delay_seconds=2.0, jitter=False)
    assert compute_backoff_delay(p, 1) == 2.0
    assert compute_backoff_delay(p, 5) == 2.0


def test_backoff_linear():
    p = RetryPolicy(backoff="linear", initial_delay_seconds=1.0, jitter=False)
    assert compute_backoff_delay(p, 1) == 1.0
    assert compute_backoff_delay(p, 2) == 2.0
    assert compute_backoff_delay(p, 3) == 3.0


def test_backoff_exponential():
    p = RetryPolicy(backoff="exponential", initial_delay_seconds=1.0, jitter=False)
    assert compute_backoff_delay(p, 1) == 1.0
    assert compute_backoff_delay(p, 2) == 2.0
    assert compute_backoff_delay(p, 3) == 4.0
    assert compute_backoff_delay(p, 4) == 8.0


def test_backoff_caps_at_max_delay():
    """exponential 爆炸时被 max_delay_seconds 截断。"""
    p = RetryPolicy(
        backoff="exponential",
        initial_delay_seconds=10.0,
        max_delay_seconds=60.0,
        jitter=False,
    )
    # attempt=4 → 10×2^3 = 80，截到 60
    assert compute_backoff_delay(p, 4) == 60.0


def test_backoff_jitter_within_range():
    """jitter=True 时 delay ∈ [base×0.8, base×1.2]（SPEC §9.5.6 ±20%）。"""
    p = RetryPolicy(
        backoff="constant", initial_delay_seconds=10.0,
        max_delay_seconds=100.0, jitter=True,
    )
    for _ in range(20):
        delay = compute_backoff_delay(p, 1)
        assert 8.0 <= delay <= 12.0


# ── policy_for_kind（SPEC §2.1）──────────────────────────────────────────────


@pytest.mark.parametrize(
    "kind,max_attempts",
    [
        (ErrorKind.TRANSPORT_NETWORK, 3),
        (ErrorKind.TRANSPORT_PROCESS, 2),
        (ErrorKind.TRANSPORT_TIMEOUT, 0),
        (ErrorKind.PROTOCOL_PARSE, 0),
        (ErrorKind.PROTOCOL_SCHEMA, 0),
        (ErrorKind.BUSINESS_AGENT, 0),
        (ErrorKind.BUSINESS_RATE_LIMIT, 5),
        (ErrorKind.UNKNOWN, 0),
    ],
)
def test_policy_for_kind_max_attempts(kind, max_attempts):
    """SPEC §2.1：kind → 默认 max_attempts 表。"""
    assert policy_for_kind(kind).max_attempts == max_attempts


def test_rate_limit_policy_has_long_backoff():
    """SPEC §2.1：BUSINESS_RATE_LIMIT 退避 5s/10s/20s/40s/80s（initial=5）。"""
    p = policy_for_kind(ErrorKind.BUSINESS_RATE_LIMIT)
    assert p.initial_delay_seconds == 5.0
    assert p.max_attempts == 5


# ── is_retryable + retry_on 解耦（SPEC §4.5）─────────────────────────────────


def test_is_retryable_uses_kind_default():
    """未显式覆盖 retryable → 用 _DEFAULT_RETRYABLE[kind]。"""
    err = Error(kind=ErrorKind.TRANSPORT_NETWORK, message="x")
    assert is_retryable(err) is True

    err = Error(kind=ErrorKind.BUSINESS_CONFIG, message="x")
    assert is_retryable(err) is False


def test_is_retryable_respects_explicit_retryable():
    """error.retryable 显式覆盖优先于默认。"""
    err = Error(
        kind=ErrorKind.BUSINESS_CONFIG, message="x", retryable=True,
    )
    assert is_retryable(err) is True


def test_is_retryable_retry_on_overrides_default():
    """SPEC §4.5：retry_on 命中强制 retryable=True（用户 yaml 不 breaking）。

    例：TRANSPORT_TIMEOUT 默认 retryable=False；用户 retry_on=[timeout] 命中 → 强制 True。
    """
    err = Error(
        kind=ErrorKind.TRANSPORT_TIMEOUT, message="x", retryable=False,
    )
    assert is_retryable(err, retry_on_keys=("timeout",)) is True


@pytest.mark.parametrize(
    "retry_on,kind,expected",
    [
        (("spawn_error",), ErrorKind.TRANSPORT_PROCESS, True),
        (("timeout",), ErrorKind.TRANSPORT_TIMEOUT, True),
        (("api_error",), ErrorKind.BUSINESS_AGENT, True),
        (("http_429",), ErrorKind.BUSINESS_RATE_LIMIT, True),
        (("validator_failed",), ErrorKind.PROTOCOL_SCHEMA, True),
        # 不命中：
        (("timeout",), ErrorKind.BUSINESS_CONFIG, False),
        (("spawn_error",), ErrorKind.BUSINESS_AGENT, False),
    ],
)
def test_retry_on_matches_kind(retry_on, kind, expected):
    """SPEC §4.5 retry_on → kind 子集映射（5 Literal 全覆盖）。"""
    err = Error(kind=kind, message="x", raw={"d": 1})
    assert is_retryable(err, retry_on_keys=retry_on) is expected


# ── layer_for_kind 派生 ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kind,expected_layer",
    [
        (ErrorKind.TRANSPORT_NETWORK, "transport"),
        (ErrorKind.PROTOCOL_PARSE, "protocol"),
        (ErrorKind.BUSINESS_AGENT, "business"),
        (ErrorKind.UNKNOWN, "business"),  # 默认 business（旧 tape 兼容）
    ],
)
def test_layer_for_kind(kind, expected_layer):
    assert layer_for_kind(kind) == expected_layer


# ── emit_retry_started / emit_retry_exhausted ────────────────────────────────


class _CaptureSink:
    """duck typing Protocol 满足的 bus 替身（捕获 emit 调用）。"""

    def __init__(self):
        self.events: list[tuple[str, dict, str | None]] = []

    async def emit(self, type, data=None, *, node=None, session_id=None):
        self.events.append((type, dict(data or {}), node))


def test_emit_retry_started_writes_layer_kind_reason_next_retry_at():
    """ADR §4.5：retry_started.data 带 layer/reason/kind/next_retry_at（合并决策）。"""
    sink = _CaptureSink()
    err = Error(kind=ErrorKind.TRANSPORT_NETWORK, message="TCP reset")
    asyncio.run(emit_retry_started(
        sink,
        node="worker",
        attempt=2,
        max_attempts=3,
        error=err,
        delay_seconds=2.0,
        reason="connection refused",
    ))

    assert len(sink.events) == 1
    type_, data, node = sink.events[0]
    assert type_ == "retry_started"
    assert node == "worker"
    assert data["attempt"] == 2
    assert data["max_attempts"] == 3
    assert data["kind"] == "transport_network"
    assert data["error_type"] == "transport_network"  # 读兼容期（同 kind 值）
    assert data["layer"] == "transport"
    assert data["reason"] == "connection refused"
    assert data["delay_seconds"] == 2.0
    # next_retry_at 是 ISO 时间戳
    assert isinstance(data["next_retry_at"], str)
    assert "T" in data["next_retry_at"]


def test_emit_retry_started_next_retry_at_none_when_no_delay():
    """delay_seconds=0 → next_retry_at=None（不重试场景）。"""
    sink = _CaptureSink()
    err = Error(kind=ErrorKind.UNKNOWN, message="x", raw={"d": 1})
    asyncio.run(emit_retry_started(
        sink, node="n", attempt=1, max_attempts=0,
        error=err, delay_seconds=0.0,
    ))
    assert sink.events[0][1]["next_retry_at"] is None


def test_emit_retry_started_with_bus_none_no_crash():
    """bus=None → 不 emit，不崩（执行层无 bus 场景的容错）。"""
    err = Error(kind=ErrorKind.TRANSPORT_NETWORK, message="x")
    asyncio.run(emit_retry_started(
        None, node="n", attempt=1, max_attempts=3,
        error=err, delay_seconds=1.0,
    ))


def test_emit_retry_exhausted_writes_layer_last_kind():
    """ADR §4.5：retry_exhausted 同步加 layer。"""
    sink = _CaptureSink()
    err = Error(kind=ErrorKind.PROTOCOL_PARSE, message="parse failed")
    asyncio.run(emit_retry_exhausted(
        sink, node="n", attempts=3, error=err,
    ))
    assert len(sink.events) == 1
    type_, data, _ = sink.events[0]
    assert type_ == "retry_exhausted"
    assert data["last_kind"] == "protocol_parse"
    assert data["last_error_type"] == "protocol_parse"
    assert data["layer"] == "protocol"
    assert data["attempts"] == 3


# ── RetryPolicy frozen ───────────────────────────────────────────────────────


def test_retry_policy_is_frozen():
    p = RetryPolicy()
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        p.max_attempts = 99  # type: ignore[misc]


# ── execute_with_transport_retry（SPEC §5.2 三层不吞错 E2E 核心）─────────────


from orca.exec.retry import execute_with_transport_retry  # noqa: E402


def test_transport_retry_succeeds_first_attempt():
    """SPEC §3：首次成功 → 不重试，不 emit retry_started。"""
    call_count = [0]

    async def fetch():
        call_count[0] += 1
        return "ok"

    err = Error(kind=ErrorKind.TRANSPORT_NETWORK, message="x")
    result = asyncio.run(execute_with_transport_retry(fetch, bus=None, node="n", error=err))
    assert result == "ok"
    assert call_count[0] == 1


def test_transport_retry_exhausts_and_reraises():
    """SPEC §5.2：连续 3 次 ConnectionError → 重试 3 次 → 耗尽 re-raise 原始异常。

    INTENT（铁律 4 不互相吞错）：transport 重试耗尽后 kind 不变，原异常上抛。
    """
    call_count = [0]

    async def fetch():
        call_count[0] += 1
        raise ConnectionError(f"TCP reset attempt {call_count[0]}")

    err = Error(kind=ErrorKind.TRANSPORT_NETWORK, message="initial")
    with pytest.raises(ConnectionError, match="TCP reset attempt 3"):
        asyncio.run(execute_with_transport_retry(fetch, bus=None, node="n", error=err))
    # TRANSPORT_NETWORK 默认 max_attempts=3
    assert call_count[0] == 3


def test_transport_retry_no_attempts_for_non_retryable():
    """SPEC §2.1：max_attempts=0 的 kind（如 UNKNOWN）不重试，首次失败立即 re-raise。"""
    call_count = [0]

    async def fetch():
        call_count[0] += 1
        raise OSError("disk full")

    err = Error(kind=ErrorKind.UNKNOWN, message="x", raw={"d": 1})
    with pytest.raises(OSError):
        asyncio.run(execute_with_transport_retry(fetch, bus=None, node="n", error=err))
    assert call_count[0] == 1


def test_transport_retry_emits_retry_started_with_layer():
    """SPEC §3 + ADR §4.5：每次重试 emit retry_started{layer: transport}。"""
    sink = _CaptureSink()
    attempts = [0]

    async def fetch():
        attempts[0] += 1
        if attempts[0] < 3:
            raise ConnectionError(f"reset {attempts[0]}")
        return "ok"

    err = Error(kind=ErrorKind.TRANSPORT_NETWORK, message="x")
    result = asyncio.run(execute_with_transport_retry(
        fetch, bus=sink, node="worker", error=err,
    ))
    assert result == "ok"
    # 2 次 retry_started（attempt 1→2, 2→3）
    retry_started = [e for e in sink.events if e[0] == "retry_started"]
    assert len(retry_started) == 2
    for type_, data, node in retry_started:
        assert data["layer"] == "transport"
        assert data["kind"] == "transport_network"
        assert node == "worker"


def test_transport_retry_recovers_after_one_failure():
    """SPEC §3：首次失败，第二次成功 → 不 emit retry_exhausted。"""
    sink = _CaptureSink()
    attempts = [0]

    async def fetch():
        attempts[0] += 1
        if attempts[0] == 1:
            raise ConnectionError("transient")
        return "recovered"

    err = Error(kind=ErrorKind.TRANSPORT_NETWORK, message="x")
    result = asyncio.run(execute_with_transport_retry(
        fetch, bus=sink, node="n", error=err,
    ))
    assert result == "recovered"
    assert any(e[0] == "retry_started" for e in sink.events)
    assert not any(e[0] == "retry_exhausted" for e in sink.events)
