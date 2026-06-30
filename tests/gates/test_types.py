"""test_types.py —— HumanGate 原语（SPEC §1 / 计划 G1.2）。"""

from __future__ import annotations

import dataclasses

import pytest

from orca.gates.types import HumanGate


def test_construct_tool_permission():
    """两个 source 共用同一模型，仅 context 内容不同（SPEC §1.1 §7.1）。"""
    gate = HumanGate(
        id="g1",
        prompt="批准 Bash 调用？",
        options=["allow", "deny"],
        context={"tool": "Bash", "tool_input": {"command": "ls"}},
        source="tool_permission",
        run_id="run-1",
        node="review",
    )
    assert gate.id == "g1"
    assert gate.source == "tool_permission"
    assert gate.options == ["allow", "deny"]
    # timeout_hint 默认 None（SPEC §1.1）
    assert gate.timeout_hint is None


def test_construct_agent_ask_free_text():
    """agent_ask source + options=None（自由文本，SPEC §1.2 第二行）。"""
    gate = HumanGate(
        id="g2",
        prompt="需要数据库连接串",
        options=None,
        context={"question": "db url"},
        source="agent_ask",
        run_id="run-1",
        node=None,  # workflow 级（SPEC §1.1 node 可 None）
    )
    assert gate.source == "agent_ask"
    assert gate.options is None
    assert gate.node is None


def test_frozen_immutable():
    """frozen dataclass：构造后不可变（多壳并发读无 race，SPEC §1.1）。"""
    gate = HumanGate(
        id="g3",
        prompt="p",
        options=["a"],
        context={},
        source="tool_permission",
        run_id="r",
        node="n",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        gate.prompt = "mutated"  # type: ignore[misc]


def test_timeout_hint_optional():
    """timeout_hint 可显式给（壳的 UI 提示，非强制，SPEC §1.1）。"""
    gate = HumanGate(
        id="g4",
        prompt="p",
        context={},
        source="agent_ask",
        run_id="r",
        node="n",
        timeout_hint=30.0,
    )
    assert gate.timeout_hint == 30.0
