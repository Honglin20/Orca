"""tests/iface/in_session/test_error_management.py —— in-session 错误管理 SPEC 验收守门。

覆盖 SPEC ``docs/specs/2026-07-23-in-session-error-management.md``（定稿 v2）的可测阻断项：
  - AC9：``consecutive_fail_count`` 4 fixture（简单连续 / 被他节点 nc 重置 /
    被同节点 nc 重置 / 跨 workflow_started 边界）。
  - AC1/AC2/AC5：``advance_step`` recoverable 分支（重 arm + 连续 N 次升格 emit 顺序）。
  - AC10：render_error 全 irrecoverable 回归（不 emit [nf, ns]）。
  - AC8：单测 + grep 守门（RecoverableInSessionError）。

测试路径：``advance_step(... prompts_dir=None)`` inline 是单测主路径（决策逻辑），
项目惯例：``asyncio.run``（无 pytest-asyncio，对齐 tests/iface/in_session/test_node_memory.py）。
"""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import pytest

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.iface.in_session._step_io import apply_step_result
from orca.run import step as step_mod
from orca.run.step import (
    END,
    ERR_OUTPUT_SCHEMA_MISMATCH,
    InSessionError,
    RecoverableInSessionError,
    StepResult,
    advance_step,
    consecutive_fail_count,
)
from orca.schema.workflow import AgentNode, Route, Workflow


# ── fixtures / helpers ─────────────────────────────────────────────────────


