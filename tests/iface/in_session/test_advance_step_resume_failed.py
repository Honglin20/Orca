"""tests/iface/in_session/test_advance_step_resume_failed.py —— resume failed run SPEC 验收。

覆盖 SPEC ``docs/specs/2026-08-11-resume-failed-and-configurable-escalation.md``：
  - AC1：可配升格（recoverable_max_attempts=2 → 2 次升格；默认 20）。
  - AC2：默认 20（无 yaml 字段 → wf.recoverable_max_attempts == 20）。
  - AC3：resume 触发（failed run advance_step → resumed=True + emits [workflow_resumed,
    node_started] + prompt 含失败历史）。
  - AC5：计数清零（resume 后 consecutive_fail_count == 0）。
  - AC6：终态保留（cancelled/completed → done=True + emits==[]，不 resume）。
  - AC7：无可定位节点（failed 但 current_node None / 非 agent → failed_no_resumable_node）。

测试路径：``advance_step(... prompts_dir=None)`` inline 是单测主路径（决策逻辑），
项目惯例：``asyncio.run``（无 pytest-asyncio，对齐 test_error_management.py）。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from orca.events.bus import EventBus
from orca.events.replay import replay_state
from orca.events.tape import Tape
from orca.iface.in_session._step_io import apply_step_result
from orca.run.step import (
    InSessionError,
    StepResult,
    advance_step,
    consecutive_fail_count,
)
from orca.schema.workflow import AgentNode, Route, Workflow


# ── fixtures / helpers ─────────────────────────────────────────────────────


def _wf_with_schema(max_attempts: int = 3) -> Workflow:
    """单节点 agent wf（a → $end），a 声明 output_schema 要求 {k: string}。

    ``max_attempts`` 参数控制 ``recoverable_max_attempts``（SPEC 2026-08-11 §1.1）。
    """
    return Workflow(
        name="resume_test_wf",
        entry="a",
        recoverable_max_attempts=max_attempts,
        nodes=[
            AgentNode(
                name="a",
                executor="opencode",
                model="d/d",
                prompt="do A",
                output_schema={"type": "object", "required": ["k"],
                               "properties": {"k": {"type": "string"}}},
                routes=[Route(to="$end")],
            )
        ],
    )


def _wf_no_schema(max_attempts: int = 3) -> Workflow:
    """单节点 agent wf（a → $end），a 无 output_schema。"""
    return Workflow(
        name="resume_test_no_schema_wf",
        entry="a",
        recoverable_max_attempts=max_attempts,
        nodes=[
            AgentNode(
                name="a",
                executor="opencode",
                model="d/d",
                prompt="do A freely",
                routes=[Route(to="$end")],
            )
        ],
    )


def _apply(bus: EventBus, result: StepResult, wf: Workflow) -> None:
    """asyncio.run 包装：把 StepResult.emits 落 tape。"""
    asyncio.run(apply_step_result(bus, result, wf=wf, run_id="r-test"))


def _new_run(tmp_path: Path, wf: Workflow) -> tuple[Tape, EventBus]:
    """bootstrap 一个 wf（advance_step inline），返 (tape, bus)。"""
    tape = Tape(tmp_path / "tape.jsonl", run_id="r-test", resume=True)
    bus = EventBus(tape)
    r0 = advance_step(tape, wf, run_id="r-test", prompts_dir=None)
    _apply(bus, r0, wf)
    return tape, bus


def _escalate_to_failed(tape: Tape, bus: EventBus, wf: Workflow, bad_output: str = "BAD") -> None:
    """连续坏 output 撞 recoverable_max_attempts → workflow_failed 落 tape。"""
    max_att = wf.recoverable_max_attempts
    for _ in range(max_att):
        r = advance_step(tape, wf, output=bad_output, run_id="r-test", prompts_dir=None)
        _apply(bus, r, wf)


def _write_tape_raw(path: Path, events: list[dict]) -> Tape:
    """从原始 event dict 列表构造 tape（手写 tape fixture 用）。seq 自动递增。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for seq, evt in enumerate(events, start=1):
        evt = {**evt, "seq": seq, "timestamp": 0.0,
               "session_id": evt.get("session_id"), "node": evt.get("node")}
        lines.append(json.dumps(evt, ensure_ascii=False))
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return Tape(path, run_id="r-test", resume=True)


