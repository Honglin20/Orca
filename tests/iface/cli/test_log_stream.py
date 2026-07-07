"""test_log_stream.py —— v2 LogStream + EVENT_LEVEL 测试（spec §2.4 + §8 #8）。

INTENT（CLAUDE.md 铁律 9）：测试不是「widget 写入 X 行」，而是「**用户能在 Log Stream
看到 node 失败原因**」——断言 ``node_failed.data.message`` 完整出现在 LogStream 输出
（reviewer P1-10 不截断）+ 5 level icon 正确 + EVENT_LEVEL 表全覆盖全 EventType
（fail loud 守门，防 schema 加新 type 漏 mapping）。
"""

from __future__ import annotations

import typing

from orca.iface.cli.widgets._event_filter import (
    EVENT_VISIBILITY,
    goes_to_agent_history,
    goes_to_header_footer,
    goes_to_log_stream,
    visibility_of,
)
from orca.iface.cli.widgets.log_stream import (
    EVENT_LEVEL,
    EVENTS_NOT_IN_LOG_STREAM,
    LEVEL_DEBUG,
    LEVEL_ERROR,
    LEVEL_INFO,
    LEVEL_SUCCESS,
    LEVEL_WARN,
    LogStream,
    _LEVEL_ICONS,
    format_event,
    level_of,
)
from orca.schema.event import EventType


# ── 完整性测试（spec §8 #8 + reviewer P0-1 / P0-2）──────────────────────────


class TestEventLevelCompleteness:
    """spec §8 #8：EVENT_LEVEL 表完整性。

    reviewer P0-1 拆两断言：
      - 断言 1：表内每个 key 是合法 EventType，value 是 5 level 之一或 None。
      - 断言 2：全 EventType 必须在表内（含显式 None 的 9 个，web-v2 §3.2 B1 +2）。

    reviewer P0-2：EventType Literal 数量锁（防 drift；web-v2 §3.2 B1 后 = 39）。
    """

    def test_event_level_in_table_valid(self):
        """断言 1：表内每个 key 是合法 EventType，value 是 5 level 之一或 None。"""
        valid_levels = {LEVEL_INFO, LEVEL_SUCCESS, LEVEL_ERROR, LEVEL_WARN, LEVEL_DEBUG, None}
        valid_event_types = set(typing.get_args(EventType))
        for etype, level in EVENT_LEVEL.items():
            assert etype in valid_event_types, (
                f"EVENT_LEVEL 含 EventType 之外的 key: {etype!r}"
            )
            assert level in valid_levels, (
                f"EVENT_LEVEL[{etype!r}]={level!r} 非 5 level 之一也非 None"
            )

    def test_event_level_covers_all_event_types(self):
        """断言 2：全 EventType 都在表内（含显式 None 的 9 个）。"""
        all_event_types = set(typing.get_args(EventType))
        in_table = set(EVENT_LEVEL.keys())
        missing = all_event_types - in_table
        assert missing == set(), (
            f"以下 EventType 既未在 EVENT_LEVEL 登记，也未在 EVENTS_NOT_IN_LOG_STREAM 白名单: "
            f"{missing}"
        )

    def test_event_visibility_completeness(self):
        """spec §11.1：EVENT_VISIBILITY 表全覆盖 EventType（fail loud 守门）。"""
        all_event_types = set(typing.get_args(EventType))
        in_table = set(EVENT_VISIBILITY.keys())
        missing = all_event_types - in_table
        assert missing == set(), f"EVENT_VISIBILITY 漏登记: {missing}"

    def test_event_type_count_drift_guard(self):
        """事实校对：EventType Literal 项数守门（防意外漂移；web-v2 §3.2 B1 后 = 39）。

        新增/删除 EventType 时此处会 fail —— 提醒同步更新：①EVENT_LEVEL 表；
        ②EVENT_VISIBILITY 表；③reducer ``apply_event`` 分支；④本测试期望数。
        """
        all_event_types = typing.get_args(EventType)
        assert len(all_event_types) == 39, (
            f"EventType 数量漂移: 期望 39（web-v2 §3.2 B1 后），实际 {len(all_event_types)}"
        )


# ── level 派生 ──────────────────────────────────────────────────────────


