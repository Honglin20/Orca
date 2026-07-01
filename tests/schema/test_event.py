"""tests/schema/test_event.py —— Event 构造与 EventType Literal 约束。

覆盖 SPEC §7.3：合法 type 构造、非法 type 被拒、全部 type 覆盖。
用 typing.get_args 遍历 EventType，覆盖意图而非硬编码数量。
"""

import typing

import pytest
from pydantic import ValidationError

from orca.schema import Event, EventType


def test_event_construct():
    e = Event(type="workflow_started", seq=1, timestamp=0.0)
    assert e.seq == 1
    assert e.type == "workflow_started"
    assert e.timestamp == 0.0
    assert e.node is None
    assert e.data == {}


def test_event_with_payload():
    e = Event(
        type="node_completed",
        seq=5,
        timestamp=123.0,
        node="trainer",
        data={"elapsed": 1.2, "output": {"score": 0.9}},
    )
    assert e.node == "trainer"
    assert e.data["output"]["score"] == 0.9


def test_event_invalid_type_rejected():
    with pytest.raises(ValidationError):
        Event(type="nonexistent", seq=1, timestamp=0.0)


def test_event_extra_forbid():
    with pytest.raises(ValidationError):
        Event(type="error", seq=1, timestamp=0.0, bogus=1)


def test_all_event_types_construct():
    """SPEC §7.3：所有 event type 都能构造。遍历 Literal 全集，不漏一个。"""
    types = typing.get_args(EventType)
    assert len(types) > 0
    for i, t in enumerate(types):
        e = Event(type=t, seq=i, timestamp=float(i))
        assert e.type == t


def test_event_type_count_matches_spec_literal():
    """SPEC §3.2 的 Literal 代码块实际 22 个值（含 phase-10 新增的 workflow_cancelled）。

    显式断言数量，防止后续误删/误增 type 而不自知（fail loud）。
    """
    assert len(typing.get_args(EventType)) == 22
