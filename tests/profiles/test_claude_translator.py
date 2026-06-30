"""tests/profiles/test_claude_translator.py —— claude_translator 纯函数测试（决策 1）。

测试在 tests/profiles/（**不在** tests/exec/claude/）—— 因为 translator 代码归属 profiles 层
（决策 1，见 docs/releases/2026-06-30-phase4-exec.md）。SPEC §7.11 写的
``tests/exec/claude/test_translator.py`` 路径已被决策 1 修正。

覆盖 SPEC §7.4 / 计划 C.5 全部断言：
  - text_delta → agent_message（增量片段）
  - thinking_delta → agent_thinking（增量片段）
  - input_json_delta → []（不翻译）
  - assistant tool_use → agent_tool_call（完整 input）
  - user tool_result → agent_tool_result
  - result success → agent_usage（input/output/cache/cost）
  - result is_error → []
  - 未知 type → []（不抛）
  - 所有产出 Event.session_id == 入参 session_id
  - 纯函数性（同输入两次调用结果相同）

fixture：``sample_with_bash.jsonl``（42 行真实 claude stream-json，
claude_code_version 2.1.150），从 AgentHarness 录制**只读拷贝**（不修改原文件）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca.profiles.translators.claude import claude_translator

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_with_bash.jsonl"
SESSION = "orca-session-abc123"


# ── fixture 加载（真实 stream-json，只读不改原文件）──────────────────────────


@pytest.fixture(scope="module")
def full_stream() -> list[str]:
    """完整 42 行 fixture（每行一个 stream-json）。"""
    return [ln.rstrip("\n") for ln in FIXTURE.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.fixture(scope="module")
def lines_by_type(full_stream) -> dict[str, list[dict]]:
    """按顶层 type 分组 fixture 行（便于按需取单行）。"""
    grouped: dict[str, list[dict]] = {}
    for ln in full_stream:
        obj = json.loads(ln)
        grouped.setdefault(obj.get("type"), []).append(obj)
    return grouped


def _first_line(lines_by_type, top_type: str, **predicates) -> str:
    """取第一个匹配的 fixture 行的原始字符串。"""
    for obj in lines_by_type.get(top_type, []):
        if all(_deep_get(obj, k) == v for k, v in predicates.items()):
            return json.dumps(obj)
    raise AssertionError(f"fixture 无匹配 {top_type} {predicates}")


def _deep_get(d, dotted_key):
    cur = d
    for part in dotted_key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


# ── 单行常量（C.1：从 fixture 实测拆出，给单行单元测试用）─────────────────────

# 这些常量在 module 顶层用 pathlib 读 fixture（fixture 是稳定录制文件，module-scope 安全）。
_STREAM = [ln for ln in FIXTURE.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _find(predicate):
    """返回第一个满足 predicate(obj) 的原始行字符串。"""
    for ln in _STREAM:
        obj = json.loads(ln)
        if predicate(obj):
            return ln
    raise AssertionError("fixture 未找到目标行")


# text_delta 行（fixture 第 37 行附近：delta.text == "DONE"）
SAMPLE_TEXT_DELTA_LINE = _find(
    lambda o: o.get("type") == "stream_event"
    and _deep_get(o, "event.type") == "content_block_delta"
    and _deep_get(o, "event.delta.type") == "text_delta"
)
# thinking_delta 行（fixture 第 7-24 行，取第一段 "The"）
SAMPLE_THINKING_DELTA_LINE = _find(
    lambda o: o.get("type") == "stream_event"
    and _deep_get(o, "event.type") == "content_block_delta"
    and _deep_get(o, "event.delta.type") == "thinking_delta"
)
# input_json_delta 行（fixture 第 28 行）
SAMPLE_INPUT_JSON_DELTA_LINE = _find(
    lambda o: o.get("type") == "stream_event"
    and _deep_get(o, "event.delta.type") == "input_json_delta"
)
# assistant tool_use 行（fixture 第 29 行：完整 Bash tool_use）
SAMPLE_TOOL_USE_LINE = _find(
    lambda o: o.get("type") == "assistant"
    and any(
        isinstance(b, dict) and b.get("type") == "tool_use"
        for b in (o.get("message", {}).get("content") or [])
    )
)
# user tool_result 行（fixture 第 33 行：result="PHASE_B_FIXTURE"）
SAMPLE_TOOL_RESULT_LINE = _find(
    lambda o: o.get("type") == "user"
    and any(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for b in (o.get("message", {}).get("content") or [])
    )
)
# result success 行（fixture 第 42 行）
SAMPLE_RESULT_SUCCESS_LINE = _find(
    lambda o: o.get("type") == "result" and o.get("subtype") == "success"
)


# ── text_delta → agent_message（增量片段）────────────────────────────────────


def test_text_delta_to_agent_message_increment():
    obj = json.loads(SAMPLE_TEXT_DELTA_LINE)
    expected_text = obj["event"]["delta"]["text"]
    events = claude_translator(SAMPLE_TEXT_DELTA_LINE, SESSION)
    assert len(events) == 1
    assert events[0].type == "agent_message"
    assert events[0].data == {"text": expected_text}  # 增量片段，非拼接
    assert events[0].session_id == SESSION


# ── thinking_delta → agent_thinking（增量片段）───────────────────────────────


def test_thinking_delta_to_agent_thinking_increment():
    obj = json.loads(SAMPLE_THINKING_DELTA_LINE)
    expected_text = obj["event"]["delta"]["thinking"]
    events = claude_translator(SAMPLE_THINKING_DELTA_LINE, SESSION)
    assert len(events) == 1
    assert events[0].type == "agent_thinking"
    assert events[0].data == {"text": expected_text}
    assert events[0].session_id == SESSION


# ── input_json_delta → []（不翻译，SPEC §10 决策 2）──────────────────────────


def test_input_json_delta_not_translated():
    events = claude_translator(SAMPLE_INPUT_JSON_DELTA_LINE, SESSION)
    assert events == []  # 工具参数增量不拼接，等完整 assistant 消息发 tool_call


# ── assistant tool_use → agent_tool_call（完整 input）────────────────────────


def test_assistant_tool_use_to_agent_tool_call():
    obj = json.loads(SAMPLE_TOOL_USE_LINE)
    tool_use_block = next(
        b for b in obj["message"]["content"] if isinstance(b, dict) and b.get("type") == "tool_use"
    )
    events = claude_translator(SAMPLE_TOOL_USE_LINE, SESSION)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "agent_tool_call"
    assert ev.data["tool"] == "Bash"
    assert ev.data["tool_call_id"] == "call_60676e3300b04f738d92fcdc"
    # 完整 input（非增量拼接）
    assert ev.data["args"] == tool_use_block["input"]
    assert ev.data["args"]["command"] == "echo PHASE_B_FIXTURE"
    assert ev.session_id == SESSION


def test_assistant_text_block_not_duplicated():
    """assistant 行的 text/thinking block 不发（增量已在 stream_event 发过，避免重复）。"""
    # fixture 第 38 行：assistant 完整 message，content 仅 text block
    line = _find(
        lambda o: o.get("type") == "assistant"
        and all(
            isinstance(b, dict) and b.get("type") == "text"
            for b in (o.get("message", {}).get("content") or [])
        )
    )
    events = claude_translator(line, SESSION)
    assert events == []  # text block 是增量片段的回声，不重复发


# ── user tool_result → agent_tool_result ─────────────────────────────────────


def test_user_tool_result_to_agent_tool_result():
    events = claude_translator(SAMPLE_TOOL_RESULT_LINE, SESSION)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "agent_tool_result"
    assert ev.data["tool_call_id"] == "call_60676e3300b04f738d92fcdc"
    assert ev.data["result"] == "PHASE_B_FIXTURE"  # 实测关键断言值
    assert ev.session_id == SESSION


# ── result success → agent_usage（input/output/cache/cost）──────────────────


def test_result_success_to_agent_usage():
    events = claude_translator(SAMPLE_RESULT_SUCCESS_LINE, SESSION)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "agent_usage"
    # 实测关键断言值（来自 fixture 第 42 行）
    assert ev.data["input_tokens"] == 31163
    assert ev.data["output_tokens"] == 49
    assert ev.data["cache_tokens"] == 31168  # = cache_read_input_tokens
    assert ev.data["cost_usd"] == pytest.approx(0.17262400000000003)
    assert ev.session_id == SESSION


def test_result_success_text_not_in_translator_events():
    """result 文本（result.result）不在 translator 事件里——走 CLIRunner.on_result（SPEC §4.4）。

    translator 只产 agent_usage；node_completed 的 output 由 executor 经 on_result + 结构化提取构造。
    """
    events = claude_translator(SAMPLE_RESULT_SUCCESS_LINE, SESSION)
    for ev in events:
        assert "output" not in ev.data  # 不含最终输出文本
        assert ev.type != "node_completed"  # node_completed 是 executor 职责


# ── result is_error → []（executor 层处理）───────────────────────────────────


def test_result_is_error_returns_empty():
    """result + is_error=true → []（executor 据 on_result / 退出码走 stream 错误路径）。"""
    err_result = json.dumps(
        {"type": "result", "subtype": "error", "is_error": True, "result": "boom"}
    )
    assert claude_translator(err_result, SESSION) == []


# ── system api_retry → error 事件（warning 级，可见不阻断）──────────────────


def test_system_api_retry_to_error_event():
    line = json.dumps(
        {
            "type": "system",
            "subtype": "api_retry",
            "retry_count": 2,
            "max_retries": 5,
            "wait_seconds": 3.5,
            "error": "rate limited",
        }
    )
    events = claude_translator(line, SESSION)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "error"
    assert ev.data["phase"] == "api_retry"
    assert ev.data["error_type"] == "ApiRetry"
    assert ev.data["retry_count"] == 2
    assert ev.data["wait_seconds"] == 3.5
    assert "rate limited" in ev.data["message"]
    assert ev.session_id == SESSION


def test_system_init_returns_empty():
    """system/init 行不产事件（可选记日志，SPEC §3.1）。"""
    line = json.dumps(
        {"type": "system", "subtype": "init", "model": "claude-x", "session_id": "claude-internal"}
    )
    assert claude_translator(line, SESSION) == []


def test_system_status_returns_empty():
    line = json.dumps({"type": "system", "subtype": "status", "status": "requesting"})
    assert claude_translator(line, SESSION) == []


# ── 未知 type → []（不抛）─────────────────────────────────────────────────────


def test_unknown_type_returns_empty():
    assert claude_translator(json.dumps({"type": "foobar"}), SESSION) == []


def test_content_block_stop_returns_empty():
    """content_block_stop / message_stop 等 stream_event 子类型不产事件。"""
    line = json.dumps({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}})
    assert claude_translator(line, SESSION) == []


def test_message_stop_returns_empty():
    line = json.dumps({"type": "stream_event", "event": {"type": "message_stop"}})
    assert claude_translator(line, SESSION) == []


# ── 非 JSON / 非法结构 → []（防御性，不抛）───────────────────────────────────


def test_non_json_line_returns_empty():
    """非 JSON 行 → []（不抛；CLIRunner 已对心跳行 debug log + 跳过）。"""
    assert claude_translator("not-json-heartbeat", SESSION) == []
    assert claude_translator("", SESSION) == []


def test_non_dict_json_returns_empty():
    assert claude_translator("[1,2,3]", SESSION) == []
    assert claude_translator('"a string"', SESSION) == []


# ── session_id 一致性（铁律 5）───────────────────────────────────────────────


def test_all_events_carry_input_session_id(full_stream):
    """全流跑一遍：每个产出 Event.session_id == 入参 SESSION（铁律 5）。"""
    for line in full_stream:
        for ev in claude_translator(line, SESSION):
            assert ev.session_id == SESSION, f"session_id 不一致：{ev.type} {ev.data}"


def test_session_id_not_claude_internal(full_stream):
    """translator 不复用 claude 流里的 session_id 字段（SPEC §3.2）。

    claude fixture 行带 ``session_id``（内部会话 id），translator 必须用入参 SESSION，
    不把 claude 的内部 id 塞进 Event。
    """
    claude_internal = "1f8139af-b231-4002-b64e-ef8e8c269a9a"  # fixture 里的 claude session_id
    for line in full_stream:
        for ev in claude_translator(line, SESSION):
            assert ev.session_id == SESSION
            assert ev.session_id != claude_internal


# ── 纯函数性（铁律 3：同输入两次调用结果相同）─────────────────────────────────


def test_pure_function_idempotent():
    """同输入两次调用产出相同 Event（无副作用，铁律 3）。"""
    e1 = claude_translator(SAMPLE_TOOL_USE_LINE, SESSION)
    e2 = claude_translator(SAMPLE_TOOL_USE_LINE, SESSION)
    assert len(e1) == len(e2)
    for a, b in zip(e1, e2):
        assert a.type == b.type
        assert a.data == b.data
        assert a.session_id == b.session_id


def test_seq_zero_placeholder():
    """决策 2：executor/translator 不写 tape，Event.seq=0 占位（orchestrator 重分配）。"""
    events = claude_translator(SAMPLE_RESULT_SUCCESS_LINE, SESSION)
    assert all(ev.seq == 0 for ev in events)


# ── 全流回归：完整 fixture 跑通产合理事件集 ──────────────────────────────────


def test_full_stream_translates_to_expected_event_types(full_stream):
    """整 42 行 fixture 跑一遍，产出的 type 集合符合预期（覆盖 bash 工具调用流）。"""
    all_events = []
    for line in full_stream:
        all_events.extend(claude_translator(line, SESSION))
    types = [ev.type for ev in all_events]
    # 这条 fixture 的语义：思考 → Bash 工具调用 → 工具结果 → 输出 DONE
    assert "agent_thinking" in types  # thinking_delta 片段
    assert "agent_tool_call" in types  # Bash tool_use
    assert "agent_tool_result" in types  # tool_result "PHASE_B_FIXTURE"
    assert "agent_usage" in types  # result 的 usage（只一次）
    # agent_message：fixture 第二回合 text_delta "DONE"
    assert "agent_message" in types
    # agent_usage 只出现一次（铁律：只发一次，避免重复计数）
    assert types.count("agent_usage") == 1
