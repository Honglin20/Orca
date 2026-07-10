"""tests/exec/test_result.py —— Result / Error 信封不变量 + 投影 + layer 派生（SPEC §1.1）。

覆盖：
  - ok↔error 互斥（构造器 + __post_init__ validator）
  - Error.from_exec_error 字段投影
  - layer_from_kind 派生（Python 3.10 ``.value.split``）
  - with_hint 跨边界重写（Error 本体不变）
  - UNKNOWN 必须带 raw（铁律 6 fail loud）
"""

from __future__ import annotations

import pytest

from orca.exec.error import ExecError
from orca.exec.error_kinds import ErrorKind
from orca.exec.result import Error, Result


# ── Result 不变量：ok↔error 互斥 ──────────────────────────────────────────────


def test_result_ok_success():
    r = Result.ok_({"x": 1})
    assert r.ok is True
    assert r.data == {"x": 1}
    assert r.error is None


def test_result_err_failure():
    err = Error(kind=ErrorKind.BUSINESS_AGENT, message="x", raw={"detail": "..."})
    r = Result.err(err)
    assert r.ok is False
    assert r.error is err
    assert r.data is None


def test_result_ok_with_error_rejects():
    """ok=True 时 error 非 None → fail loud（__post_init__ validator）。"""
    err = Error(kind=ErrorKind.BUSINESS_AGENT, message="x", raw={"d": 1})
    with pytest.raises(ValueError, match="不能带 error"):
        Result(ok=True, data=None, error=err)


def test_result_err_without_error_rejects():
    """ok=False 时 error=None → fail loud。"""
    with pytest.raises(ValueError, match="必须带 error"):
        Result(ok=False, error=None)


# ── Error.from_exec_error 投影 ────────────────────────────────────────────────


def test_error_from_exec_error_projects_fields():
    """ExecError {kind,message,phase,node,raw} → Error {kind,message,raw,retryable,cause_id}。

    phase / node 不进信封（信封层只关心分类轴 + 诊断 raw）。
    """
    exc = ExecError(
        phase="timeout", message="超时了", node="worker",
        raw={"stderr": "boom"},
    )
    err = Error.from_exec_error(exc, cause_id="seq-42")
    assert err.kind is ErrorKind.TRANSPORT_TIMEOUT
    assert err.message == "超时了"
    assert err.raw == {"stderr": "boom"}
    # TRANSPORT_TIMEOUT 默认 retryable=False（_DEFAULT_RETRYABLE 表）
    assert err.retryable is False
    assert err.cause_id == "seq-42"


def test_error_from_exec_error_rate_limit_retryable_true():
    """RATE_LIMIT 默认 retryable=True（SPEC §2.1）。"""
    exc = ExecError(
        phase="stream", message="限流", kind=ErrorKind.BUSINESS_RATE_LIMIT,
    )
    err = Error.from_exec_error(exc)
    assert err.retryable is True


# ── layer_from_kind 派生 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kind,expected_layer",
    [
        (ErrorKind.TRANSPORT_NETWORK, "transport"),
        (ErrorKind.TRANSPORT_PROCESS, "transport"),
        (ErrorKind.TRANSPORT_TIMEOUT, "transport"),
        (ErrorKind.PROTOCOL_PARSE, "protocol"),
        (ErrorKind.PROTOCOL_MCP, "protocol"),
        (ErrorKind.PROTOCOL_SCHEMA, "protocol"),
        (ErrorKind.BUSINESS_GATE, "business"),
        (ErrorKind.BUSINESS_AGENT, "business"),
        (ErrorKind.BUSINESS_CONFIG, "business"),
        (ErrorKind.BUSINESS_RATE_LIMIT, "business"),
    ],
)
def test_layer_from_kind_derives_correctly(kind, expected_layer):
    """kind 前缀 → layer 派生（ADR §4.1 决策 1.3 / Python 3.10 ``.value.split``）。"""
    err = Error(kind=kind, message="x", raw={"d": 1} if kind is ErrorKind.UNKNOWN else None)
    assert err.layer_from_kind() == expected_layer


def test_layer_from_kind_unknown():
    err = Error(kind=ErrorKind.UNKNOWN, message="x", raw={"d": 1})
    assert err.layer_from_kind() == "unknown"


# ── with_hint 跨边界重写 ─────────────────────────────────────────────────────


def test_with_hint_rewrites_only_hint():
    """with_hint 重写 _hint，Error 本体不变（DRY + ADR §4.1 跨边界 _hint 重写）。"""
    err = Error(kind=ErrorKind.BUSINESS_CONFIG, message="missing setup_outputs")
    r1 = Result.err(err, hint="for run layer")
    r2 = r1.with_hint("for MCP shell: call describe_workflow first")
    assert r2._hint == "for MCP shell: call describe_workflow first"
    # Error 本体不变
    assert r2.error is err
    # 原 Result 不变（frozen）
    assert r1._hint == "for run layer"


def test_with_hint_does_not_mutate_original():
    err = Error(kind=ErrorKind.BUSINESS_AGENT, message="x")
    r1 = Result.err(err)
    r2 = r1.with_hint("new hint")
    assert r1._hint is None
    assert r2._hint == "new hint"


# ── UNKNOWN 必须带 raw（铁律 6 fail loud）────────────────────────────────────


def test_unknown_without_raw_fails_loud():
    """UNKNOWN 错误 raw=None → __post_init__ 抛 ValueError（SPEC §0.1 铁律 6）。"""
    with pytest.raises(ValueError, match="UNKNOWN 错误必须保留 raw"):
        Error(kind=ErrorKind.UNKNOWN, message="?", raw=None)


def test_unknown_with_raw_ok():
    err = Error(kind=ErrorKind.UNKNOWN, message="?", raw={"detail": "unknown backend"})
    assert err.kind is ErrorKind.UNKNOWN
    assert err.raw == {"detail": "unknown backend"}


# ── Error frozen ─────────────────────────────────────────────────────────────


def test_error_is_frozen():
    """Error 是 frozen dataclass（不可变，SPEC §1.1）。"""
    err = Error(kind=ErrorKind.BUSINESS_AGENT, message="x")
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        err.message = "y"  # type: ignore[misc]