# ── AC3：resume 触发 ────────────────────────────────────────────────────────


def test_resume_failed_run_emits_workflow_resumed_and_node_started(tmp_path):
    """AC3：failed run 调 advance_step（无 output）→ resumed=True + emits=[workflow_resumed,
    node_started(<失败节点>)] + prompt 含失败历史。"""
    wf = _wf_with_schema(max_attempts=3)
    tape, bus = _new_run(tmp_path, wf)
    _escalate_to_failed(tape, bus, wf)

    # 确认 failed 终态
    assert replay_state(tape).status == "failed"

    # resume
    result = advance_step(tape, wf, run_id="r-test", prompts_dir=None)
    assert result.done is False
    assert result.resumed is True
    assert result.node == "a"
    assert result.reason == "recovered_from_failure"
    assert result.retry_count == 0
    assert result.retry_budget == 3  # wf.recoverable_max_attempts
    types = [e.type for e in result.emits]
    assert types == ["workflow_resumed", "node_started"]
    assert result.emits[0].data["resumed_node"] == "a"
    assert result.emits[0].data["reason"] == "recovered_from_failure"
    assert result.emits[1].node == "a"
    # prompt 含失败历史（3 次升格的历次失败）
    assert result.prompt is not None
    assert "前序尝试失败" in result.prompt


def test_resume_failed_run_advances_after_correct_output(tmp_path):
    """AC3 续（AC9 等价单测）：resume 后喂合法 output → node_completed → workflow_completed。"""
    wf = _wf_with_schema(max_attempts=3)
    tape, bus = _new_run(tmp_path, wf)
    _escalate_to_failed(tape, bus, wf)

    # resume
    r_resume = advance_step(tape, wf, run_id="r-test", prompts_dir=None)
    assert r_resume.resumed
    _apply(bus, r_resume, wf)

    # 喂合法 output → node_completed → $end
    r_done = advance_step(tape, wf, output='{"k": "v"}', run_id="r-test", prompts_dir=None)
    assert r_done.done is True
    assert r_done.reason == "completed"
    types = [e.type for e in r_done.emits]
    assert "node_completed" in types and "workflow_completed" in types


# ── AC5：计数清零 ────────────────────────────────────────────────────────────


def test_consecutive_failures_reset_on_resume(tmp_path):
    """AC5：resume 后 consecutive_fail_count == 0（workflow_resumed reset 边界）。

    手动写 tape 含 [ws, ns(a), nf(a)×2, wf_failed(a)] → resume emits [workflow_resumed,
    node_started] 落 tape → consecutive_fail_count == 0。
    """
    wf = _wf_with_schema(max_attempts=2)
    tape, bus = _new_run(tmp_path, wf)
    _escalate_to_failed(tape, bus, wf)

    # 升格前有失败记录
    assert consecutive_fail_count(tape, "a") > 0

    # resume 并落 tape
    r = advance_step(tape, wf, run_id="r-test", prompts_dir=None)
    assert r.resumed
    _apply(bus, r, wf)

    # workflow_resumed reset 边界生效 → 计数清零
    assert consecutive_fail_count(tape, "a") == 0


# ── AC1/AC2：可配升格 ──────────────────────────────────────────────────────


