"""test_event_visibility.py —— EVENT_VISIBILITY 表完整性 + 派发语义测试。

spec v2 §11.1 + §2.4 acceptance criteria：
  - 完整性测试（``test_event_visibility_completeness``）：用 ``EventType.__args__``
    逐一断言覆盖；漏 EventType → 立即 fail（fail loud）。
  - 新增 EventType 守卫：CI 跑完整性测试，schema 加新 type 但忘加 visibility 时自动 fail。
  - 派发语义：每个 visibility tag 都至少有 1 个 EventType（无孤儿 tag）。

v2 重映射（spec §11.1）：
  - ``show`` / ``show_dim`` → Agent History（业务核心：message/tool_call/...）
  - ``show_compact`` / ``show_warn`` / ``show_error`` → Log Stream（高层节点事件）
  - ``hide_main`` → Header footer（agent_usage）或 ChartPanel 路径（custom chart）
  - ``hide_all`` → 仅 tape（prompt_rendered）
"""

from __future__ import annotations

import typing

import pytest

from orca.iface.cli.widgets._event_filter import (
    EVENT_VISIBILITY,
    Visibility,
    visibility_of,
)
from orca.schema.event import EventType


class TestEventVisibilityCompleteness:
    """spec v1.1 §6.4：全 32 EventType 必须在 EVENT_VISIBILITY 表里登记。"""

    def test_event_visibility_completeness(self):
        """遍历 EventType Literal 全集，逐一断言 EVENT_VISIBILITY 覆盖。

        fail loud：schema 加新 type 但漏 mapping → 本测试立即 fail（消费者拿到 KeyError
        会让事件凭空消失，比 silent fallback 更可观测）。
        """
        all_types = typing.get_args(EventType)
        assert len(all_types) > 0  # sanity：Literal 非空
        missing = [t for t in all_types if t not in EVENT_VISIBILITY]
        assert missing == [], (
            f"以下 EventType 未在 EVENT_VISIBILITY 登记（消费者会 KeyError，事件凭空消失）："
            f"{missing}"
        )

    def test_no_extra_keys_beyond_event_type(self):
        """反向守门：EVENT_VISIBILITY 不能有 EventType 之外的 key（防拼写错位 drift）。"""
        all_types = set(typing.get_args(EventType))
        extras = [k for k in EVENT_VISIBILITY if k not in all_types]
        assert extras == [], (
            f"EVENT_VISIBILITY 含 EventType 之外的 key（拼写错或 schema 删 type 未清理）："
            f"{extras}"
        )

    def test_every_visibility_tag_has_at_least_one_event(self):
        """spec §6.4：7 个 visibility tag 每个都至少有 1 个 EventType（无孤儿 tag）。

        消费者按 tag 派发；某 tag 0 命中 = 死代码，应清理或重新归类。
        """
        used_tags: dict[str, list[str]] = {}
        for etype, vis in EVENT_VISIBILITY.items():
            used_tags.setdefault(vis, []).append(etype)
        # 7 个声明的 tag（Literal 全集）
        all_tags = set(typing.get_args(Visibility))
        unused = all_tags - set(used_tags.keys())
        assert unused == set(), (
            f"以下 visibility tag 无任何 EventType 使用（孤儿 tag，消费者死代码）：{unused}"
        )


class TestVisibilityOf:
    """``visibility_of`` 函数：fail loud + 与 dict 一致。"""

    def test_known_type_returns_value(self):
        assert visibility_of("agent_message") == "show"
        assert visibility_of("agent_usage") == "hide_main"
        assert visibility_of("prompt_rendered") == "hide_all"
        assert visibility_of("node_failed") == "show_error"

    def test_unknown_type_raises_key_error(self):
        """fail loud：未知 type 抛 KeyError（不静默 fallback 到 hide_all/show_compact）。"""
        with pytest.raises(KeyError):
            visibility_of("__nonexistent_type__")

    def test_consistency_with_dict(self):
        """``visibility_of`` 与直接 dict index 一致（DRY：函数只是统一访问点）。"""
        for etype, vis in EVENT_VISIBILITY.items():
            assert visibility_of(etype) == vis


class TestNoiseGovernanceAssignments:
    """spec §6.1 / §6.2 关键事件归位锁定（防 drift）。"""

    def test_prompt_rendered_is_hide_all(self):
        """§6.1：prompt_rendered 仅 tape，TUI 完全不显示。"""
        assert EVENT_VISIBILITY["prompt_rendered"] == "hide_all"

    def test_agent_usage_is_hide_main(self):
        """§6.2：agent_usage 收敛到 Header footer（不进 Activity Stream 主流）。"""
        assert EVENT_VISIBILITY["agent_usage"] == "hide_main"

    def test_error_classes_are_show_error(self):
        """§6.3：error/failed/exhausted → Log Stream + Red 强调。"""
        assert EVENT_VISIBILITY["error"] == "show_error"
        assert EVENT_VISIBILITY["node_failed"] == "show_error"
        assert EVENT_VISIBILITY["workflow_failed"] == "show_error"
        assert EVENT_VISIBILITY["retry_exhausted"] == "show_error"

    def test_warn_classes_are_show_warn(self):
        """gate/interrupt/retry_started/validator_failed → Log Stream + Amber 强调。"""
        assert EVENT_VISIBILITY["human_decision_requested"] == "show_warn"
        assert EVENT_VISIBILITY["interrupt_requested"] == "show_warn"
        assert EVENT_VISIBILITY["retry_started"] == "show_warn"
        assert EVENT_VISIBILITY["validator_failed"] == "show_warn"  # v2：与 EVENT_LEVEL 对齐

    def test_business_core_is_show(self):
        """v2 §11.1：业务核心事件 → Agent History（show/show_dim）。"""
        assert EVENT_VISIBILITY["agent_message"] == "show"
        assert EVENT_VISIBILITY["agent_tool_call"] == "show"
        assert EVENT_VISIBILITY["agent_tool_result"] == "show"

    def test_custom_goes_to_chart_panel_not_log_stream(self):
        """v2 §11.1：custom(chart) 走 NodeDetail ChartPanel 路径（hide_main），
        不进 Log Stream / Agent History。"""
        assert EVENT_VISIBILITY["custom"] == "hide_main"

    def test_dialog_message_goes_to_log_stream(self):
        """v2 §11.1：dialog_message 是 debug 级事件（→ Log Stream 默认隐藏）。"""
        assert EVENT_VISIBILITY["dialog_message"] == "show_compact"

    def test_thinking_is_show_dim(self):
        """thinking 弱化（dim）但仍可见。"""
        assert EVENT_VISIBILITY["agent_thinking"] == "show_dim"
