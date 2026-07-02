"""tests/exec/test_terminate.py —— TerminateExecutor 单测（terminate step）。

覆盖 INTENT：
  - success status → node_started + node_completed(status=success, outputs=渲染后)
  - failed status → 同上但 status=failed（executor 视角下仍是 node_completed，
    终态分发归 orchestrator）
  - reason Jinja2 渲染（{{ inputs.x }}）
  - outputs Jinja2 渲染（每 key 独立）
  - 空 reason / 空 outputs（向后兼容，默认值）
  - 渲染失败 → node_failed + error 双发（phase=render，fail loud）

executor 不 emit workflow_completed/failed —— 那是 orchestrator 的职责（依赖单向铁律）。
"""

from __future__ import annotations

import asyncio

from orca.exec.context import RunContext
from orca.exec.terminate import TerminateExecutor
from orca.schema import Event, TerminateNode


def _run(coro):
    return asyncio.run(coro)


async def _collect(node, ctx) -> list[Event]:
    exe = TerminateExecutor()
    return [ev async for ev in exe.exec(node, ctx)]


def _ctx(inputs=None, outputs=None) -> RunContext:
    return RunContext(inputs=inputs or {}, outputs=outputs or {}, run_id="r1")


# ── 成功路径 ─────────────────────────────────────────────────────────────────


def test_terminate_success_emits_completed_with_status_success():
    """status=success → node_started + node_completed(status=success)。"""
    node = TerminateNode(name="t", status="success")
    events = _run(_collect(node, _ctx()))
    assert events[0].type == "node_started"
    assert events[0].data["kind"] == "terminate"
    assert events[0].data["status"] == "success"
    completed = events[-1]
    assert completed.type == "node_completed"
    assert completed.data["status"] == "success"
    assert completed.data["reason"] == ""
    assert completed.data["outputs"] == {}


def test_terminate_failed_emits_completed_with_status_failed():
    """status=failed → 仍是 node_completed（executor 视角非失败），data.status=failed。

    关键 INTENT：executor 不判断终态，failed 是 terminate 的业务声明（不是 executor error）。
    orchestrator 据 data.status 分发 workflow_failed{WorkflowTerminated}。
    """
    node = TerminateNode(name="t", status="failed", reason="分类未知")
    events = _run(_collect(node, _ctx()))
    completed = events[-1]
    assert completed.type == "node_completed"
    assert completed.data["status"] == "failed"
    assert completed.data["reason"] == "分类未知"


# ── Jinja2 渲染 ──────────────────────────────────────────────────────────────


def test_terminate_reason_renders_jinja2():
    """reason 是 Jinja2 模板，渲染后写入 node_completed.data.reason。"""
    node = TerminateNode(
        name="t", status="failed", reason="未知类别 {{ inputs.category }}"
    )
    events = _run(_collect(node, _ctx(inputs={"category": "C"})))
    assert events[-1].data["reason"] == "未知类别 C"


def test_terminate_outputs_renders_each_key():
    """outputs 每 key 独立 Jinja2 渲染（同 set_node 机制）。"""
    node = TerminateNode(
        name="t",
        status="success",
        outputs={
            "rejected_category": "{{ inputs.category }}",
            "literal_key": "fixed-value",
        },
    )
    events = _run(_collect(node, _ctx(inputs={"category": "Z"})))
    assert events[-1].data["outputs"] == {
        "rejected_category": "Z",
        "literal_key": "fixed-value",
    }


def test_terminate_outputs_can_reference_upstream_output():
    """terminate.outputs 引用上游 node 的 output（{{ classifier.output.category }}）。"""
    ctx = _ctx(outputs={"classifier": {"output": {"category": "X"}}})
    node = TerminateNode(
        name="t",
        status="failed",
        reason="reject {{ classifier.output.category }}",
        outputs={"cat": "{{ classifier.output.category }}"},
    )
    events = _run(_collect(node, ctx))
    completed = events[-1]
    assert completed.data["reason"] == "reject X"
    assert completed.data["outputs"] == {"cat": "X"}


def test_terminate_empty_reason_and_outputs_default():
    """空 reason / 空 outputs 默认值（向后兼容，无 Jinja2 求值开销）。"""
    node = TerminateNode(name="t", status="success")
    events = _run(_collect(node, _ctx()))
    completed = events[-1]
    assert completed.data["reason"] == ""
    assert completed.data["outputs"] == {}


# ── 渲染失败 fail loud ───────────────────────────────────────────────────────


def test_terminate_render_failure_fail_loud():
    """reason 引用未定义变量 → node_failed(phase=render) + error 双发（fail loud）。"""
    node = TerminateNode(name="t", status="failed", reason="{{ undefined_var }}")
    events = _run(_collect(node, _ctx()))
    failed = [e for e in events if e.type == "node_failed"]
    assert len(failed) == 1
    assert failed[0].data["phase"] == "render"
    assert failed[0].data["error_type"] == "RenderError"
    # error 事件双发（诊断用，SPEC §6）
    assert any(e.type == "error" and e.data["phase"] == "render" for e in events)


def test_terminate_outputs_render_failure_fail_loud():
    """outputs 某 key 渲染失败 → node_failed(phase=render)（与 reason 路径同 fail loud）。"""
    node = TerminateNode(
        name="t",
        status="success",
        outputs={"bad": "{{ no_such_node.output.x }}"},
    )
    events = _run(_collect(node, _ctx()))
    assert any(e.type == "node_failed" and e.data["phase"] == "render" for e in events)


# ── 生命周期 + session_id 一致 ───────────────────────────────────────────────


def test_lifecycle_and_session_id_consistent():
    """单次 exec 所有 Event.session_id 一致（铁律 5）。"""
    node = TerminateNode(name="t", status="success", reason="ok")
    events = _run(_collect(node, _ctx()))
    assert events[0].type == "node_started"
    assert events[-1].type == "node_completed"
    sids = {e.session_id for e in events}
    assert len(sids) == 1
