"""tests/profiles/test_opencode_translator.py —— opencode_translator 纯函数测试。

覆盖（按真实 opencode v1.14.22 NDJSON 校准，fixture 见 fixtures/opencode_sample.jsonl）：
  - step_start → []（无业务信号）
  - text → agent_message（整块，非增量）
  - tool_use completed → agent_tool_call + agent_tool_result（一次发）
  - tool_use non-completed → []（半成品不发）
  - step_finish → agent_usage（input/output/cache.read/cost）
  - error → error 事件（带 message）
  - 未知 type / 非 JSON → []（不抛）
  - 所有产出 Event.session_id == 入参 session_id
  - 纯函数性（同输入两次调用结果相同）

fixture：``opencode_sample.jsonl``（7 行真实抓取，tool output 已脱敏缩小，无 token 泄漏）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca.profiles.translators.opencode import opencode_translator

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "opencode_sample.jsonl"
SESSION = "orca-session-opencode-xyz789"


# ── fixture 加载（真实 NDJSON，只读不改原文件）──────────────────────────────


@pytest.fixture(scope="module")
def full_stream() -> list[str]:
    """完整 7 行 fixture（每行一个 opencode NDJSON part）。"""
    return [
        ln.rstrip("\n")
        for ln in FIXTURE.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


# ── 单行常量（按顶层 type 从 fixture 取，便于单行单元测试）────────────────────

_STREAM = [ln for ln in FIXTURE.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _find(top_type: str) -> str:
    """返回第一条顶层 type == top_type 的原始行字符串。"""
    for ln in _STREAM:
        obj = json.loads(ln)
        if obj.get("type") == top_type:
            return ln
    raise AssertionError(f"fixture 无 type={top_type} 行")


SAMPLE_STEP_START_LINE = _find("step_start")
SAMPLE_TEXT_LINE = _find("text")
SAMPLE_TOOL_USE_LINE = _find("tool_use")
SAMPLE_STEP_FINISH_LINE = _find("step_finish")


# ── step_start → [] ──────────────────────────────────────────────────────────


def test_step_start_returns_empty():
    """step_start 是回合开始信号，无业务事件（agent 生命周期由 executor 管）。"""
    assert opencode_translator(SAMPLE_STEP_START_LINE, SESSION) == []


# ── text → agent_message（整块，非增量）──────────────────────────────────────


def test_text_to_agent_message_whole_block():
    """text 事件的 part.text 整块发为 agent_message（opencode 一次发完整段，非 token 增量）。"""
    obj = json.loads(SAMPLE_TEXT_LINE)
    expected_text = obj["part"]["text"]
    events = opencode_translator(SAMPLE_TEXT_LINE, SESSION)
    assert len(events) == 1
    assert events[0].type == "agent_message"
    assert events[0].data == {"text": expected_text}  # 整块
    assert events[0].session_id == SESSION


def test_empty_text_returns_empty():
    """空 text 不发（减少噪音，与 claude_translator 一致）。"""
    line = json.dumps({"type": "text", "part": {"type": "text", "text": ""}})
    assert opencode_translator(line, SESSION) == []


# ── tool_use completed → agent_tool_call + agent_tool_result（一次发）────────


def test_tool_use_completed_emits_call_and_result():
    """completed 状态的工具事件一次发 call + result（opencode 把两者合在一条事件里）。"""
    obj = json.loads(SAMPLE_TOOL_USE_LINE)
    part = obj["part"]
    state = part["state"]
    events = opencode_translator(SAMPLE_TOOL_USE_LINE, SESSION)
    assert len(events) == 2

    call_ev, result_ev = events
    assert call_ev.type == "agent_tool_call"
    assert call_ev.data["tool"] == part["tool"]  # "bash"
    assert call_ev.data["tool_call_id"] == part["callID"]
    assert call_ev.data["args"] == state["input"]  # 完整 input

    assert result_ev.type == "agent_tool_result"
    assert result_ev.data["tool_call_id"] == part["callID"]
    assert result_ev.data["result"] == state["output"]
    assert result_ev.session_id == SESSION


def test_tool_use_running_returns_empty():
    """非 completed 状态（running/pending）的半成品不发。"""
    line = json.dumps(
        {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "bash",
                "callID": "call_x",
                "state": {"status": "running", "input": {"command": "ls"}},
            },
        }
    )
    assert opencode_translator(line, SESSION) == []


def test_tool_result_truncated_when_huge():
    """工具 output 超 _TOOL_RESULT_MAX_CHARS 时截断（防异常输出喷爆事件流）。"""
    huge_output = "x" * 5000
    line = json.dumps(
        {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "bash",
                "callID": "call_big",
                "state": {"status": "completed", "input": {}, "output": huge_output},
            },
        }
    )
    events = opencode_translator(line, SESSION)
    result_ev = [e for e in events if e.type == "agent_tool_result"][0]
    assert result_ev.data["result"].endswith("…[truncated]")
    assert len(result_ev.data["result"]) < len(huge_output)


def test_tool_result_structured_output_json_serialized():
    """工具 output 为 dict/list（结构化）→ JSON 串（罕见但 opencode 工具可能返回）。

    覆盖 _normalize_tool_output 的 json.dumps 分支（实测 opencode output 多为 str，此为防御）。
    """
    structured_output = {"files": ["a.txt", "b.txt"], "count": 2}
    line = json.dumps(
        {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "list",
                "callID": "call_struct",
                "state": {"status": "completed", "input": {}, "output": structured_output},
            },
        }
    )
    events = opencode_translator(line, SESSION)
    result_ev = [e for e in events if e.type == "agent_tool_result"][0]
    # 结构化 output 经 json.dumps 成串（可读、可逆）
    parsed = json.loads(result_ev.data["result"])
    assert parsed == structured_output


# ── step_finish → agent_usage ────────────────────────────────────────────────


def test_step_finish_to_agent_usage():
    """step_finish 的 tokens/cost → agent_usage（cache_tokens 来自 cache.read）。"""
    obj = json.loads(SAMPLE_STEP_FINISH_LINE)
    part = obj["part"]
    tokens = part["tokens"]
    cache = tokens.get("cache") or {}
    events = opencode_translator(SAMPLE_STEP_FINISH_LINE, SESSION)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "agent_usage"
    assert ev.data["input_tokens"] == tokens["input"]
    assert ev.data["output_tokens"] == tokens["output"]
    assert ev.data["cache_tokens"] == cache.get("read", 0)
    assert ev.data["cost_usd"] == part["cost"]
    assert ev.session_id == SESSION


# ── error → error 事件 ───────────────────────────────────────────────────────


def test_error_event_translated():
    """opencode error（error.name + error.data.message）→ Orca error 事件。

    真实结构（抓取）：{"type":"error","error":{"name":"UnknownError","data":{"message":"..."}}}
    opencode 无结构化 HTTP 码字段，故不设 api_error_status（RunAccumulator 抓不到时为 None）。
    """
    line = json.dumps(
        {
            "type": "error",
            "error": {
                "name": "UnknownError",
                "data": {"message": "Model not found: provider/glm-4.6"},
            },
        }
    )
    events = opencode_translator(line, SESSION)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "error"
    assert ev.data["error_type"] == "UnknownError"
    assert ev.data["message"] == "Model not found: provider/glm-4.6"
    assert ev.data["phase"] == "stream"
    # 无 api_error_status（opencode error.data 不带结构化 HTTP 码）
    assert "api_error_status" not in ev.data
    assert ev.session_id == SESSION


def test_error_event_missing_data_falls_back_to_name():
    """error.data 缺失时 message 回落到 error.name（fail loud 不丢信息）。"""
    line = json.dumps({"type": "error", "error": {"name": "SomeError"}})
    ev = opencode_translator(line, SESSION)[0]
    assert ev.data["message"] == "SomeError"
    assert ev.data["error_type"] == "SomeError"


# ── 未知 type / 非法结构 → []（防御性，不抛）──────────────────────────────────


def test_unknown_type_returns_empty():
    assert opencode_translator(json.dumps({"type": "foobar"}), SESSION) == []


def test_non_json_line_returns_empty():
    """非 JSON 行 → []（不抛；CLIRunner 已对心跳行 debug log + 跳过）。"""
    assert opencode_translator("not-json-heartbeat", SESSION) == []
    assert opencode_translator("", SESSION) == []


def test_non_dict_json_returns_empty():
    assert opencode_translator("[1,2,3]", SESSION) == []
    assert opencode_translator('"a string"', SESSION) == []


# ── session_id 一致性（铁律 5）───────────────────────────────────────────────


def test_all_events_carry_input_session_id(full_stream):
    """全流跑一遍：每个产出 Event.session_id == 入参 SESSION（铁律 5）。"""
    for line in full_stream:
        for ev in opencode_translator(line, SESSION):
            assert ev.session_id == SESSION, f"session_id 不一致：{ev.type} {ev.data}"


def test_session_id_not_opencode_internal(full_stream):
    """translator 不复用 opencode 流里的 sessionID 字段。

    fixture 行带 ``sessionID``（opencode 内部会话 id），translator 必须用入参 SESSION。
    """
    for line in full_stream:
        for ev in opencode_translator(line, SESSION):
            assert ev.session_id == SESSION


# ── 纯函数性（铁律 3：同输入两次调用结果相同）─────────────────────────────────


def test_pure_function_idempotent():
    """同输入两次调用产出相同 Event（无副作用，铁律 3）。"""
    e1 = opencode_translator(SAMPLE_TOOL_USE_LINE, SESSION)
    e2 = opencode_translator(SAMPLE_TOOL_USE_LINE, SESSION)
    assert len(e1) == len(e2)
    for a, b in zip(e1, e2):
        assert a.type == b.type
        assert a.data == b.data
        assert a.session_id == b.session_id


def test_seq_zero_placeholder():
    """决策 2：executor/translator 不写 tape，Event.seq=0 占位（orchestrator 重分配）。"""
    events = opencode_translator(SAMPLE_STEP_FINISH_LINE, SESSION)
    assert all(ev.seq == 0 for ev in events)


# ── 全流回归：完整 fixture 跑通产合理事件集 ──────────────────────────────────


def test_full_stream_translates_to_expected_event_types(full_stream):
    """整 7 行 fixture 跑一遍，产出的 type 集合符合预期（覆盖 text+tool+usage）。"""
    all_events = []
    for line in full_stream:
        all_events.extend(opencode_translator(line, SESSION))
    types = [ev.type for ev in all_events]
    # 这条 fixture 的语义：text → bash 工具 → step_finish → (二回合) text → step_finish
    assert "agent_message" in types  # 文本段
    assert "agent_tool_call" in types  # bash 工具调用
    assert "agent_tool_result" in types  # 工具结果
    assert "agent_usage" in types  # step_finish 的 usage（出现 2 次，每 step 一次）
    assert types.count("agent_usage") == 2