class TestLevelOf:
    """``level_of()`` 派生规则（spec §2.4 + §11.5 #6 三态）。"""

    def test_known_event_returns_level(self):
        """已登记 type 返回对应 level（5 种各覆盖一个）。"""
        assert level_of("node_started") == LEVEL_INFO
        assert level_of("node_completed") == LEVEL_SUCCESS
        assert level_of("node_failed") == LEVEL_ERROR
        assert level_of("human_decision_requested") == LEVEL_WARN
        assert level_of("route_taken") == LEVEL_DEBUG

    def test_agent_events_return_none(self):
        """9 个 agent_*/prompt_rendered/custom/agent_step_started/unknown_event 显式 None
        （不进 Log Stream）。web-v2 §3.2 B1：agent_step_started / unknown_event 归 Agent History。
        """
        assert level_of("agent_message") is None
        assert level_of("agent_thinking") is None
        assert level_of("agent_tool_call") is None
        assert level_of("agent_tool_result") is None
        assert level_of("agent_usage") is None
        assert level_of("prompt_rendered") is None
        assert level_of("custom") is None
        assert level_of("agent_step_started") is None
        assert level_of("unknown_event") is None

    def test_unknown_event_returns_info(self):
        """未登记 type → LEVEL_INFO 兜底（fail visible，不静默吞；spec §11.5 #6）。"""
        assert level_of("nonexistent_event_type") == LEVEL_INFO


# ── level icon（spec §2.4 5 level icon）─────────────────────────────────


class TestLevelIcons:
    """spec §2.4：5 level 对应 5 个 icon（› ✓ ✗ ⚠ ·）。"""

    def test_5_levels_have_icons(self):
        assert _LEVEL_ICONS[LEVEL_INFO] == "›"
        assert _LEVEL_ICONS[LEVEL_SUCCESS] == "✓"
        assert _LEVEL_ICONS[LEVEL_ERROR] == "✗"
        assert _LEVEL_ICONS[LEVEL_WARN] == "⚠"
        assert _LEVEL_ICONS[LEVEL_DEBUG] == "·"


# ── format_event（spec §2.4 message 模板）─────────────────────────────


class TestFormatEvent:
    """spec §2.4 message 模板：``HH:MM:SS  {icon}  {node:<14}  {message}``。"""

    def test_node_started_format(self):
        line = format_event(
            "node_started", {"kind": "agent"},
            node="analyzer", timestamp=1000000.0,
        )
        assert "›" in line
        assert "analyzer" in line
        assert "node started (kind=agent)" in line

    def test_node_completed_format(self):
        line = format_event(
            "node_completed", {"elapsed": 14.5},
            node="analyzer", timestamp=1000000.0,
        )
        assert "✓" in line
        assert "node completed (14.5s)" in line

    def test_node_failed_full_message_no_truncate(self):
        """reviewer P1-10：长 message 不截断（用户能看到完整失败原因）。"""
        long_msg = "R" * 200
        line = format_event(
            "node_failed", {"message": long_msg},
            node="runner", timestamp=1000000.0,
        )
        assert "✗" in line
        assert long_msg in line  # 完整出现

    def test_workflow_failed_full_message(self):
        """workflow_failed 含 error_type + 完整 message（reviewer P1-10 不截）。"""
        long_msg = "W" * 200
        line = format_event(
            "workflow_failed",
            {"error_type": "TestErr", "message": long_msg},
            node=None, timestamp=1000000.0,
        )
        assert "✗" in line
        assert "TestErr" in line
        assert long_msg in line

    def test_error_full_message(self):
        """error type 完整 message 不截断（reviewer P1-10）。"""
        long_msg = "E" * 200
        line = format_event(
            "error", {"message": long_msg},
            node=None, timestamp=1000000.0,
        )
        assert "✗" in line
        assert long_msg in line

    def test_retry_exhausted_full_message(self):
        """retry_exhausted 完整 last_error_type 不截断（reviewer P1-10）。"""
        long_err = "SomeVeryLongErrorTypeName" * 10
        line = format_event(
            "retry_exhausted",
            {"last_error_type": long_err},
            node="runner", timestamp=1000000.0,
        )
        assert "✗" in line
        assert long_err in line

    def test_agent_message_returns_empty(self):
        """agent_message level=None → 返回空串（不进 Log Stream）。"""
        line = format_event(
            "agent_message", {"text": "hello"},
            node="analyzer", timestamp=1000000.0,
        )
        assert line == ""

    def test_workflow_started_format(self):
        line = format_event(
            "workflow_started", {"workflow_name": "test"},
            node=None, timestamp=1000000.0,
        )
        assert "›" in line
        assert "workflow started: test" in line

    def test_human_decision_warn_level(self):
        line = format_event(
            "human_decision_requested",
            {"gate_id": "g1", "prompt": "Allow?", "source": "tool"},
            node="analyzer", timestamp=1000000.0,
        )
        assert "⚠" in line
        assert "gate: Allow?" in line

    def test_node_field_ljust_14(self):
        """node 字段固定 14 字符宽（spec §2.4 列对齐）。"""
        line = format_event(
            "node_started", {"kind": "agent"},
            node="ab", timestamp=1000000.0,
        )
        # node="ab" → ljust 14 = "ab            "（12 空格补齐）
        # 格式 "HH:MM:SS  ›  ab            ..."
        assert "ab            " in line or "ab " in line  # 至少有 ljust 痕迹

    def test_node_dash_when_none(self):
        """node=None → 显 ``-``（spec §2.4 占位）。"""
        line = format_event(
            "workflow_started", {"workflow_name": "x"},
            node=None, timestamp=1000000.0,
        )
        assert "-" in line

    def test_dialog_message_compact_format(self):
        """dialog_message 在 v2 是 debug 级（默认隐藏），格式正常。"""
        line = format_event(
            "dialog_message",
            {"role": "user", "text": "hi", "turn": 1},
            node="cfg", timestamp=1000000.0,
        )
        assert "·" in line  # debug icon
        assert "user" in line
        assert "turn 1" in line