def _wf_with_schema() -> Workflow:
    """单节点 agent wf（a → $end），a 声明 output_schema 要求 {k: string}。"""
    return Workflow(
        name="err_mgmt_wf",
        entry="a",
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


def _two_node_wf() -> Workflow:
    """两节点线性 wf（a → b → $end），**两节点都有 schema**（便于构造「他节点 nc 重置」
    + 多节点 recoverable 集成测试 —— b 也有 schema 才能让 b 也触发 output_schema_mismatch）。"""
    return Workflow(
        name="err_mgmt_two_wf",
        entry="a",
        nodes=[
            AgentNode(
                name="a",
                executor="opencode",
                model="d/d",
                prompt="do A",
                output_schema={"type": "object", "required": ["k"],
                               "properties": {"k": {"type": "string"}}},
                routes=[Route(to="b")],
            ),
            AgentNode(
                name="b",
                executor="opencode",
                model="d/d",
                prompt="do B",
                output_schema={"type": "object", "required": ["k"],
                               "properties": {"k": {"type": "string"}}},
                routes=[Route(to="$end")],
            ),
        ],
    )


def _write_tape(path: Path, events: list[tuple[str, str | None]]) -> Tape:
    """从 (type, node) 序列构造 tape（单测 fixture 用）。

    每条事件最小合法字段（seq 自动递增；data 空 dict；node 可 None）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for seq, (etype, node) in enumerate(events, start=1):
        lines.append(json.dumps({
            "seq": seq, "type": etype, "timestamp": 0.0,
            "node": node, "session_id": None, "data": {},
        }, ensure_ascii=False))
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return Tape(path, run_id="r-test", resume=True)


def _tape_of(path: Path) -> Tape:
    return Tape(path, run_id="r-test", resume=True)


# ── AC9：consecutive_fail_count 4 fixture ──────────────────────────────────


def test_consecutive_fail_count_simple_run(tmp_path):
    """fixture (i)：简单连续 node_failed(a) → count = 2（无 node_completed 重置）。"""
    tape = _write_tape(tmp_path / "t.jsonl", [
        ("workflow_started", None),
        ("node_started", "a"),
        ("node_failed", "a"),
        ("node_started", "a"),
        ("node_failed", "a"),
        ("node_started", "a"),
    ])
    assert consecutive_fail_count(tape, "a") == 2


def test_consecutive_fail_count_reset_by_other_node_completed(tmp_path):
    """fixture (ii)：被他节点 node_completed 重置 → 只计末尾 streak。"""
    tape = _write_tape(tmp_path / "t.jsonl", [
        ("workflow_started", None),
        ("node_started", "a"),
        ("node_failed", "a"),       # 旧 streak
        ("node_started", "a"),
        ("node_completed", "b"),    # 他节点 nc → 重置
        ("node_started", "a"),
        ("node_failed", "a"),       # 新 streak（=1）
        ("node_started", "a"),
    ])
    assert consecutive_fail_count(tape, "a") == 1


def test_consecutive_fail_count_reset_by_same_node_completed(tmp_path):
    """fixture (iii)：被同节点 node_completed 重置 → 计 nc 之后的 streak。"""
    tape = _write_tape(tmp_path / "t.jsonl", [
        ("workflow_started", None),
        ("node_started", "a"),
        ("node_failed", "a"),
        ("node_failed", "a"),       # 旧 streak（2 次，已被 nc 截断）
        ("node_started", "a"),
        ("node_completed", "a"),    # 同节点 nc → 重置
        ("node_started", "a"),
        ("node_failed", "a"),       # 新 streak（=1）
        ("node_started", "a"),
    ])
    assert consecutive_fail_count(tape, "a") == 1


def test_consecutive_fail_count_crosses_workflow_started_boundary(tmp_path):
    """fixture (iv)：跨 workflow_started 边界 —— ws 不是重置点，count 持续。"""
    tape = _write_tape(tmp_path / "t.jsonl", [
        ("workflow_started", None),
        ("node_started", "a"),
        ("node_failed", "a"),
        ("node_started", "a"),
        ("node_failed", "a"),
        ("node_started", "a"),
    ])
    # ws 不重置（不是 node_completed）→ 连续 2 次
    assert consecutive_fail_count(tape, "a") == 2


def test_consecutive_fail_count_zero_when_no_failures(tmp_path):
    """无 node_failed → count = 0。"""
    tape = _write_tape(tmp_path / "t.jsonl", [
        ("workflow_started", None),
        ("node_started", "a"),
    ])
    assert consecutive_fail_count(tape, "a") == 0


def test_consecutive_fail_count_only_counts_current_node(tmp_path):
    """只计 node_failed(current_node)，他节点 failed 不计。"""
    tape = _write_tape(tmp_path / "t.jsonl", [
        ("workflow_started", None),
        ("node_started", "a"),
        ("node_failed", "b"),   # 他节点 failed，不计
        ("node_failed", "a"),   # 计
        ("node_started", "a"),
    ])
    assert consecutive_fail_count(tape, "a") == 1


# ── AC1/AC2/AC5：advance_step recoverable 分支 + 升格 ──────────────────────


def _apply(bus: EventBus, result: StepResult, wf: Workflow) -> None:
    """asyncio.run 包装：把 StepResult.emits 落 tape（项目惯例，无 pytest-asyncio）。"""
    asyncio.run(apply_step_result(bus, result, wf=wf, run_id="r-test"))


def _new_run(tmp_path: Path, wf: Workflow) -> tuple[Tape, EventBus, StepResult]:
    """bootstrap 一个 wf（advance_step inline），返 (tape, bus, entry result)。"""
    tape = Tape(tmp_path / "tape.jsonl", run_id="r-test", resume=True)
    bus = EventBus(tape)
    r0 = advance_step(tape, wf, run_id="r-test", prompts_dir=None)
    _apply(bus, r0, wf)
    return tape, bus, r0


def test_recoverable_output_schema_mismatch_re_arms(tmp_path):
    """AC1：output 非 JSON → recoverable，run 存活，重 arm 同节点。

    StepResult：``recoverable=True, done=False, retry_count=1, retry_budget=2``；
    emits = ``[node_failed(a), node_started(a)]``（无 workflow_failed）。
    """
    wf = _wf_with_schema()
    tape, bus, _ = _new_run(tmp_path, wf)

    result = advance_step(tape, wf, output="NOT_JSON", run_id="r-test", prompts_dir=None)

    assert result.done is False                     # run 存活
    assert result.recoverable is True
    assert result.node == "a"                       # 重 arm 同节点
    assert result.retry_count == 1
    assert result.retry_budget == 2
    assert result.error_kind == ERR_OUTPUT_SCHEMA_MISMATCH
    assert result.reason and ("output_schema" in result.reason or "非 JSON" in result.reason)
    assert result.hint and "反馈" in result.hint
    # emits：[nf, ns]，无 workflow_failed（未升格）
    types = [e.type for e in result.emits]
    assert types == ["node_failed", "node_started"]
    assert all(e.node == "a" for e in result.emits)
    # node_failed data 4 字段（SPEC §4.2 E6）
    nf_data = result.emits[0].data
    assert nf_data["kind"] == ERR_OUTPUT_SCHEMA_MISMATCH
    assert nf_data["error_type"] == ERR_OUTPUT_SCHEMA_MISMATCH
    assert "message" in nf_data and "phase" in nf_data


def test_recoverable_then_correct_output_advances(tmp_path):
    """AC1 续：recoverable 后主 session 反馈重派 → 正确 output 推进到 $end（run 不终态）。"""
    wf = _wf_with_schema()
    tape, bus, _ = _new_run(tmp_path, wf)

    # 1st bad → recoverable
    r1 = advance_step(tape, wf, output="NOT_JSON", run_id="r-test", prompts_dir=None)
    assert r1.recoverable
    _apply(bus, r1, wf)

    # 2nd: 正确 output（满足 schema {k: string}）→ node_completed → $end
    r2 = advance_step(tape, wf, output='{"k": "v"}', run_id="r-test", prompts_dir=None)
    assert r2.done is True
    assert r2.reason == "completed"
    types = [e.type for e in r2.emits]
    assert "node_completed" in types and "workflow_completed" in types


def test_recoverable_escalation_after_3_consecutive(tmp_path):
    """AC2：连续 3 次 recoverable 失败 → 升格 workflow_failed，emit 顺序 nf→ns→workflow_failed。

    retry_count 单调递增（1, 2），第 3 次 done=True（升格）。count 从 tape 派生（consecutive_fail_count）。
    """
    wf = _wf_with_schema()
    tape, bus, _ = _new_run(tmp_path, wf)

    # 1st bad
    r1 = advance_step(tape, wf, output="BAD1", run_id="r-test", prompts_dir=None)
    assert r1.recoverable and r1.retry_count == 1 and not r1.done
    _apply(bus, r1, wf)
    assert consecutive_fail_count(tape, "a") == 1

    # 2nd bad
    r2 = advance_step(tape, wf, output="BAD2", run_id="r-test", prompts_dir=None)
    assert r2.recoverable and r2.retry_count == 2 and not r2.done
    _apply(bus, r2, wf)
    assert consecutive_fail_count(tape, "a") == 2

    # 3rd bad → 升格
    r3 = advance_step(tape, wf, output="BAD3", run_id="r-test", prompts_dir=None)
    assert r3.done is True
    assert not r3.recoverable                       # 升格后不再 recoverable（终态）
    assert r3.error_kind == ERR_OUTPUT_SCHEMA_MISMATCH
    types = [e.type for e in r3.emits]
    # E8 钉死：nf → ns → workflow_failed
    assert types == ["node_failed", "node_started", "workflow_failed"]
    assert r3.emits[2].data["kind"] == ERR_OUTPUT_SCHEMA_MISMATCH
    assert "exhausted" in r3.reason


def test_recoverable_multi_node_count_not_polluted_across_nodes(tmp_path):
    """集成（code-reviewer 🟢#8）：a 失败 2 次 → a 正解 → 推进到 b → b 失败 → b.retry_count==1。

    守 ``consecutive_fail_count`` 谓词「遇 node_completed(任意节点) 重置」的集成效果：
    a 推进到 b 时 emit ``node_completed(a)`` → 计数器重置 → b 首次失败 retry_count==1
    （而非继承 a 的 streak=2 导致不公平升格）。本测试用上 ``_two_node_wf`` fixture（之前 dead）。
    """
    wf = _two_node_wf()
    tape, bus, _ = _new_run(tmp_path, wf)

    # a 失败 2 次（retry_count 1 → 2，未升格）
    r1 = advance_step(tape, wf, output="A_BAD_1", run_id="r-test", prompts_dir=None)
    assert r1.recoverable and r1.retry_count == 1
    _apply(bus, r1, wf)
    r2 = advance_step(tape, wf, output="A_BAD_2", run_id="r-test", prompts_dir=None)
    assert r2.recoverable and r2.retry_count == 2
    _apply(bus, r2, wf)
    assert consecutive_fail_count(tape, "a") == 2

    # a 正解 → 推进到 b（emit node_completed(a) → 计数器重置）
    r3 = advance_step(tape, wf, output='{"k": "a-value"}', run_id="r-test", prompts_dir=None)
    assert r3.done is False and r3.node == "b"
    _apply(bus, r3, wf)
    # 计数器已被 node_completed(a) 重置
    assert consecutive_fail_count(tape, "a") == 0
    assert consecutive_fail_count(tape, "b") == 0

    # b 首次失败 → retry_count==1（不被 a 历史 streak=2 污染，否则会直接 retry_count==3 升格）
    r4 = advance_step(tape, wf, output="B_BAD", run_id="r-test", prompts_dir=None)
    assert r4.recoverable, "b 首次失败应 recoverable（不被 a 历史污染）"
    assert r4.retry_count == 1, (
        f"b 首次失败 retry_count 应为 1（a 的 streak 已被 nc(a) 重置），实得 {r4.retry_count}"
    )
    assert r4.node == "b"
    _apply(bus, r4, wf)
    assert consecutive_fail_count(tape, "b") == 1


def test_recoverable_escalation_tape_idempotent_replay(tmp_path):
    """AC5：升格后的 tape 事件序列经 reducer 重放，RunState 一致（state.status=failed）。"""
    from orca.events.replay import replay_state

    wf = _wf_with_schema()
    tape, bus, _ = _new_run(tmp_path, wf)
    for _ in range(3):
        r = advance_step(tape, wf, output="BAD", run_id="r-test", prompts_dir=None)
        _apply(bus, r, wf)

    state = replay_state(tape)
    assert state.status == "failed"                 # workflow_failed → 终态
    # 重放两次结果相同（G2 幂等硬约束）
    state2 = replay_state(tape)
    assert state2.status == state.status
    assert state2.node_status == state.node_status


def test_recoverable_single_failure_tape_replays_to_running(tmp_path):
    """AC5 中间态（code-reviewer 🔴）：1 次 recoverable 的 tape 重放 → state.status='running'。

    SPEC §7 AC4 resume 续跑的先决条件：reducer 重放含 ``[nf, ns]`` 的 tape 必须得出
    ``state.status='running'``，否则 ``advance_step`` 的 ``state.status in (completed/failed/
    cancelled)`` 短路会把 run 误判为终态。本测试守住这个不变量（仅升格终态测试不够 ——
    若 reducer 未来误把 node_failed 当 state 判死信号，升格测仍通过但 resume 静默失效）。
    """
    from orca.events.replay import replay_state

    wf = _wf_with_schema()
    tape, bus, _ = _new_run(tmp_path, wf)

    # 1 次 bad → recoverable（emit [nf, ns]，run 存活）
    r = advance_step(tape, wf, output="BAD", run_id="r-test", prompts_dir=None)
    assert r.recoverable
    _apply(bus, r, wf)

    state = replay_state(tape)
    assert state.status == "running", (
        f"recoverable 后 state.status 应为 running（run 存活，AC4 resume 前提），"
        f"实得 {state.status!r}"
    )
    assert state.node_status.get("a") == "running", (
        f"node_status['a'] 应为 running（重 arm 后），实得 {state.node_status.get('a')!r}"
    )
    # 重放两次结果一致（G2 幂等）
    state2 = replay_state(tape)
    assert state2.status == state.status
    assert state2.node_status == state.node_status


# ── AC10：render_error 全 irrecoverable 回归 ────────────────────────────────


def test_render_error_is_irrecoverable(tmp_path):
    """AC10：下游 prompt 引用上游缺失字段 → InSessionError(render_error)（非 recoverable）。

    render_error 在 _parse_output **之后**（渲染下一节点 prompt 时）抛，不在 recoverable
    try/except 范围 → 透传 advance_step（不 emit [nf, ns]）。``_parse_output`` 成功（a 无 schema），
    完成后渲染 b 引用 a.output.nope → UndefinedError → _render_or_fail → InSessionError。
    """
    wf = Workflow(
        name="render_err_wf",
        entry="a",
        nodes=[
            AgentNode(
                name="a", executor="opencode", model="d/d",
                prompt="do A", routes=[Route(to="b")],
            ),
            AgentNode(
                name="b", executor="opencode", model="d/d",
                prompt="基于 {{ a.output.nope }} 总结", routes=[Route(to="$end")],
            ),
        ],
    )
    tape, bus, _ = _new_run(tmp_path, wf)

    # 完成 a（合法 output，a 无 schema）→ 渲染 b prompt 抛 render_error
    with pytest.raises(InSessionError) as exc_info:
        advance_step(tape, wf, output="some-text", run_id="r-test", prompts_dir=None)
    assert exc_info.value.error_kind == "render_error"
    # 关键：不是 RecoverableInSessionError（render_error 仍 irrecoverable）
    assert not isinstance(exc_info.value, RecoverableInSessionError)


def test_render_error_template_syntax_is_irrecoverable(tmp_path):
    """AC10 子分支（code-reviewer 🟡#4）：Jinja 模板语法错 → render_error（非 recoverable）。

    SPEC §7 AC10 列举 render_error 三类：缺字段 / **语法错** / outputs 模板。
    前两类已覆盖（``test_render_error_is_irrecoverable`` + cli ``test_next_outputs_template_render_failure_fails_loud``），
    本测试守第三类：``{% if x %}`` 无 ``{% endif %}`` → Jinja ``TemplateSyntaxError`` 经
    ``render_prompt`` 包成 ``ExecError`` → ``_render_or_fail`` 包成 ``InSessionError(render_error)``。
    防未来 ``_render_or_fail`` 把语法错与 UndefinedError 区分处理（或漏 catch）逃逸成脏崩溃。
    """
    wf = Workflow(
        name="render_syntax_err_wf",
        entry="a",
        nodes=[
            AgentNode(
                name="a", executor="opencode", model="d/d",
                # {% if %} 无 endif → Jinja TemplateSyntaxError
                prompt="{% if undefined_var %}unterminated", routes=[Route(to="$end")],
            ),
        ],
    )
    tape = Tape(tmp_path / "tape_syntax.jsonl", run_id="r-syntax", resume=True)
    bus = EventBus(tape)

    # bootstrap 渲染 entry 节点 a 即抛 TemplateSyntaxError → InSessionError(render_error)
    with pytest.raises(InSessionError) as exc_info:
        advance_step(tape, wf, run_id="r-syntax", prompts_dir=None)
    assert exc_info.value.error_kind == "render_error"
    assert not isinstance(exc_info.value, RecoverableInSessionError)


# ── AC8b：grep 守门 ─────────────────────────────────────────────────────────


def test_parse_output_raises_recoverable_not_plain():
    """AC8b grep 守门：``_parse_output`` 的 schema-mismatch raise 须用 RecoverableInSessionError。

    防回归：未来若有人把 recoverable 改回 plain ``raise InSessionError``，本测 fail loud。
    """
    src = inspect.getsource(step_mod._parse_output)
    assert "raise RecoverableInSessionError" in src, (
        "_parse_output 必须 raise RecoverableInSessionError（output_schema_mismatch recoverable）"
    )
    assert "raise InSessionError" not in src, (
        "_parse_output 不得 plain raise InSessionError（schema mismatch 须 recoverable 子类）"
    )
