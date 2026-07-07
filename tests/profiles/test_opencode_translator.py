"""tests/profiles/test_opencode_translator.py —— opencode_translator 纯函数测试。

覆盖（按真实 opencode v1.14.22 NDJSON 校准 + web-shell-v2 §3.2 B1 lossless 扩展；
fixture 见 fixtures/opencode_sample.jsonl）：
  - step_start → agent_step_started（web-v2 §3.2 B1，data.step_reason 可选）
  - reasoning → agent_thinking（web-v2 §3.2 B1，--thinking on 时发；整块）
  - text → agent_message（整块，非增量）
  - tool_use completed → agent_tool_call + agent_tool_result（一次发）
  - tool_use non-completed → []（半成品不发）
  - step_finish → agent_usage（input/output/cache.read/cost + **reasoning_tokens**）
  - error → error 事件（带 message）
  - 未知 type → unknown_event（web-v2 §3.2 D8 tape escape hatch，**绝不静默丢**）
  - 非 JSON → []（CLIRunner 兜底，translator 防御性）
  - 所有产出 Event.session_id == 入参 session_id
  - 纯函数性（同输入两次调用结果相同）

fixture：``opencode_sample.jsonl``（9 行：含 reasoning capture + 1 条 experimental 未知 envelope，
tool output 已脱敏缩小，无 token 泄漏）。
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
SAMPLE_REASONING_LINE = _find("reasoning")
SAMPLE_TEXT_LINE = _find("text")
SAMPLE_TOOL_USE_LINE = _find("tool_use")
SAMPLE_STEP_FINISH_LINE = _find("step_finish")


# ── step_start → agent_step_started（web-v2 §3.2 B1 liveness 心跳）───────────


def test_step_start_to_agent_step_started_with_reason():
    """step_start（带 reason）→ agent_step_started{step_reason: <reason>}。

    web-shell-v2 §3.2 B1：opencode step_start 是 liveness 心跳；part.reason 透传到前端
    便于「第 N 步」标记。**不进 RunState**（reducer no-op），仅 AgentHistory 显示。

    注：真实 opencode v1.14.22 抓取中 ``step_start.part`` 从不带 ``reason`` 字段（reason
    只在 ``step_finish.part.reason`` 出现）。本测试**内联构造**带 reason 的 step_start，
    锁死 translator 对该字段的 defensive 透传逻辑（防未来 opencode 协议变更 / 或其他 fork
    后端把 reason 放 step_start 时 Orca 不丢字段）。fixture 不覆盖此分支（real protocol
    不发），故不能用 fixture 喂。
    """
    line = json.dumps({
        "type": "step_start",
        "part": {
            "id": "p2", "type": "step-start", "messageID": "m2",
            "reason": "tool-calls",
        },
    })
    events = opencode_translator(line, SESSION)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "agent_step_started"
    assert ev.data == {"step_reason": "tool-calls"}
    assert ev.session_id == SESSION


def test_step_start_reason_non_string_falls_back_to_empty_data():
    """reason 非 str（None / 数字 / 缺失）→ data={}（防御性，part.reason 类型异常不抛）。"""
    for reason_value in (None, 123, [], {}):
        line = json.dumps({
            "type": "step_start",
            "part": {"id": "p", "type": "step-start", "reason": reason_value},
        })
        ev = opencode_translator(line, SESSION)[0]
        assert ev.type == "agent_step_started"
        assert ev.data == {}, f"reason={reason_value!r} 应被忽略（非 str）"


def test_step_start_without_reason_emits_empty_data():
    """step_start（无 reason，首 step）→ agent_step_started{}（data 为空 dict）。"""
    line = json.dumps({
        "type": "step_start",
        "part": {"id": "p1", "type": "step-start", "messageID": "m1"},
    })
    events = opencode_translator(line, SESSION)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "agent_step_started"
    assert ev.data == {}  # 无 reason → 空 dict（不是 None，不是 missing key）


# ── reasoning → agent_thinking（web-v2 §3.2 B1，--thinking on）───────────────


def test_reasoning_to_agent_thinking_whole_block():
    """reasoning envelope（--thinking on）→ agent_thinking{text}（整块）。

    web-shell-v2 §3.2 B1 / §5.3：与 claude thinking_delta 同 canonical 事件，前端琥珀折叠。
    opencode 块级语义（非 token 增量），整块发。
    """
    obj = json.loads(SAMPLE_REASONING_LINE)
    expected_text = obj["part"]["text"]
    events = opencode_translator(SAMPLE_REASONING_LINE, SESSION)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "agent_thinking"
    assert ev.data == {"text": expected_text}
    assert ev.session_id == SESSION


def test_empty_reasoning_returns_empty():
    """空 reasoning 文本不发（与 _translate_text 同步，减少噪音）。"""
    line = json.dumps({"type": "reasoning", "part": {"type": "reasoning", "text": ""}})
    assert opencode_translator(line, SESSION) == []


def test_reasoning_missing_text_key_returns_empty():
    """reasoning 的 part.text 完全缺失（不是空串）→ []（防御性，与空串同路径）。"""
    line = json.dumps({"type": "reasoning", "part": {"type": "reasoning"}})
    assert opencode_translator(line, SESSION) == []


def test_reasoning_missing_part_returns_empty():
    """reasoning 的 part 字段缺失 / 非 dict → []（防御性）。"""
    line = json.dumps({"type": "reasoning"})
    assert opencode_translator(line, SESSION) == []
    line = json.dumps({"type": "reasoning", "part": "not-a-dict"})
    assert opencode_translator(line, SESSION) == []


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
    """step_finish 的 tokens/cost → agent_usage（cache_tokens 来自 cache.read，
    **reasoning_tokens 来自 tokens.reasoning**，web-v2 §3.2 B1）。"""
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
    # web-v2 §3.2 B1：reasoning_tokens 从 tokens.reasoning 取
    assert ev.data["reasoning_tokens"] == tokens.get("reasoning", 0)
    assert ev.session_id == SESSION


def test_step_finish_without_reasoning_defaults_zero():
    """旧 opencode 协议（无 tokens.reasoning 字段）→ reasoning_tokens=0（lossless 兜底）。

    消费侧 ``data.get('reasoning_tokens', 0)`` 兜底，**不破坏旧 tape replay**。
    """
    line = json.dumps({
        "type": "step_finish",
        "part": {
            "type": "step-finish",
            "tokens": {"total": 100, "input": 80, "output": 20, "cache": {"read": 0}},
            "cost": 0.001,
        },
    })
    ev = opencode_translator(line, SESSION)[0]
    assert ev.data["reasoning_tokens"] == 0


def test_step_finish_missing_tokens_returns_empty():
    """step_finish 的 tokens 字段缺失 / 非 dict → []（防御性，fail loud 不抛）。"""
    line = json.dumps({"type": "step_finish", "part": {"type": "step-finish", "cost": 0.001}})
    assert opencode_translator(line, SESSION) == []
    line = json.dumps({"type": "step_finish", "part": {"type": "step-finish", "tokens": "x"}})
    assert opencode_translator(line, SESSION) == []


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


def test_unknown_type_emits_unknown_event():
    """未知 envelope type → unknown_event（web-v2 §3.2 D8 tape escape hatch）。

    **绝不静默丢**：raw 整行进 tape（reducer MUST no-op，仅 LogStream / AgentHistory dim 渲染）。
    便于排查协议漂移（opencode 加新 envelope → 用户立刻在 LogStream 看到 ? unknown 行）。
    """
    line = json.dumps({"type": "experimental_event", "part": {"note": "future envelope"}})
    events = opencode_translator(line, SESSION)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "unknown_event"
    assert ev.data["source"] == "opencode"
    assert ev.data["raw"] == {"type": "experimental_event", "part": {"note": "future envelope"}}
    assert ev.session_id == SESSION


def test_missing_type_field_emits_unknown_event():
    """完全无 type 字段的 envelope → unknown_event（防御性，fail-loud 不丢）。"""
    line = json.dumps({"part": {"x": 1}, "sessionID": "ses_internal"})
    events = opencode_translator(line, SESSION)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "unknown_event"
    assert ev.data["source"] == "opencode"
    assert ev.data["raw"]["part"] == {"x": 1}


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
    """整 fixture 跑一遍，产出的 type 集合符合预期（覆盖 text+tool+usage+reasoning+step_start+unknown）。"""
    all_events = []
    for line in full_stream:
        all_events.extend(opencode_translator(line, SESSION))
    types = [ev.type for ev in all_events]
    # fixture 9 行的语义：
    #   step_start(1) → agent_step_started
    #   text(1) → agent_message
    #   tool_use → agent_tool_call + agent_tool_result
    #   step_finish(1, tool-calls, reasoning=67) → agent_usage (含 reasoning_tokens)
    #   step_start(2, 无 reason) → agent_step_started
    #   reasoning → agent_thinking
    #   text(2) → agent_message
    #   step_finish(2, stop, reasoning=109) → agent_usage (含 reasoning_tokens)
    #   experimental_event → unknown_event
    assert types.count("agent_message") == 2
    assert types.count("agent_tool_call") == 1
    assert types.count("agent_tool_result") == 1
    assert types.count("agent_usage") == 2  # 每 step 一条
    assert types.count("agent_step_started") == 2  # 两个 step
    assert types.count("agent_thinking") == 1  # reasoning envelope
    assert types.count("unknown_event") == 1  # experimental envelope
    # reasoning_tokens 进 agent_usage（web-v2 §3.2 B1）
    usage_events = [ev for ev in all_events if ev.type == "agent_usage"]
    assert all("reasoning_tokens" in ev.data for ev in usage_events)
    assert usage_events[0].data["reasoning_tokens"] == 67  # step 1
    assert usage_events[1].data["reasoning_tokens"] == 109  # step 2