def test_recoverable_max_attempts_config_low_threshold(tmp_path):
    """AC1：recoverable_max_attempts=2 → 2 次连续失败升格（而非默认 20 次）。"""
    wf = _wf_with_schema(max_attempts=2)
    tape, bus = _new_run(tmp_path, wf)

    # 1st bad → recoverable
    r1 = advance_step(tape, wf, output="BAD1", run_id="r-test", prompts_dir=None)
    assert r1.recoverable and not r1.done
    assert r1.retry_count == 1
    _apply(bus, r1, wf)

    # 2nd bad → 升格（threshold=2）
    r2 = advance_step(tape, wf, output="BAD2", run_id="r-test", prompts_dir=None)
    assert r2.done is True
    assert not r2.recoverable
    types = [e.type for e in r2.emits]
    assert "workflow_failed" in types


def test_recoverable_max_attempts_config_one_failure_survives(tmp_path):
    """AC1 续：recoverable_max_attempts=2 → 1 次失败不升格（run 存活，retry_count=1）。"""
    wf = _wf_with_schema(max_attempts=2)
    tape, bus = _new_run(tmp_path, wf)

    r1 = advance_step(tape, wf, output="BAD", run_id="r-test", prompts_dir=None)
    assert r1.recoverable and not r1.done
    assert r1.retry_budget == 1  # 2 - 1 = 1
    _apply(bus, r1, wf)
    assert replay_state(tape).status == "running"


def test_recoverable_max_attempts_default_is_20():
    """AC2：无 yaml 字段 → wf.recoverable_max_attempts == 20（默认值）。"""
    wf = Workflow(
        name="default_threshold_wf",
        entry="a",
        nodes=[
            AgentNode(name="a", executor="opencode", model="d/d",
                      prompt="do A", routes=[Route(to="$end")]),
        ],
    )
    assert wf.recoverable_max_attempts == 20


def test_recoverable_max_attempts_ge_1_validated():
    """AC1 边界：recoverable_max_attempts=0 → pydantic 校验失败（ge=1 fail loud）。"""
    with pytest.raises(ValidationError):
        Workflow(
            name="zero_threshold_wf",
            entry="a",
            recoverable_max_attempts=0,
            nodes=[
                AgentNode(name="a", executor="opencode", model="d/d",
                          prompt="do A", routes=[Route(to="$end")]),
            ],
        )


# ── AC6：终态保留（cancelled / completed 不可 resume）─────────────────────


def test_cancelled_run_not_resumed(tmp_path):
    """AC6：cancelled run → advance_step → done=True + emits==[]（不 resume）。"""
    tape = _write_tape_raw(tmp_path / "tape.jsonl", [
        {"type": "workflow_started", "data": {"workflow_name": "wf"}},
        {"type": "node_started", "node": "a", "data": {}},
        {"type": "workflow_cancelled", "data": {}},
    ])
    wf = _wf_with_schema()
    result = advance_step(tape, wf, run_id="r-test", prompts_dir=None)
    assert result.done is True
    assert result.emits == []
    assert "already_cancelled" in (result.reason or "")


def test_completed_run_not_resumed(tmp_path):
    """AC6：completed run → advance_step → done=True + emits==[]（不 resume）。"""
    tape = _write_tape_raw(tmp_path / "tape.jsonl", [
        {"type": "workflow_started", "data": {"workflow_name": "wf"}},
        {"type": "node_started", "node": "a", "data": {}},
        {"type": "node_completed", "node": "a", "data": {"output": "ok"}},
        {"type": "workflow_completed", "data": {}},
    ])
    wf = _wf_with_schema()
    result = advance_step(tape, wf, run_id="r-test", prompts_dir=None)
    assert result.done is True
    assert result.emits == []
    assert "already_completed" in (result.reason or "")


# ── AC7：无可定位失败节点 ──────────────────────────────────────────────────


