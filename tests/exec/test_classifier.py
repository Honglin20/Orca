"""tests/exec/test_classifier.py —— classify_exception + classify_backend_output 双入口规则表。

覆盖 SPEC §2.2 完整规则表（17 行 first-match-wins）+ profile 钩子调度顺序 + UNKNOWN 兜底。
"""

from __future__ import annotations

import pytest

from orca.exec.classifier import classify_backend_output, classify_exception
from orca.exec.error import ExecError
from orca.exec.error_kinds import ErrorKind
from orca.exec.result import Error


# ── classify_exception ───────────────────────────────────────────────────────


def test_classify_exception_exec_error_pass_through():
    """ExecError 直接投影（kind 不重新分类，ADR §4.1 决策 1.4）。"""
    exc = ExecError(phase="timeout", message="x")
    err = classify_exception(exc)
    assert err.kind is ErrorKind.TRANSPORT_TIMEOUT
    assert err.message == "x"


def test_classify_exception_timeout():
    err = classify_exception(TimeoutError("stalled"))
    assert err.kind is ErrorKind.TRANSPORT_TIMEOUT


def test_classify_exception_broken_pipe_to_protocol_mcp():
    """SPEC §2.2 行 10：BrokenPipeError → PROTOCOL_MCP。"""
    err = classify_exception(BrokenPipeError("client gone"))
    assert err.kind is ErrorKind.PROTOCOL_MCP


def test_classify_exception_connection_error_to_transport_network():
    """SPEC §2.2 行 4：ConnectionError → TRANSPORT_NETWORK。"""
    err = classify_exception(ConnectionError("refused"))
    assert err.kind is ErrorKind.TRANSPORT_NETWORK


def test_classify_exception_os_error_to_transport_network():
    """OSError 子类（非 BrokenPipe / Connection）→ TRANSPORT_NETWORK。"""
    err = classify_exception(OSError("disk full"))
    assert err.kind is ErrorKind.TRANSPORT_NETWORK


def test_classify_exception_unknown_preserves_raw():
    """未识别 exception → UNKNOWN（raw 完整保留，铁律 6）。"""
    class _CustomErr(Exception):
        pass
    err = classify_exception(_CustomErr("weird"))
    assert err.kind is ErrorKind.UNKNOWN
    assert err.raw is not None
    assert err.raw["exception"] == "_CustomErr"


def test_classify_exception_does_not_reclassify_exec_error_subclass():
    """WorkflowAborted 是 ExecError 子类（kind=BUSINESS_GATE）；classifier 透传不重分类。"""
    from orca.run.errors import WorkflowAborted
    exc = WorkflowAborted(node="worker")
    err = classify_exception(exc)
    assert err.kind is ErrorKind.BUSINESS_GATE


def test_classify_exception_route_error_subclass():
    """RouteError 是 ExecError 子类（kind=BUSINESS_CONFIG）；classifier 透传。"""
    from orca.run.router import RouteError
    exc = RouteError("no match", node="decide", output=None)
    err = classify_exception(exc)
    assert err.kind is ErrorKind.BUSINESS_CONFIG


# ── classify_backend_output 规则表（SPEC §2.2 行 1-17）──────────────────────────


def test_backend_oom_exit_137_transport_process_retryable_true():
    """行 1：exit_code=137（OOM/SIGKILL）→ TRANSPORT_PROCESS + retryable=True 显式覆盖。"""
    err = classify_backend_output({"exit_code": 137, "stderr": "killed"})
    assert err.kind is ErrorKind.TRANSPORT_PROCESS
    assert err.retryable is True  # OOM 显式覆盖


def test_backend_oom_exit_139_transport_process_retryable_true():
    """行 1：exit_code=139（SIGSEGV）→ TRANSPORT_PROCESS + retryable=True。"""
    err = classify_backend_output({"exit_code": 139})
    assert err.kind is ErrorKind.TRANSPORT_PROCESS
    assert err.retryable is True


