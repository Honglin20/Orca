"""tests/exec/claude/test_accumulator.py —— RunAccumulator 直接单元测试。

RunAccumulator 是「跨后端共享的终态累积器」（events + result_line 两模式共用），是本特性
核心抽象。经 e2e 间接覆盖不足以钉死内部分支语义（尤其 events 模式的 consume_event 各分支、
make_on_result_hook 的 5 字段映射、diagnose 的多信号拼接），故补直接单测做回归保护。

依赖：只构造 Event 与 RunAccumulator，无 mock、无 spawn。
"""

from __future__ import annotations

import time

from orca.exec.claude.accumulator import RunAccumulator
from orca.schema import Event


def _ev(event_type: str, data: dict, session_id: str = "s1") -> Event:
    """构造占位 Event（seq=0，consume_event 不读 seq/timestamp，仅 type/data）。"""
    return Event(
        seq=0,
        type=event_type,  # type: ignore[arg-type]
        timestamp=time.time(),
        session_id=session_id,
        data=dict(data),
    )


# ── make_on_result_hook（result_line 模式）────────────────────────────────────


def test_make_on_result_hook_writes_all_five_fields():
    """5 参闭包一次性填满累积器的 5 个字段（行为逐字同重构前 on_result 闭包）。"""
    acc = RunAccumulator()
    hook = acc.make_on_result_hook()
    hook("final answer", {"input_tokens": 10}, 0.5, True, 529)
    assert acc.result_text == "final answer"
    assert acc.usage == {"input_tokens": 10}
    assert acc.cost == 0.5
    assert acc.is_error is True
    assert acc.api_error_status == 529


def test_make_on_result_hook_api_error_status_defaults_none():
    """第 5 参 api_error_status 可选，缺省 None（签名兼容老 4 参调用）。"""
    acc = RunAccumulator()
    hook = acc.make_on_result_hook()
    hook("text", {}, 0.0, False)
    assert acc.api_error_status is None
    assert acc.is_error is False


# ── consume_event（events 模式）───────────────────────────────────────────────


def test_consume_event_agent_message_appends():
    """多条 agent_message 的 text 拼接成最终答案（events 模式核心契约）。"""
    acc = RunAccumulator()
    acc.consume_event(_ev("agent_message", {"text": "Hello "}))
    acc.consume_event(_ev("agent_message", {"text": "World"}))
    assert acc.events_result_text == "Hello World"


def test_consume_event_empty_text_not_appended():
    """空 text 的 agent_message 不追加（result_text 不被空串污染）。"""
    acc = RunAccumulator()
    acc.consume_event(_ev("agent_message", {"text": ""}))
    acc.consume_event(_ev("agent_message", {"text": "only"}))
    assert acc.events_result_text == "only"


def test_consume_event_agent_usage_last_wins():
    """多条 agent_usage：usage/cost 以最后一条为准（钉死「覆盖」语义，非累加）。"""
    acc = RunAccumulator()
    acc.consume_event(_ev("agent_usage", {
        "input_tokens": 100, "output_tokens": 10, "cache_tokens": 0, "cost_usd": 0.01,
    }))
    acc.consume_event(_ev("agent_usage", {
        "input_tokens": 200, "output_tokens": 20, "cache_tokens": 5, "cost_usd": 0.02,
    }))
    assert acc.usage == {
        "input_tokens": 200, "output_tokens": 20, "cache_tokens": 5, "cost_usd": 0.02,
    }
    assert acc.cost == 0.02


def test_consume_event_error_sets_is_error_and_message():
    """error 事件置 is_error + 抓 message（让 diagnose 能带具体失败原因）。"""
    acc = RunAccumulator()
    acc.consume_event(_ev("error", {
        "error_type": "UnknownError", "phase": "stream", "message": "Model not found",
    }))
    assert acc.is_error is True
    assert acc.error_message == "Model not found"


def test_consume_event_error_with_api_error_status():
    """error 事件带 api_error_status（int）→ 累积器抓 HTTP 码（events 模式扩展点）。"""
    acc = RunAccumulator()
    acc.consume_event(_ev("error", {
        "error_type": "ApiError", "phase": "stream",
        "message": "boom", "api_error_status": 529,
    }))
    assert acc.api_error_status == 529


def test_consume_event_error_invalid_api_error_status_becomes_none():
    """api_error_status 非数字 → None（fail loud 不抛，diagnose 仍带 message）。"""
    acc = RunAccumulator()
    acc.consume_event(_ev("error", {
        "error_type": "X", "phase": "stream",
        "message": "boom", "api_error_status": "not-a-number",
    }))
    assert acc.api_error_status is None
    assert acc.is_error is True


def test_consume_event_ignores_irrelevant_types():
    """非终态事件（tool_call/tool_result/thinking/node_*）不污染累积器字段。"""
    acc = RunAccumulator()
    acc.consume_event(_ev("agent_tool_call", {"tool": "bash", "args": {}, "tool_call_id": "c1"}))
    acc.consume_event(_ev("agent_tool_result", {"tool_call_id": "c1", "result": "out"}))
    acc.consume_event(_ev("agent_thinking", {"text": "hmm"}))
    assert acc.result_text is None
    assert acc.events_result_text is None
    assert acc.usage is None
    assert acc.is_error is False


# ── events_result_text 边界 ──────────────────────────────────────────────────


def test_events_result_text_none_when_no_messages():
    """无任何 agent_message → events_result_text 为 None（executor 的「无 result」判定依赖此）。"""
    acc = RunAccumulator()
    assert acc.events_result_text is None


# ── diagnose（两模式共用错误摘要）────────────────────────────────────────────


def test_diagnose_combines_all_signals():
    """HTTP 码 + is_error + error_message + result + stderr 五段全拼。"""
    acc = RunAccumulator(
        result_text="partial result",
        is_error=True,
        api_error_status=529,
        error_message="overloaded",
    )
    diag = acc.diagnose(stderr="some stderr tail")
    assert "HTTP 529" in diag
    assert "result.is_error=true" in diag
    assert "error=" in diag
    assert "overloaded" in diag
    assert "partial result" in diag
    assert "some stderr tail" in diag
    # 分隔符是中文分号
    assert "；" in diag


def test_diagnose_empty_returns_placeholder():
    """空累积器 + 空 stderr → 兜底占位（不返回空串，可观测性）。"""
    acc = RunAccumulator()
    diag = acc.diagnose(stderr="")
    assert diag == "（无 stderr / result 详情）"


def test_diagnose_truncates_result_and_stderr():
    """result 文本与 stderr 末尾均截断到 300 字符（防异常输出喷爆诊断）。"""
    long_result = "x" * 1000
    long_stderr = "y" * 1000
    acc = RunAccumulator(result_text=long_result)
    diag = acc.diagnose(stderr=long_stderr)
    # result 截 300 + repr 引号；stderr 取末尾 300
    assert "x" * 300 in diag
    assert "y" * 300 in diag
    assert long_result not in diag  # 完整 1000 字符不在
