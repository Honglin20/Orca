"""tests/profiles/test_capabilities.py —— ProviderCapabilities frozen + extra="forbid"。

覆盖 SPEC §6.6：frozen（构造后不可变）/ extra="forbid"（未知字段拒绝）/ 7 字段约束。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orca.profiles.capabilities import ProviderCapabilities


def _full_kwargs(**overrides) -> dict:
    kw = dict(
        mcp_tools=True,
        streaming_events=True,
        structured_output="native",
        interrupt=True,
        checkpoint_resume=True,
        usage_tracking=True,
        concurrent_safe=True,
    )
    kw.update(overrides)
    return kw


def test_construct_full_capabilities():
    cap = ProviderCapabilities(**_full_kwargs())
    assert cap.mcp_tools is True
    assert cap.structured_output == "native"
    assert cap.concurrent_safe is True


def test_frozen_immutable():
    """frozen=True：构造后不可变（profile 是契约，不能漂移，SPEC §4.4 / §6.6）。"""
    cap = ProviderCapabilities(**_full_kwargs())
    with pytest.raises((ValidationError, TypeError)):
        cap.mcp_tools = False  # type: ignore[misc]


def test_extra_forbid_rejects_unknown_field():
    """extra='forbid'：未知字段拒绝（fail loud，SPEC §6.6）。"""
    with pytest.raises(ValidationError) as exc:
        ProviderCapabilities(**_full_kwargs(unknown_field="x"))
    assert "unknown_field" in str(exc.value)


def test_structured_output_literal_constraint():
    """structured_output 仅接受 'native' / 'prompt_injection' / 'none'。"""
    for val in ("native", "prompt_injection", "none"):
        cap = ProviderCapabilities(**_full_kwargs(structured_output=val))
        assert cap.structured_output == val
    with pytest.raises(ValidationError):
        ProviderCapabilities(**_full_kwargs(structured_output="bogus"))


def test_all_seven_fields_required():
    """7 个能力字段全部必填（无默认，SPEC §4.4）。"""
    for field in (
        "mcp_tools", "streaming_events", "structured_output", "interrupt",
        "checkpoint_resume", "usage_tracking", "concurrent_safe",
    ):
        kw = _full_kwargs()
        del kw[field]
        with pytest.raises(ValidationError):
            ProviderCapabilities(**kw)


def test_field_types_are_bool_where_appropriate():
    cap = ProviderCapabilities(**_full_kwargs())
    for field in (
        "mcp_tools", "streaming_events", "interrupt",
        "checkpoint_resume", "usage_tracking", "concurrent_safe",
    ):
        assert isinstance(getattr(cap, field), bool)