def test_backend_non_zero_non_one_exit_business_agent():
    """行 2：exit_code=42（非 0 非 1）→ BUSINESS_AGENT。"""
    err = classify_backend_output({"exit_code": 42, "stderr": "boom"})
    assert err.kind is ErrorKind.BUSINESS_AGENT


def test_backend_http_429_business_rate_limit_retryable_true():
    """行 12：http_status=429 → BUSINESS_RATE_LIMIT + retryable=True。"""
    err = classify_backend_output({
        "http_status": 429, "message": "Too Many Requests",
    })
    assert err.kind is ErrorKind.BUSINESS_RATE_LIMIT
    assert err.retryable is True


def test_backend_is_error_business_agent():
    """行 6：is_error=true → BUSINESS_AGENT（stream agent 失败）。"""
    err = classify_backend_output({"is_error": True, "message": "API error"})
    assert err.kind is ErrorKind.BUSINESS_AGENT


def test_backend_tool_use_id_business_agent():
    """行 6：tool_use_id 存在 → BUSINESS_AGENT（agent 工具调用失败）。"""
    err = classify_backend_output({
        "tool_use_id": "toolu_01", "message": "tool threw",
    })
    assert err.kind is ErrorKind.BUSINESS_AGENT


def test_backend_phase_stream_no_is_error_protocol_parse():
    """行 7：phase=stream + 无 is_error + 无 tool_use_id → PROTOCOL_PARSE（first-match-wins）。"""
    err = classify_backend_output({
        "phase": "stream", "message": "parse failed",
    })
    assert err.kind is ErrorKind.PROTOCOL_PARSE


def test_backend_phase_schema_protocol_schema():
    """行 8：phase=schema → PROTOCOL_SCHEMA。"""
    err = classify_backend_output({"phase": "schema", "message": "schema invalid"})
    assert err.kind is ErrorKind.PROTOCOL_SCHEMA


def test_backend_phase_validator_protocol_schema():
    """行 9：phase=validator → PROTOCOL_SCHEMA（v2：validator 归此）。"""
    err = classify_backend_output({"phase": "validator", "message": "criteria failed"})
    assert err.kind is ErrorKind.PROTOCOL_SCHEMA


def test_backend_phase_config_business_config():
    """行 13：phase=config → BUSINESS_CONFIG。"""
    err = classify_backend_output({"phase": "config", "message": "bad yaml"})
    assert err.kind is ErrorKind.BUSINESS_CONFIG


def test_backend_phase_render_business_config():
    err = classify_backend_output({"phase": "render", "message": "Jinja2 err"})
    assert err.kind is ErrorKind.BUSINESS_CONFIG


def test_backend_phase_interrupted_business_gate():
    err = classify_backend_output({"phase": "interrupted", "message": "user abort"})
    assert err.kind is ErrorKind.BUSINESS_GATE


def test_backend_phase_result_parse_protocol_parse():
    err = classify_backend_output({"phase": "result_parse", "message": "no result"})
    assert err.kind is ErrorKind.PROTOCOL_PARSE


def test_backend_phase_timeout_transport_timeout():
    err = classify_backend_output({"phase": "timeout", "message": "stalled"})
    assert err.kind is ErrorKind.TRANSPORT_TIMEOUT


def test_backend_unknown_falls_to_unknown():
    """无任何信号命中 → UNKNOWN（raw 保留）。"""
    err = classify_backend_output({"unfamiliar_field": "x"})
    assert err.kind is ErrorKind.UNKNOWN
    assert err.raw is not None


# ── profile 钩子调度顺序（Q6 单一分类权威）────────────────────────────────────


class _FakeProfile:
    """模拟 profile，提供 classify_backend_error 钩子。"""

    def __init__(self, return_value):
        self._return = return_value

    def classify_backend_error(self, raw):
        return self._return


def test_profile_hook_takes_precedence():
    """SPEC §2.2 调度顺序 (1)：profile 钩子优先，返非 None → 用之。"""
    profile = _FakeProfile(ErrorKind.BUSINESS_AGENT)
    err = classify_backend_output(
        {"exit_code": 137, "stderr": "OOM"},  # 默认会归 TRANSPORT_PROCESS
        profile=profile,
    )
    assert err.kind is ErrorKind.BUSINESS_AGENT