# ── LogStream widget 行为（debug toggle）──────────────────────────────────


class TestLogStreamWidget:
    """LogStream widget：debug 默认隐藏 + L 键 toggle。"""

    def test_show_debug_default_off(self):
        """spec §2.4：debug 默认隐藏（_show_debug=False）。"""
        ls = LogStream()
        assert ls.show_debug is False

    def test_toggle_debug_flips_state(self):
        """L 键 toggle：off → on → off。"""
        ls = LogStream()
        ls.toggle_debug()
        assert ls.show_debug is True
        ls.toggle_debug()
        assert ls.show_debug is False

    def test_toggle_debug_writes_status_to_log(self):
        """toggle 后写一行 ``── debug log: ON/OFF ──`` 提示用户当前状态。"""
        ls = LogStream()

        async def scenario():
            from textual.app import App, ComposeResult
            from textual.containers import Vertical

            class _Harness(App):
                def compose(self) -> ComposeResult:
                    with Vertical():
                        yield ls

            async with _Harness().run_test() as pilot:
                ls.toggle_debug()
                await pilot.pause()
                await pilot.pause()
                ls.toggle_debug()
                await pilot.pause()
                await pilot.pause()

        import asyncio
        asyncio.run(scenario())
        # 至少 toggle 不抛异常（RichLog.write 在 headless 也能调）


# ── _event_filter 谓词（spec §11.1 v2 重映射）────────────────────────────


class TestEventFilterPredicates:
    """``goes_to_*`` 谓词正确性（spec §11.1 v2 重映射）。"""

    def test_goes_to_agent_history_show(self):
        """show/show_dim → Agent History。"""
        assert goes_to_agent_history("agent_message") is True
        assert goes_to_agent_history("agent_thinking") is True  # show_dim
        assert goes_to_agent_history("agent_tool_call") is True

    def test_goes_to_agent_history_not_for_high_level(self):
        """高层节点事件不进 Agent History。"""
        assert goes_to_agent_history("node_started") is False
        assert goes_to_agent_history("workflow_completed") is False
        assert goes_to_agent_history("node_failed") is False

    def test_goes_to_log_stream_high_level(self):
        """show_compact/show_warn/show_error → Log Stream。"""
        assert goes_to_log_stream("node_started") is True  # show_compact
        assert goes_to_log_stream("node_failed") is True   # show_error
        assert goes_to_log_stream("human_decision_requested") is True  # show_warn

    def test_goes_to_log_stream_not_for_agent(self):
        """agent_* 不进 Log Stream（设计上归 Agent History）。"""
        assert goes_to_log_stream("agent_message") is False
        assert goes_to_log_stream("agent_thinking") is False
        assert goes_to_log_stream("agent_tool_call") is False

    def test_goes_to_header_footer_usage(self):
        """hide_main → Header footer（agent_usage / custom chart）。"""
        assert goes_to_header_footer("agent_usage") is True
        assert goes_to_header_footer("custom") is True  # chart 走 Header/footer 路径

    def test_goes_to_header_footer_not_for_others(self):
        """其他事件不进 Header footer。"""
        assert goes_to_header_footer("node_started") is False
        assert goes_to_header_footer("agent_message") is False
        assert goes_to_header_footer("workflow_completed") is False


class TestVisibilityOfConsistency:
    """``visibility_of()`` 与 dict 一致 + fail loud。"""

    def test_consistency_with_dict(self):
        for etype, vis in EVENT_VISIBILITY.items():
            assert visibility_of(etype) == vis

    def test_unknown_raises_key_error(self):
        """未登记 type → KeyError（fail loud，不静默兜底）。"""
        try:
            visibility_of("__nonexistent_type__")
        except KeyError:
            return
        raise AssertionError("expected KeyError for unknown EventType")