def test_resume_failed_no_resumable_node_current_none(tmp_path):
    """AC7：failed 但 current_node=None（workflow 级失败 node=None）→ failed_no_resumable_node。

    手写 tape 含 workflow_failed(node=None) 且无 node_started → reducer 不覆盖 current_node
    → current_node 保持 None → resume 无法定位节点 → done=True。
    """
    tape = _write_tape_raw(tmp_path / "tape.jsonl", [
        {"type": "workflow_started", "data": {"workflow_name": "wf"}},
        {"type": "workflow_failed", "node": None, "data": {"kind": "internal_error"}},
    ])
    state = replay_state(tape)
    assert state.status == "failed"
    assert state.current_node is None

    wf = _wf_with_schema()
    result = advance_step(tape, wf, run_id="r-test", prompts_dir=None)
    assert result.done is True
    assert result.reason == "failed_no_resumable_node"
    assert result.emits == []


def test_resume_failed_prompt_contains_failure_history(tmp_path):
    """AC3 细节：resume 后 prompt 含升格前的历次失败（让 agent 知历史以自我纠正）。

    SPEC §2.3 首次 resume 例外：retry_count=0 但 failure_history 非空。
    """
    wf = _wf_with_schema(max_attempts=3)
    tape, bus = _new_run(tmp_path, wf)

    # 3 次坏 output → 升格（prompt 应含 3 条失败历史）
    for i in range(3):
        r = advance_step(
            tape, wf, output=f"BAD{i}", run_id="r-test", prompts_dir=None,
        )
        _apply(bus, r, wf)

    assert replay_state(tape).status == "failed"

    # resume
    result = advance_step(tape, wf, run_id="r-test", prompts_dir=None)
    assert result.resumed
    assert result.retry_count == 0  # SPEC §2.3：计数重开
    # prompt 含 3 条历次失败（SPEC §2.3 首次 resume 例外：retry_count=0 但历史非空）
    assert result.prompt is not None
    assert "前序尝试失败" in result.prompt
    assert "Attempt 1" in result.prompt
    assert "Attempt 2" in result.prompt
    assert "Attempt 3" in result.prompt


# ── AC11：irrecoverable resume 短路（SPEC-REVIEW E2）───────────────────────


def test_resume_failed_irrecoverable_render_error_raises(tmp_path):
    """AC11（SPEC-REVIEW E2）：render_error 终态的 run resume → advance_step resume 分支
    调 _deliver → _render_or_fail 抛 InSessionError(render_error) → [workflow_resumed,
    node_started] emits **未返回**（advance_step emit-only 纯函数 raise 时 emits 丢弃）。

    断言点：InSessionError 抛出 + error_kind=render_error（emits 从未到达调用方）。
    """
    # 构造 wf：a 的 prompt 引用不存在的上游字段（render_error）
    wf = Workflow(
        name="irrecoverable_resume_wf",
        entry="a",
        recoverable_max_attempts=2,
        nodes=[
            AgentNode(
                name="a",
                executor="opencode",
                model="d/d",
                prompt="基于 {{ nonexistent.field }} 渲染",
                routes=[Route(to="$end")],
            ),
        ],
    )
    # 手写 failed tape：a 曾 started 但 render_error 导致 workflow_failed(node=a)
    tape = _write_tape_raw(tmp_path / "tape.jsonl", [
        {"type": "workflow_started", "data": {"workflow_name": "irrecoverable_resume_wf"}},
        {"type": "node_started", "node": "a", "data": {}},
        {"type": "workflow_failed", "node": "a",
         "data": {"kind": "render_error", "message": "UndefinedError: nonexistent"}},
    ])
    state = replay_state(tape)
    assert state.status == "failed"
    assert state.current_node == "a"

    # resume → advance_step 走 resume 分支 → _deliver → _render_or_fail → raise
    with pytest.raises(InSessionError) as exc_info:
        advance_step(tape, wf, run_id="r-test", prompts_dir=None)
    assert exc_info.value.error_kind == "render_error"
    # 关键：advance_step raise → [workflow_resumed, node_started] emits 从未返回
    # （emit-only 纯函数：raise 时局部 emits list 被丢弃，apply_step_result 不被调）