def test_profile_hook_returning_none_falls_through():
    """调度顺序 (2)：profile 返 None → 走通用规则表。"""
    profile = _FakeProfile(None)
    err = classify_backend_output(
        {"exit_code": 137}, profile=profile,
    )
    assert err.kind is ErrorKind.TRANSPORT_PROCESS


class _ExplodingProfile:
    def classify_backend_error(self, raw):
        raise RuntimeError("profile imploded")


def test_profile_hook_exception_swallowed():
    """调度顺序 (1)：profile 钩子禁止抛错（异常 → None，让通用表兜底）。"""
    err = classify_backend_output(
        {"exit_code": 137}, profile=_ExplodingProfile(),
    )
    assert err.kind is ErrorKind.TRANSPORT_PROCESS


# ── 纯函数不变量 ─────────────────────────────────────────────────────────────


def test_classifier_is_pure_function():
    """同样输入同样输出（铁律 3：classifier 纯函数）。"""
    raw = {"exit_code": 137}
    e1 = classify_backend_output(raw)
    e2 = classify_backend_output(raw)
    assert e1.kind == e2.kind
    assert e1.message == e2.message


# ── ErrorLayerFromKind on classified errors ────────────────────────────────────


def test_classified_error_layer_via_kind():
    """classify 出来的 Error.layer_from_kind 与 kind 一致。"""
    err = classify_backend_output({"exit_code": 137})
    assert err.layer_from_kind() == "transport"

    err2 = classify_backend_output({"phase": "validator"})
    assert err2.layer_from_kind() == "protocol"

    err3 = classify_backend_output({"phase": "config"})
    assert err3.layer_from_kind() == "business"


# ── first-match-wins 优先级 + WorkflowTerminated negative ────────────────────


def test_backend_stream_with_is_error_and_tool_use_id_first_match_wins():
    """SPEC §2.2 first-match-wins：phase=stream + is_error=True + tool_use_id 同时存在
    → 命中行 6（BUSINESS_AGENT），不被后面的 phase=stream 行 7（PROTOCOL_PARSE）抢Match。
    """
    err = classify_backend_output({
        "phase": "stream",
        "is_error": True,
        "tool_use_id": "toolu_01",
        "message": "agent tool failed",
    })
    assert err.kind is ErrorKind.BUSINESS_AGENT


def test_classify_exception_workflow_terminated_falls_to_unknown():
    """SPEC §4.1 决策 1.2：WorkflowTerminated 是独立 exception（非 ExecError 子类），
    classifier 不直接处理（orchestrator 显式翻译）；落到 UNKNOWN 分支。
    """
    from orca.run.errors import WorkflowTerminated
    exc = WorkflowTerminated(
        status="failed", reason="business reject", outputs={}, node="reject",
    )
    err = classify_exception(exc)
    assert err.kind is ErrorKind.UNKNOWN
    assert err.raw is not None


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: KeyError("missing"),
        lambda: ValueError("bad value"),
        lambda: RuntimeError("weird"),
        lambda: Exception("generic"),
    ],
)
def test_classify_exception_stdlib_unknown(exc_factory):
    """SPEC §2.2 行 17：其他 stdlib 异常 → UNKNOWN（raw 保留）。"""
    err = classify_exception(exc_factory())
    assert err.kind is ErrorKind.UNKNOWN


def test_classify_exception_profile_hook_implosion_logged():
    """SPEC §2.2 第 1 条：profile 钩子抛错 → log warning + 走通用规则表（不静默吞）。"""
    class _Boom:
        def classify_backend_error(self, raw):
            raise RuntimeError("imploded")

    err = classify_exception(
        ConnectionError("refused"),
        profile=_Boom(),  # type: ignore[arg-type]
    )
    # profile 异常降级 → 走通用 isinstance 表 → ConnectionError → TRANSPORT_NETWORK
    assert err.kind is ErrorKind.TRANSPORT_NETWORK
