"""tests/exec/test_set.py —— SetExecutor（Jinja2 求值，SPEC §7.7 / 计划 D.4）。

覆盖：
  - values 求值成功 → node_completed.output={key: 求值结果}
  - 引用 inputs / 上游 output
  - 渲染失败 → node_failed(phase=render)
"""

from __future__ import annotations

import asyncio

import pytest

from orca.exec.context import RunContext
from orca.exec.set_node import SetExecutor
from orca.schema import Event, SetNode


def _run(coro):
    return asyncio.run(coro)


async def _collect(node, ctx) -> list[Event]:
    exe = SetExecutor()
    return [ev async for ev in exe.exec(node, ctx)]


def _ctx(inputs=None, outputs=None) -> RunContext:
    return RunContext(inputs=inputs or {}, outputs=outputs or {}, run_id="r1")


# ── 求值成功 ─────────────────────────────────────────────────────────────────


def test_set_evaluates_values():
    node = SetNode(name="st", values={"a": "{{ inputs.x }}", "b": "literal"})
    events = _run(_collect(node, _ctx(inputs={"x": 42})))
    assert events[0].type == "node_started"
    completed = events[-1]
    assert completed.type == "node_completed"
    assert completed.data["output"] == {"a": "42", "b": "literal"}


def test_set_references_upstream_output():
    """set 引用上游 node 的 output（{{ finder.output.found }}，SPEC §7.9）。"""
    ctx = _ctx(outputs={"finder": {"output": {"found": 7}}})
    node = SetNode(name="st", values={"count": "{{ finder.output.found }}"})
    events = _run(_collect(node, ctx))
    assert events[-1].data["output"] == {"count": "7"}


def test_set_mixed_literal_and_template():
    node = SetNode(
        name="st",
        values={"name": "orca", "iter": "{{ inputs.i }}", "flag": "on"},
    )
    events = _run(_collect(node, _ctx(inputs={"i": 3})))
    assert events[-1].data["output"] == {"name": "orca", "iter": "3", "flag": "on"}


# ── 渲染失败 fail loud ───────────────────────────────────────────────────────


def test_set_render_failure_fail_loud():
    """某个 value 引用未定义变量 → node_failed(phase=render)（SPEC §6 / §7.7）。"""
    node = SetNode(name="st", values={"a": "{{ undefined_var }}"})
    events = _run(_collect(node, _ctx()))
    failed = [e for e in events if e.type == "node_failed"]
    assert len(failed) == 1
    assert failed[0].data["phase"] == "render"
    assert failed[0].data["error_type"] == "RenderError"
    # error 事件双发
    assert any(e.type == "error" and e.data["phase"] == "render" for e in events)


# ── 生命周期 + session_id 一致 ───────────────────────────────────────────────


def test_lifecycle_and_session_id_consistent():
    node = SetNode(name="st", values={"a": "1"})
    events = _run(_collect(node, _ctx()))
    assert events[0].type == "node_started"
    assert events[-1].type == "node_completed"
    sids = {e.session_id for e in events}
    assert len(sids) == 1
