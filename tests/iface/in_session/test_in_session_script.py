"""tests/iface/in_session/test_in_session_script.py —— in-session script 节点 pass-through。

覆盖 SPEC ``docs/specs/2026-08-21-in-session-script-node.md`` §8 单测面：
  - step 纯决策：``advance_step`` 四分支（entry script / 分支 3 尾部分流 / 分支 4 重发 /
    分支 3 入口 state_corrupt 守卫）+ ``advance_after_script``（END / agent / script 链 /
    unsupported）。
  - helper ``execute_script_inline``：真 bash echo/exit/sleep + ns 先行 / nc|nf 各自成批 +
    尾随 error 事件丢弃 + tail ≤500（§2.4）。
  - 共享循环 ``advance_with_scripts``：fake executor 链透传（AC2）+ D3 max_iter 撞顶 +
    失败信封 auto_executed 注入（AC8）+ M1 merged_inputs + D2 路由 inputs + D4 hook。
  - G2 对齐（AC6）：同一 fake ``make_executor`` 注入两路，全长 ``(type, node)`` 序列逐项相等。
  - CLI 层：AC1（§3 批 1–批 4 增量逐字）/ AC3 / AC4 / AC5 / AC7（entry 链 + B8 短路）/
    AC9（窗口 i 恢复 + 计数不增 + state_corrupt）/ D4 前置 ensure / busy hint（U1）。
  - daemon：AC10（共享循环透传 + try 界 + 回复含 auto_executed）。

项目惯例：``asyncio.run``（无 pytest-asyncio）；CLI 走 CliRunner。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from typer.testing import CliRunner

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.iface.in_session._step_io import (
    advance_with_scripts,
    apply_step_result,
    execute_script_inline,
    fail_in_session,
)
from orca.iface.in_session.cli import app
from orca.run.orchestrator import Orchestrator
from orca.run.step import (
    ERR_SCRIPT_TIMEOUT,
    ERR_STATE_CORRUPT,
    ERR_UNSUPPORTED_NODE_KIND,
    InSessionError,
    advance_after_script,
    advance_step,
)
from orca.schema.workflow import AgentNode, Route, ScriptNode, SetNode, Workflow
from tests.run.conftest import FakeExecutor, make_bus, run_async

# ── wf fixtures（pydantic 直构，纯决策单测用）────────────────────────────────


def _wf_asb() -> Workflow:
    """a(agent) → s(script) → b(agent)（SPEC §3 / AC1 形）。"""
    return Workflow(
        name="asb_wf", entry="a",
        nodes=[
            AgentNode(name="a", executor="opencode", model="d/d",
                      prompt="do A", routes=[Route(to="s")]),
            ScriptNode(name="s", command="echo s-out", routes=[Route(to="b")]),
            AgentNode(name="b", executor="opencode", model="d/d",
                      prompt="do B", routes=[Route(to="$end")]),
        ],
    )


def _new_tape(tmp_path: Path, name: str = "t") -> tuple[Tape, EventBus]:
    tape = Tape(tmp_path / f"{name}.jsonl", run_id="r1", resume=True)
    return tape, EventBus(tape)


def _advance_apply(bus: EventBus, tape: Tape, wf: Workflow, **kw) -> Any:
    """advance_step + apply_step_result（inline 决策单测驱动用）。"""
    r = advance_step(tape, wf, run_id="r1", prompts_dir=None, **kw)
    asyncio.run(apply_step_result(bus, r, wf=wf, run_id="r1"))
    return r


def _seed_window_i(bus: EventBus, wf: Workflow, yaml_path: Path | None = None) -> None:
    """手构窗口 i tape：ws → ns(a) → nc(a) → rt(a→s) → ns(s)（script 执行中 CLI 被杀，
    nc 未落）。ws 带 yaml_path 让 CLI 的 ``_load_wf_for_run`` 可反查。"""

    async def _seed() -> None:
        ws_data: dict[str, Any] = {"workflow_name": wf.name, "inputs": {}}
        if yaml_path is not None:
            ws_data["yaml_path"] = str(yaml_path.resolve())
        await bus.emit("workflow_started", ws_data)
        await bus.emit("node_started", {"node": "a"}, node="a")
        await bus.emit("node_completed", {"output": "OUT-A", "elapsed": 0.0}, node="a")
        await bus.emit("route_taken", {"from": "a", "to": "s"})
        await bus.emit("node_started", {"node": "s", "kind": "script",
                                        "command": "echo s-out"}, node="s")

    asyncio.run(_seed())


# ── step 纯决策：advance_step 四分支（§2.2）─────────────────────────────────


def test_step_entry_script_emits_ws_only(tmp_path):
    """§2.2.1：entry 是 script → emits=[workflow_started]（不 emit ns），node_kind=script。"""
    wf = Workflow(
        name="entry_script_wf", entry="s",
        nodes=[ScriptNode(name="s", command="echo x", routes=[Route(to="$end")])],
    )
    tape, _bus = _new_tape(tmp_path, "entry")
    r = advance_step(tape, wf, run_id="r1", prompts_dir=None)
    assert [e.type for e in r.emits] == ["workflow_started"]
    assert r.node == "s" and r.node_kind == "script" and r.done is False
    # §2.1：script 时三交付字段恒 None
    assert r.prompt is None and r.prompt_file is None and r.resources_root is None


def test_step_branch3_routes_to_script(tmp_path):
    """§2.2.2：a 产出后路由到 script → emits=[nc(a), rt(a→s)]（无 ns、无 deliver）。"""
    wf = _wf_asb()
    tape, bus = _new_tape(tmp_path, "b3")
    _advance_apply(bus, tape, wf)
    r = advance_step(tape, wf, output="OUT-A", run_id="r1", prompts_dir=None)
    assert [(e.type, e.node) for e in r.emits] == [
        ("node_completed", "a"), ("route_taken", None),
    ]
    assert r.node == "s" and r.node_kind == "script"
    assert r.prompt is None and r.prompt_file is None and r.resources_root is None


def test_step_branch3_guard_output_on_script_state_corrupt(tmp_path):
    """§2.2.4：pending 是 script 时宿主带 --output 交卷 → state_corrupt fail loud。"""
    wf = _wf_asb()
    tape, bus = _new_tape(tmp_path, "guard")
    _advance_apply(bus, tape, wf)
    r = advance_step(tape, wf, output="OUT-A", run_id="r1", prompts_dir=None)
    asyncio.run(apply_step_result(bus, r, wf=wf, run_id="r1"))  # rt(a→s) 落 tape
    # 现在 s running（分支 3 已推进；tape 等价于 rt 落后、ns 落前）——补 ns(s) 成窗口 i 形
    asyncio.run(bus.emit("node_started", {"node": "s"}, node="s"))
    with pytest.raises(InSessionError) as ei:
        advance_step(tape, wf, output="HOST-HIJACK", run_id="r1", prompts_dir=None)
    assert ei.value.error_kind == ERR_STATE_CORRUPT
    assert "不带 --output" in str(ei.value)


def test_step_branch4_script_idempotent_redispatch(tmp_path):
    """§2.2.3：窗口 i（ns(S) 已落、nc 未落）无 output 调用 → emits=[] + node_kind=script。"""
    wf = _wf_asb()
    tape, bus = _new_tape(tmp_path, "b4")
    _seed_window_i(bus, wf)
    r = advance_step(tape, wf, run_id="r1", prompts_dir=None)
    assert r.emits == []
    assert r.node == "s" and r.node_kind == "script" and r.done is False


def test_step_branch3_unsupported_set_kind(tmp_path):
    """§5.3：a → set（非 agent/script）→ unsupported_node_kind fail loud（文案含 agent/script）。"""
    wf = Workflow(
        name="aset_wf", entry="a",
        nodes=[
            AgentNode(name="a", executor="opencode", model="d/d",
                      prompt="do A", routes=[Route(to="v")]),
            SetNode(name="v", values={"k": "1"}, routes=[Route(to="$end")]),
        ],
    )
    tape, bus = _new_tape(tmp_path, "set")
    _advance_apply(bus, tape, wf)
    with pytest.raises(InSessionError) as ei:
        advance_step(tape, wf, output="OUT-A", run_id="r1", prompts_dir=None)
    assert ei.value.error_kind == ERR_UNSUPPORTED_NODE_KIND
    assert "agent/script" in str(ei.value)


# ── step 纯决策：advance_after_script（§2.3）────────────────────────────────


def _tape_script_completed(tmp_path, wf, name="sc") -> tuple[Tape, EventBus]:
    """tape：ws + ns(s) + nc(s)（script 已由引擎执行完、nc 已落）。"""

    async def _seed() -> None:
        await bus.emit("workflow_started", {"workflow_name": wf.name, "inputs": {}})
        await bus.emit("node_started", {"node": "s", "kind": "script",
                                        "command": "echo"}, node="s")
        await bus.emit("node_completed", {"output": {"stdout": "o", "stderr": "",
                                                     "exit_code": 0}, "elapsed": 0.01},
                       node="s")

    tape, bus = _new_tape(tmp_path, name)
    asyncio.run(_seed())
    return tape, bus


def test_after_script_to_end(tmp_path):
    """§2.3.3：s → $end → [rt, workflow_completed]，done=True。"""
    wf = Workflow(
        name="s_end_wf", entry="s",
        nodes=[ScriptNode(name="s", command="echo", routes=[Route(to="$end")])],
    )
    tape, _bus = _tape_script_completed(tmp_path, wf)
    r = advance_after_script(tape, wf, "s", inputs={}, run_id="r1", prompts_dir=None)
    assert r.done is True and r.reason == "completed"
    assert [e.type for e in r.emits] == ["route_taken", "workflow_completed"]
    assert r.emits[0].data == {"from": "s", "to": "$end"}


def test_after_script_to_agent_inline(tmp_path):
    """§2.3.4：s → b(agent) → [rt, ns(b)] + inline 三交付字段（prompt 全量、file/root None）。"""
    wf = _wf_asb()
    tape, _bus = _tape_script_completed(tmp_path, wf)
    r = advance_after_script(tape, wf, "s", inputs={}, run_id="r1", prompts_dir=None)
    assert r.done is False and r.node == "b" and r.node_kind is None
    assert [(e.type, e.node) for e in r.emits] == [("route_taken", None), ("node_started", "b")]
    assert r.emits[1].data == {"node": "b"}
    assert r.prompt  # inline 回退：prompt 是唯一交付物
    assert r.prompt_file is None and r.resources_root is None


def test_after_script_to_agent_compact(tmp_path):
    """§2.3.4 compact：prompts_dir 给定 → prompt_file 落盘、prompt=None（指针交付）。"""
    wf = _wf_asb()
    tape, _bus = _tape_script_completed(tmp_path, wf, "cpt")
    prompts_dir = tmp_path / "prompts"
    r = advance_after_script(tape, wf, "s", inputs={}, run_id="r1", prompts_dir=prompts_dir)
    assert r.prompt is None
    assert r.prompt_file and Path(r.prompt_file).is_file()
    assert Path(r.prompt_file).name == "b.md"


def test_after_script_to_script_chain(tmp_path):
    """§2.3.5：s → s2(script) → [rt]，node_kind=script（循环继续）。"""
    wf = Workflow(
        name="chain_wf", entry="s",
        nodes=[
            ScriptNode(name="s", command="echo", routes=[Route(to="s2")]),
            ScriptNode(name="s2", command="echo", routes=[Route(to="$end")]),
        ],
    )
    tape, _bus = _tape_script_completed(tmp_path, wf, "chain")
    r = advance_after_script(tape, wf, "s", inputs={}, run_id="r1", prompts_dir=None)
    assert [(e.type, e.node) for e in r.emits] == [("route_taken", None)]
    assert r.node == "s2" and r.node_kind == "script" and r.done is False


def test_after_script_unsupported_kind(tmp_path):
    """§2.3.6：s → v(set) → unsupported_node_kind fail loud。"""
    wf = Workflow(
        name="s_set_wf", entry="s",
        nodes=[
            ScriptNode(name="s", command="echo", routes=[Route(to="v")]),
            SetNode(name="v", values={"k": "1"}, routes=[Route(to="$end")]),
        ],
    )
    tape, _bus = _tape_script_completed(tmp_path, wf, "sset")
    with pytest.raises(InSessionError) as ei:
        advance_after_script(tape, wf, "s", inputs={}, run_id="r1", prompts_dir=None)
    assert ei.value.error_kind == ERR_UNSUPPORTED_NODE_KIND


# ── helper：execute_script_inline（§2.4，真 bash）───────────────────────────


def _run_inline(tmp_path, node: ScriptNode, wf: Workflow | None = None, *,
                inputs: dict | None = None):
    tape = Tape(tmp_path / "inl.jsonl", run_id="r-inl", resume=True)
    bus = EventBus(tape)
    wf = wf or Workflow(name="inl_wf", entry=node.name, nodes=[node])
    out = asyncio.run(execute_script_inline(
        bus, tape, wf, node, run_id="r-inl", inputs=inputs or {},
    ))
    return out, tape, bus


def _tape_pairs(tape: Tape) -> list[tuple[str, Any]]:
    return [(e.type, e.node) for e in tape.replay()]


def test_inline_success_batches_and_summary(tmp_path):
    """§2.4.3/§2.4.4：ns / nc 各自成批（session_id 保留）+ 返回 output 与摘要。"""
    node = ScriptNode(name="s", command="echo hello-inline", routes=[Route(to="$end")])
    (output, summary), tape, bus = _run_inline(tmp_path, node)
    assert _tape_pairs(tape) == [("node_started", "s"), ("node_completed", "s")]
    # session_id 保留（executor 产出，web 按 session 分组依据）
    events = list(tape.replay())
    assert all(e.session_id for e in events)
    assert "hello-inline" in output["stdout"] and output["exit_code"] == 0
    assert summary["node"] == "s" and summary["exit_code"] == 0
    assert summary["elapsed"] is not None
    assert "hello-inline" in summary["stdout_tail"]
    assert summary["stderr_tail"] == ""
    bus.close()


def test_inline_nonzero_exit_is_business_result(tmp_path):
    """§2.6 行 2：非零退出码不 fail loud（业务结果，路由分叉）。"""
    node = ScriptNode(name="s", command="exit 7", routes=[Route(to="$end")])
    (output, summary), tape, bus = _run_inline(tmp_path, node)
    assert _tape_pairs(tape) == [("node_started", "s"), ("node_completed", "s")]
    assert output["exit_code"] == 7 and summary["exit_code"] == 7
    bus.close()


def test_inline_timeout_drops_error_event(tmp_path):
    """§2.6 行 3 / §2.4.3：timeout → ns → nf（无 error 事件落 tape）+ script_timeout。"""
    node = ScriptNode(name="s", command="sleep 3", timeout=1, routes=[Route(to="$end")])
    with pytest.raises(InSessionError) as ei:
        _run_inline(tmp_path, node)
    assert ei.value.error_kind == ERR_SCRIPT_TIMEOUT
    tape = Tape(tmp_path / "inl.jsonl", run_id="r-inl", resume=True)
    assert _tape_pairs(tape) == [("node_started", "s"), ("node_failed", "s")]
    assert not any(e.type == "error" for e in tape.replay())
    tape.close()


def test_inline_render_failure_internal_error(tmp_path):
    """§2.6 行 4：command 模板引用未定义 inputs → ExecError(render) → internal_error（含 phase）。"""
    node = ScriptNode(name="s", command="echo {{ inputs.absent }}",
                      routes=[Route(to="$end")])
    with pytest.raises(InSessionError) as ei:
        _run_inline(tmp_path, node)
    assert ei.value.error_kind == "internal_error"
    assert "render" in str(ei.value)
    tape = Tape(tmp_path / "inl.jsonl", run_id="r-inl", resume=True)
    assert _tape_pairs(tape) == [("node_started", "s"), ("node_failed", "s")]
    assert not any(e.type == "error" for e in tape.replay())
    tape.close()


def test_inline_stdout_tail_limit_500(tmp_path):
    """§2.4.5：stdout_tail 截断 ≤500（_AUTO_EXEC_TAIL_LIMIT）。"""
    node = ScriptNode(
        name="s",
        command="head -c 600 /dev/zero | tr '\\0' 'x'",
        routes=[Route(to="$end")],
    )
    (output, summary), tape, bus = _run_inline(tmp_path, node)
    assert len(output["stdout"]) == 600
    assert len(summary["stdout_tail"]) == 500
    assert summary["stdout_tail"] == "x" * 500
    bus.close()


def test_inline_tail_takes_end_segment_not_head(tmp_path):
    """§2.4.5 回归（E2E D-1）：tail 取**末** 500 字符（Unix tail 语义——长输出的
    verdict/error 在末尾），非前 500。600 个 H/E（头）+ 尾部标记（首尾可区分），
    前 115 个字符必须被截掉。"""
    node = ScriptNode(
        name="s",
        command="head -c 600 /dev/zero | tr '\\0' 'H'; printf 'TAIL-MARKER-END'; "
                "head -c 600 /dev/zero | tr '\\0' 'E' >&2; printf 'STDERR-TAIL-END' >&2",
        routes=[Route(to="$end")],
    )
    (output, summary), tape, bus = _run_inline(tmp_path, node)
    # 头尾可区分前提：stdout = 600×H + 15 标记，stderr = 600×E + 15 标记
    assert output["stdout"] == "H" * 600 + "TAIL-MARKER-END"
    assert output["stderr"] == "E" * 600 + "STDERR-TAIL-END"
    assert len(summary["stdout_tail"]) == 500
    assert summary["stdout_tail"].endswith("TAIL-MARKER-END")
    assert summary["stdout_tail"] == "H" * 485 + "TAIL-MARKER-END"
    assert len(summary["stderr_tail"]) == 500
    assert summary["stderr_tail"].endswith("STDERR-TAIL-END")
    assert summary["stderr_tail"] == "E" * 485 + "STDERR-TAIL-END"
    bus.close()


# ── 共享循环：advance_with_scripts（§2.5，fake executor 注入）────────────────


def _patch_fake_executor(monkeypatch, *, fail: str | None = None) -> None:
    """fake make_executor（零 token 手法，先例 tests/run/test_orchestrator.py）。"""

    def _make(node, *a, **kw):
        if fail is not None and node.name == fail:
            return FakeExecutor.failing(
                error_type="ExecTimeout", message="fake timeout",
                phase="timeout", node_name=node.name,
            )
        return FakeExecutor.produces(
            {"stdout": f"fake-{node.name}", "exit_code": 0}, node_name=node.name,
        )

    monkeypatch.setattr("orca.exec.factory.make_executor", _make)


def _wf_chain() -> Workflow:
    """a(agent) → s1(script) → s2(script) → b(agent)（AC2 形）。"""
    return Workflow(
        name="chain2_wf", entry="a",
        nodes=[
            AgentNode(name="a", executor="opencode", model="d/d",
                      prompt="do A", routes=[Route(to="s1")]),
            ScriptNode(name="s1", command="echo 1", routes=[Route(to="s2")]),
            ScriptNode(name="s2", command="echo 2", routes=[Route(to="b")]),
            AgentNode(name="b", executor="opencode", model="d/d",
                      prompt="do B", routes=[Route(to="$end")]),
        ],
    )


def test_loop_script_chain_passthrough(tmp_path, monkeypatch):
    """AC2：一次 next 透传 s1→s2，停在 b；auto_executed 按序 2 条。"""
    _patch_fake_executor(monkeypatch)
    wf = _wf_chain()
    tape = Tape(tmp_path / "loop.jsonl", run_id="r-loop", resume=True)
    bus = EventBus(tape)
    r0 = run_async(advance_with_scripts(
        bus, tape, wf, output=None, cli_inputs={}, run_id="r-loop"))
    assert r0[0].node == "a" and r0[2] == []          # bootstrap：agent，零 script
    r1 = run_async(advance_with_scripts(
        bus, tape, wf, output="OUT-A", cli_inputs={}, run_id="r-loop"))
    result, _reply, auto = r1
    assert result.node == "b" and result.node_kind is None
    assert [e["node"] for e in auto] == ["s1", "s2"]
    assert all(e["exit_code"] == 0 for e in auto)
    assert _tape_pairs(tape) == [
        ("workflow_started", None),
        ("node_started", "a"),
        ("node_completed", "a"), ("route_taken", None),
        ("node_started", "s1"), ("node_completed", "s1"),
        ("route_taken", None),
        ("node_started", "s2"), ("node_completed", "s2"),
        ("route_taken", None),
        ("node_started", "b"),
    ]
    bus.close()


def test_loop_max_iter_cap(tmp_path, monkeypatch):
    """D3 / §7 edge：S 路由回自身 → 撞 max_iter（默认 100）→ internal_error 终态。"""
    _patch_fake_executor(monkeypatch)
    wf = Workflow(
        name="selfloop_wf", entry="s",
        nodes=[ScriptNode(name="s", command="echo x", routes=[Route(to="s")])],
    )
    tape = Tape(tmp_path / "loop.jsonl", run_id="r-loop", resume=True)
    bus = EventBus(tape)
    with pytest.raises(InSessionError) as ei:
        run_async(advance_with_scripts(
            bus, tape, wf, output=None, cli_inputs={}, run_id="r-loop"))
    assert ei.value.error_kind == "internal_error"
    assert "max_iter" in str(ei.value)
    # 恰执行 100 次（第 101 次前撞顶）
    assert sum(1 for t, _n in _tape_pairs(tape) if t == "node_completed") == 100
    bus.close()


def test_loop_invalid_iterations_wrapped_as_insession_error(tmp_path, monkeypatch):
    """🟡 加固守门：非法 ``inputs.iterations``（非数值）→ InSessionError(internal_error)
    信封，非裸 ValueError 穿透 except InSessionError 面（headless 同契约）。"""
    _patch_fake_executor(monkeypatch)
    wf = Workflow(
        name="bad_iter_wf", entry="s",
        nodes=[ScriptNode(name="s", command="echo x", routes=[Route(to="$end")])],
    )
    tape = Tape(tmp_path / "baditer.jsonl", run_id="r-baditer", resume=True)
    bus = EventBus(tape)
    with pytest.raises(InSessionError) as ei:
        run_async(advance_with_scripts(
            bus, tape, wf, output=None, cli_inputs={"iterations": "abc"},
            run_id="r-baditer"))
    assert ei.value.error_kind == "internal_error"
    assert "max_iter" in str(ei.value)
    bus.close()


def test_loop_failure_envelope_carries_auto_executed(tmp_path, monkeypatch):
    """AC8：链内 s2 失败 → InSessionError.auto_executed=[s1] → fail_in_session 注入失败信封。"""
    _patch_fake_executor(monkeypatch, fail="s2")
    wf = _wf_chain()
    tape = Tape(tmp_path / "loop.jsonl", run_id="r-loop", resume=True)
    bus = EventBus(tape)
    r0 = run_async(advance_with_scripts(
        bus, tape, wf, output=None, cli_inputs={}, run_id="r-loop"))
    assert r0[0].node == "a"
    with pytest.raises(InSessionError) as ei:
        run_async(advance_with_scripts(
            bus, tape, wf, output="OUT-A", cli_inputs={}, run_id="r-loop"))
    assert ei.value.error_kind == ERR_SCRIPT_TIMEOUT
    # 失败节点条目不含 s2，已成功的 s1 报备（§2.7）
    assert [e["node"] for e in ei.value.auto_executed] == ["s1"]
    reply = asyncio.run(fail_in_session(bus, ei.value))
    assert reply["done"] is True and reply["error_kind"] == ERR_SCRIPT_TIMEOUT
    assert [e["node"] for e in reply["auto_executed"]] == ["s1"]
    bus.close()


def test_loop_merged_inputs_render_command(tmp_path, monkeypatch):
    """M1 / §7 edge：script command 引用 {{ inputs.* }} 用 merged_inputs 渲染。"""
    _patch_fake_executor(monkeypatch)
    calls: list[dict] = []
    real = execute_script_inline

    async def _spy(bus, tape, wf, node, *, run_id, inputs, yaml_path=None):
        calls.append(inputs)
        return await real(bus, tape, wf, node, run_id=run_id, inputs=inputs,
                          yaml_path=yaml_path)

    import orca.iface.in_session._step_io as step_io_mod
    monkeypatch.setattr(step_io_mod, "execute_script_inline", _spy)
    wf = Workflow(
        name="m1_wf", entry="s",
        nodes=[ScriptNode(name="s", command="echo {{ inputs.mode }}",
                          routes=[Route(to="$end")])],
    )
    tape = Tape(tmp_path / "m1.jsonl", run_id="r-m1", resume=True)
    bus = EventBus(tape)
    run_async(advance_with_scripts(
        bus, tape, wf, output=None, cli_inputs={"mode": "m1-ok"}, run_id="r-m1"))
    assert calls and calls[0].get("mode") == "m1-ok"   # ctx inputs = merged（含 default 填充口径）
    bus.close()


def test_loop_route_when_inputs(tmp_path, monkeypatch):
    """D2 / §7 edge：路由 when 引用 {{ inputs.* }}（ctx inputs 修复后不再 RouteError 裸崩）。"""
    _patch_fake_executor(monkeypatch)

    def _wf(flag: str) -> Workflow:
        return Workflow(
            name=f"d2_{flag}", entry="s",
            nodes=[
                ScriptNode(name="s", command="echo x", routes=[
                    Route(when="inputs.flag == 'go'", to="b"),
                    Route(to="$end"),
                ]),
                AgentNode(name="b", executor="opencode", model="d/d",
                          prompt="do B", routes=[Route(to="$end")]),
            ],
        )

    # go → 停在 agent b
    tape = Tape(tmp_path / "d2a.jsonl", run_id="r-d2", resume=True)
    bus = EventBus(tape)
    result, _reply, _auto = run_async(advance_with_scripts(
        bus, tape, _wf("go"), output=None, cli_inputs={"flag": "go"}, run_id="r-d2"))
    assert result.node == "b" and not result.done
    bus.close()
    # stop → 直通 $end（done）
    tape2 = Tape(tmp_path / "d2b.jsonl", run_id="r-d2", resume=True)
    bus2 = EventBus(tape2)
    result2, _r2, auto2 = run_async(advance_with_scripts(
        bus2, tape2, _wf("stop"), output=None, cli_inputs={"flag": "stop"}, run_id="r-d2"))
    assert result2.done is True and result2.reason == "completed"
    assert [e["node"] for e in auto2] == ["s"]
    bus2.close()


def test_loop_on_chain_start_callback(tmp_path, monkeypatch):
    """D4：链进入前回调恰好一次；agent 结果不触发。"""
    _patch_fake_executor(monkeypatch)
    wf = _wf_chain()
    tape = Tape(tmp_path / "hook.jsonl", run_id="r-hook", resume=True)
    bus = EventBus(tape)
    cb = mock.Mock()
    run_async(advance_with_scripts(
        bus, tape, wf, output=None, cli_inputs={}, run_id="r-hook",
        on_script_chain_start=cb))
    cb.assert_not_called()          # bootstrap 停在 agent a：不进链
    run_async(advance_with_scripts(
        bus, tape, wf, output="OUT-A", cli_inputs={}, run_id="r-hook",
        on_script_chain_start=cb))
    cb.assert_called_once()         # s1→s2 链进入前恰好一次
    bus.close()


def test_loop_no_memory_kwarg_passthrough(tmp_path, monkeypatch):
    """循环内 no_memory→apply_step_result 透传守门（test_node_memory 缝隙迁移后的内层缝）。

    a(memory=True) 完成时：``no_memory=True`` → 不写记忆 MD；``no_memory=False`` → 写。
    防未来误删共享循环内 apply_step_result 的 no_memory kwarg（script 链路径记忆静默失效）。
    """
    _patch_fake_executor(monkeypatch)

    def _wf(run: str) -> Workflow:
        return Workflow(
            name=f"mem_chain_{run}", entry="a",
            nodes=[
                AgentNode(name="a", executor="opencode", model="d/d",
                          memory=True, prompt="do A", routes=[Route(to="s")]),
                ScriptNode(name="s", command="echo x", routes=[Route(to="$end")]),
            ],
        )

    def _memory_mds(root: Path) -> list[Path]:
        mem = root / ".orca" / "memory"
        return list(mem.rglob("*.md")) if mem.exists() else []

    # no_memory=True：nc(a) 落 tape 也不写 MD
    tape1 = Tape(tmp_path / "mem1.jsonl", run_id="r-mem1", resume=True)
    bus1 = EventBus(tape1)
    run_async(advance_with_scripts(
        bus1, tape1, _wf("on"), output=None, cli_inputs={}, run_id="r-mem1",
        project_root=tmp_path))
    result, _reply, _auto = run_async(advance_with_scripts(
        bus1, tape1, _wf("on"), output="OUT-A", cli_inputs={}, run_id="r-mem1",
        project_root=tmp_path, no_memory=True))
    assert result.done is True                     # a → s → $end
    assert _memory_mds(tmp_path) == []
    bus1.close()

    # 对照组 no_memory=False（默认）：写 a 的记忆 MD
    mem_root = tmp_path / "proj2"
    mem_root.mkdir()
    tape2 = Tape(mem_root / "mem2.jsonl", run_id="r-mem2", resume=True)
    bus2 = EventBus(tape2)
    run_async(advance_with_scripts(
        bus2, tape2, _wf("off"), output=None, cli_inputs={}, run_id="r-mem2",
        project_root=mem_root))
    run_async(advance_with_scripts(
        bus2, tape2, _wf("off"), output="OUT-A", cli_inputs={}, run_id="r-mem2",
        project_root=mem_root))
    mds = _memory_mds(mem_root)
    assert any("a.md" == p.name for p in mds), f"no_memory=False 应写 a 记忆 MD：{mds}"
    bus2.close()


# ── G2 对齐（AC6）───────────────────────────────────────────────────────────


def test_g2_alignment_full_tape_sequence(tmp_path, monkeypatch):
    """AC6：同一 fake make_executor 两路（headless ``orca run`` vs in-session 共享循环），
    全长 ``(type, node)`` 序列逐项相等（忽略 timestamp/seq/session_id/output）。"""
    _patch_fake_executor(monkeypatch)
    wf = Workflow(
        name="g2_wf", entry="a",
        nodes=[
            ScriptNode(name="a", command="echo a", routes=[Route(to="b")]),
            ScriptNode(name="b", command="echo b", routes=[Route(to="c")]),
            ScriptNode(name="c", command="echo c", routes=[Route(to="$end")]),
        ],
    )
    # headless
    (tmp_path / "headless").mkdir()
    bus_h, tape_h = make_bus(tmp_path / "headless")
    orch = Orchestrator(wf, bus_h)
    run_async(orch.run())
    headless = _tape_pairs(tape_h)
    # in-session（bootstrap entry script → 链直通终态）
    (tmp_path / "insession").mkdir()
    tape_i = Tape(tmp_path / "insession" / "t.jsonl", run_id="r-g2", resume=True)
    bus_i = EventBus(tape_i)
    result, _reply, auto = run_async(advance_with_scripts(
        bus_i, tape_i, wf, output=None, cli_inputs={}, run_id="r-g2"))
    insession = _tape_pairs(tape_i)
    assert result.done is True
    assert [e["node"] for e in auto] == ["a", "b", "c"]
    assert insession == headless
    bus_i.close()


# ── CLI 层（CliRunner）──────────────────────────────────────────────────────


CLI_YAML_A_S_B = """\
name: script_cli_wf
description: A(agent) → S(script) → B(agent)（in-session script 守门）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 step A 的输出。"
    routes:
      - to: s
  - name: s
    kind: script
    command: "echo s-inline"
    routes:
      - to: b
  - name: b
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "基于 {{ a.output }} 总结。"
    routes:
      - to: $end
"""

CLI_YAML_A_S1_S2_B = """\
name: script_chain_cli_wf
description: A(agent) → S1 → S2 → B(agent)（连续 script 链透传）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 step A 的输出。"
    routes:
      - to: s1
  - name: s1
    kind: script
    command: "echo one"
    routes:
      - to: s2
  - name: s2
    kind: script
    command: "echo two"
    routes:
      - to: b
  - name: b
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "基于 {{ a.output }} 总结。"
    routes:
      - to: $end
"""


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """CLI 测试环境：chdir tmp + 禁 web/守护 spawn（速度与隔离）+ 注册 fail-open 打桩。

    ``ensures`` 暴露 D4 两个前置 ensure 的 Mock（供 ``test_cli_d4_*`` 断言）。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ORCA_BOOTSTRAP_OPEN_WEB", "0")
    monkeypatch.setenv("ORCA_HOME", str(tmp_path / ".orca_home"))
    from orca.iface.in_session import cli as cli_mod
    monkeypatch.setattr(cli_mod, "_spawn_chart_daemon", mock.Mock())
    monkeypatch.setattr(cli_mod, "_wait_for_sock", lambda *a, **k: True)
    monkeypatch.setattr(cli_mod, "_spawn_sidechain_daemon", mock.Mock())
    monkeypatch.setattr(cli_mod, "_spawn_open_web", mock.Mock())
    monkeypatch.setattr(cli_mod, "_register_current_project", mock.Mock())
    ensures = {
        "_ensure_chart_daemon": mock.Mock(name="_ensure_chart_daemon"),
        "_ensure_sidechain_daemon": mock.Mock(name="_ensure_sidechain_daemon"),
    }
    for n, m in ensures.items():
        monkeypatch.setattr(cli_mod, n, m)
    return SimpleNamespace(tmp=tmp_path, ensures=ensures, cli=cli_mod)


def _write_wf(tmp_path: Path, text: str, name: str = "wf.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _bootstrap(runner: CliRunner, wf_path: Path, inputs: str = "{}") -> dict:
    result = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", inputs])
    assert result.exit_code == 0, f"bootstrap failed: {result.output}"
    return json.loads(result.output.splitlines()[-1])


def _next(runner: CliRunner, tape: str, run_id: str, *extra: str,
          expect_exit: int = 0) -> dict:
    result = runner.invoke(app, ["next", "--tape", tape, "--run-id", run_id, *extra])
    assert result.exit_code == expect_exit, (
        f"next exit {result.exit_code} (expected {expect_exit}): {result.output}"
    )
    return json.loads(result.output.splitlines()[-1])


def _tape_dicts(tape_path: Path) -> list[dict]:
    return [
        json.loads(ln)
        for ln in tape_path.read_text(encoding="utf-8").strip().splitlines()
        if ln.strip()
    ]


def _pairs(tape_path: Path) -> list[tuple[str, Any]]:
    return [(e["type"], e.get("node")) for e in _tape_dicts(tape_path)]


def test_cli_next_script_passthrough_ac1(cli_env):
    """AC1：next 增量 (type,node) 与 §3 批 1–批 4 逐字相等 + auto_executed 报备。"""
    p = _write_wf(cli_env.tmp, CLI_YAML_A_S_B)
    runner = CliRunner()
    boot = _bootstrap(runner, p)
    assert boot["node"] == "a"
    assert "auto_executed" not in boot        # 未执行 script 的调用无该字段（AC8）
    tape_path = Path(boot["tape"])
    n_before = len(_tape_dicts(tape_path))

    r = _next(runner, str(tape_path), boot["run_id"], "--output", "OUT-A")
    assert r["done"] is False and r["node"] == "b"
    assert r["prompt"]                          # b 的 prompt 交付
    inc = _pairs(tape_path)[n_before:]
    assert inc == [
        ("node_completed", "a"), ("route_taken", None),      # 批1（advance_step emits）
        ("node_started", "s"),                                # 批2（executor 首 yield）
        ("node_completed", "s"),                              # 批3（executor 完成）
        ("route_taken", None), ("node_started", "b"),         # 批4（advance_after_script）
    ]
    assert [e["node"] for e in r["auto_executed"]] == ["s"]
    assert r["auto_executed"][0]["exit_code"] == 0
    assert "s-inline" in r["auto_executed"][0]["stdout_tail"]


def test_cli_next_script_chain_ac2(cli_env):
    """AC2：S1→S2 一次 next 内联跑完返 B 的 prompt；auto_executed 2 条按序。"""
    p = _write_wf(cli_env.tmp, CLI_YAML_A_S1_S2_B)
    runner = CliRunner()
    boot = _bootstrap(runner, p)
    tape_path = Path(boot["tape"])
    r = _next(runner, str(tape_path), boot["run_id"], "--output", "OUT-A")
    assert r["done"] is False and r["node"] == "b"
    assert [e["node"] for e in r["auto_executed"]] == ["s1", "s2"]
    assert r["auto_executed"][0]["stdout_tail"].strip() == "one"
    assert r["auto_executed"][1]["stdout_tail"].strip() == "two"


def test_cli_route_fork_exit_code_ac3(cli_env):
    """AC3：非零退出=业务结果——exit 0 走 B'，非零走兜底 A'（同一 wf 两分支 E2E）。"""
    yaml_text = """\
name: fork_cli_wf
description: exit_code 路由分叉。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 step A 的输出。"
    routes:
      - to: s
  - name: s
    kind: script
    command: "exit {{ inputs.code }}"
    routes:
      - when: "output.exit_code == 0"
        to: b_ok
      - to: b_bad
  - name: b_ok
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "ok 分支。"
    routes:
      - to: $end
  - name: b_bad
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "bad 分支。"
    routes:
      - to: $end
"""
    p = _write_wf(cli_env.tmp, yaml_text)
    runner = CliRunner()

    # 分支 1：exit 0 → 命中 when → b_ok
    boot = _bootstrap(runner, p, inputs='{"code": 0}')
    r1 = _next(runner, boot["tape"], boot["run_id"], "--output", "OUT-A")
    assert r1["node"] == "b_ok"
    assert r1["auto_executed"][0]["exit_code"] == 0
    # 跑完第一个 run（marker 清零后才可再 bootstrap 同 wf）
    r_done = _next(runner, boot["tape"], boot["run_id"], "--output", "DONE")
    assert r_done["done"] is True

    # 分支 2：exit 5 → 兜底 → b_bad（不失败）
    boot2 = _bootstrap(runner, p, inputs='{"code": 5}')
    r2 = _next(runner, boot2["tape"], boot2["run_id"], "--output", "OUT-A")
    assert r2["node"] == "b_bad"
    assert r2["auto_executed"][0]["exit_code"] == 5


def test_cli_parse_json_route_defensive_ac4(cli_env):
    """AC4 正例+防御：parse_json 判定分叉；非 JSON + 防御性 when 落兜底正常推进。"""
    ok_yaml = """\
name: pj_ok_cli_wf
description: parse_json 判定。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 step A 的输出。"
    routes:
      - to: s
  - name: s
    kind: script
    command: "echo '{\\"goal_met\\": true}'"
    parse_json: true
    routes:
      - when: "output.json.goal_met"
        to: b_met
      - to: b_fallback
  - name: b_met
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "met 分支。"
    routes:
      - to: $end
  - name: b_fallback
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "fallback 分支。"
    routes:
      - to: $end
"""
    runner = CliRunner()
    boot = _bootstrap(runner, _write_wf(cli_env.tmp, ok_yaml))
    r = _next(runner, boot["tape"], boot["run_id"], "--output", "OUT-A")
    assert r["node"] == "b_met"                 # json.goal_met == true 命中

    bad_yaml = """\
name: pj_bad_cli_wf
description: parse_json 防御性 when。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 step A 的输出。"
    routes:
      - to: s
  - name: s
    kind: script
    command: "echo not-json"
    parse_json: true
    routes:
      - when: "output.json.goal_met is defined and output.json.goal_met"
        to: b_met
      - to: b_fallback
  - name: b_met
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "met 分支。"
    routes:
      - to: $end
  - name: b_fallback
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "fallback 分支。"
    routes:
      - to: $end
"""
    boot2 = _bootstrap(runner, _write_wf(cli_env.tmp, bad_yaml, "wf_bad.yaml"))
    r2 = _next(runner, boot2["tape"], boot2["run_id"], "--output", "OUT-A")
    assert r2["node"] == "b_fallback"           # 防御性写法：json=None 落兜底


def test_cli_parse_json_bare_reference_bare_crash_pinned(cli_env):
    """AC4 反例（pre-existing 钉死）：裸引用 output.json.goal_met + 非 JSON → RouteError
    裸崩（非 0 退出 + tape 无 workflow_completed）。"""
    yaml_text = """\
name: pj_bare_cli_wf
description: 裸引用 RouteError 钉死。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 step A 的输出。"
    routes:
      - to: s
  - name: s
    kind: script
    command: "echo not-json"
    parse_json: true
    routes:
      - when: "output.json.goal_met"
        to: b_met
      - to: b_fallback
  - name: b_met
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "met。"
    routes:
      - to: $end
  - name: b_fallback
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "fallback。"
    routes:
      - to: $end
"""
    runner = CliRunner()
    boot = _bootstrap(runner, _write_wf(cli_env.tmp, yaml_text))
    tape_path = Path(boot["tape"])
    result = runner.invoke(app, [
        "next", "--tape", str(tape_path), "--run-id", boot["run_id"],
        "--output", "OUT-A",
    ])
    assert result.exit_code != 0                # RouteError 裸崩（非 InSessionError）
    assert result.exception is not None
    types = [e["type"] for e in _tape_dicts(tape_path)]
    assert "workflow_completed" not in types    # 无终态完成事件


def test_cli_script_timeout_terminal_ac5(cli_env):
    """AC5：timeout → 终态 workflow_failed + error_kind=script_timeout + tape ns→nf→wf_failed（无 error）。"""
    yaml_text = CLI_YAML_A_S_B.replace(
        'command: "echo s-inline"', 'command: "sleep 3"\n    timeout: 1',
    ).replace("name: script_cli_wf", "name: script_to_cli_wf")
    runner = CliRunner()
    boot = _bootstrap(runner, _write_wf(cli_env.tmp, yaml_text))
    tape_path = Path(boot["tape"])
    r = _next(runner, str(tape_path), boot["run_id"], "--output", "OUT-A",
              expect_exit=1)
    assert r["done"] is True
    assert r["error_kind"] == "script_timeout"
    assert "auto_executed" not in r             # 首个 script 即失败 → 零成功条目 → 省略
    assert _pairs(tape_path)[-3:] == [
        ("node_started", "s"), ("node_failed", "s"), ("workflow_failed", None),
    ]
    assert "error" not in [t for t, _n in _pairs(tape_path)]


def test_cli_entry_script_chain_bootstrap_ac7a(cli_env):
    """AC7 前半：entry=script 链可 bootstrap，一次调用停在 agent b。"""
    yaml_text = """\
name: entry_chain_cli_wf
description: entry script 链 → agent。
entry: s1
nodes:
  - name: s1
    kind: script
    command: "echo one"
    routes:
      - to: s2
  - name: s2
    kind: script
    command: "echo two"
    routes:
      - to: b
  - name: b
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "收尾。"
    routes:
      - to: $end
"""
    runner = CliRunner()
    boot = _bootstrap(runner, _write_wf(cli_env.tmp, yaml_text))
    assert boot["done"] is False and boot["node"] == "b"
    assert boot["prompt"]
    assert [e["node"] for e in boot["auto_executed"]] == ["s1", "s2"]
    # 非 B8 路径：marker 已写
    assert list(cli_env.tmp.glob("runs/orca-*.json")), "entry 链停 agent 应写 marker"


def test_cli_entry_script_done_short_circuit_ac7b_b8(cli_env):
    """AC7 后半 / B8：entry script 直通 $end → done=true，不写 marker/env/守护/web/注册。"""
    yaml_text = """\
name: entry_end_cli_wf
description: entry script 直通 $end（B8 短路）。
entry: s
nodes:
  - name: s
    kind: script
    command: "echo solo"
    routes:
      - to: $end
"""
    runner = CliRunner()
    boot = _bootstrap(runner, _write_wf(cli_env.tmp, yaml_text))
    assert boot["done"] is True
    assert boot["reason"] == "completed"
    assert [e["node"] for e in boot["auto_executed"]] == ["s"]
    assert "solo" in boot["auto_executed"][0]["stdout_tail"]
    # B8：锁外全套动作全部跳过
    assert not list(cli_env.tmp.glob("runs/orca-*.json")), "终态短路不应写 marker"
    cli_env.cli._register_current_project.assert_not_called()
    cli_env.cli._spawn_chart_daemon.assert_not_called()
    cli_env.cli._spawn_sidechain_daemon.assert_not_called()
    cli_env.cli._spawn_open_web.assert_not_called()
    run_dirs = list((cli_env.tmp / "runs").glob("*/orca_env.sh"))
    assert not run_dirs, "终态短路不应写 env 文件"
    assert not list((cli_env.tmp / "runs").glob("*/artifacts")), "终态短路不应 mkdir artifacts"
    # tape 终态完整：ws → ns → nc → rt($end) → workflow_completed
    tape_path = Path(boot["tape"])
    assert _pairs(tape_path) == [
        ("workflow_started", None),
        ("node_started", "s"), ("node_completed", "s"),
        ("route_taken", None), ("workflow_completed", None),
    ]


def test_cli_window_i_recovery_ac9(cli_env):
    """AC9 前半：tape 截断至 ns(S)（窗口 i）→ 不带 --output 重执行 S 正常推进 + 计数不增。"""
    p = _write_wf(cli_env.tmp, CLI_YAML_A_S_B)
    from orca.iface.in_session.marker import (
        ActivationMarker, marker_path, read_marker, write_marker,
    )
    tape_path = cli_env.tmp / "runs" / "r-win.jsonl"
    tape_path.parent.mkdir(parents=True, exist_ok=True)
    tape = Tape(tape_path, run_id="r-win", resume=True)
    bus = EventBus(tape)
    _seed_window_i(bus, _wf_asb(), yaml_path=p)
    bus.close()
    write_marker(marker_path(tape_path.parent, "r-win"),
                 ActivationMarker(run_id="r-win", model="m", no_output_count=0))

    runner = CliRunner()
    r = _next(runner, str(tape_path), "r-win")            # 不带 --output
    assert r["done"] is False and r["node"] == "b"
    assert [e["node"] for e in r["auto_executed"]] == ["s"]
    pairs = _pairs(tape_path)
    # at-least-once：tape 允许同节点重复 ns（S 重执行）
    assert pairs.count(("node_started", "s")) == 2
    assert pairs.count(("node_completed", "s")) == 1
    # 合规计数不增（最终 result emits 非空；中间 emits=[] 不计数）
    m = read_marker(marker_path(tape_path.parent, "r-win"))
    assert m is not None and m.no_output_count == 0


def test_cli_window_i_with_output_state_corrupt_ac9(cli_env):
    """AC9 后半：窗口 i 带 --output 交卷 → state_corrupt fail loud。"""
    p = _write_wf(cli_env.tmp, CLI_YAML_A_S_B)
    tape_path = cli_env.tmp / "runs" / "r-win2.jsonl"
    tape_path.parent.mkdir(parents=True, exist_ok=True)
    tape = Tape(tape_path, run_id="r-win2", resume=True)
    bus = EventBus(tape)
    _seed_window_i(bus, _wf_asb(), yaml_path=p)
    bus.close()
    from orca.iface.in_session.marker import ActivationMarker, marker_path, write_marker
    write_marker(marker_path(tape_path.parent, "r-win2"),
                 ActivationMarker(run_id="r-win2", model="m", no_output_count=0))

    runner = CliRunner()
    r = _next(runner, str(tape_path), "r-win2", "--output", "HIJACK",
              expect_exit=1)
    assert r["done"] is True and r["error_kind"] == "state_corrupt"


def test_cli_d4_daemon_ensure_before_script_chain(cli_env):
    """D4：next 即将进 script 链前前置 ensure（终态 done 下尾部 ensure 跳过 → 被调即前置证据）。"""
    yaml_text = """\
name: script_d4_cli_wf
description: a → s → $end（D4 前置 ensure 判定）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 step A 的输出。"
    routes:
      - to: s
  - name: s
    kind: script
    command: "echo s-d4"
    routes:
      - to: $end
"""
    runner = CliRunner()
    boot = _bootstrap(runner, _write_wf(cli_env.tmp, yaml_text))
    r = _next(runner, boot["tape"], boot["run_id"], "--output", "OUT-A")
    assert r["done"] is True                     # a → s → $end：尾部 ensure 跳过
    cli_env.ensures["_ensure_chart_daemon"].assert_called()
    cli_env.ensures["_ensure_sidechain_daemon"].assert_called()


def test_busy_reply_hint_u1():
    """U1：busy 信封 hint 含通用提示（kill 持锁 next / 锁随进程释放）。"""
    import contextlib
    import io

    from orca.iface.in_session import cli as cli_mod

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli_mod._echo_busy_reply()
    data = json.loads(buf.getvalue())
    assert data["reason"] == "busy" and data["retry_after_ms"] == 500
    assert "script" in data["hint"] and "kill" in data["hint"]


# ── daemon（AC10 daemon 部分）───────────────────────────────────────────────


def _bare_daemon(tmp_path: Path, wf: Workflow, run_id: str = "r-daemon"):
    """绕开 __init__（flock/signal）直构 daemon：真 tape/bus（内联 script 要写 tape）。"""
    from orca.iface.in_session.daemon import InSessionDaemon
    inst = InSessionDaemon.__new__(InSessionDaemon)
    inst.wf = wf
    inst.run_id = run_id
    inst.inputs = {}
    tape = Tape(tmp_path / f"{run_id}.jsonl", run_id=run_id, resume=True)
    inst.tape = tape
    inst.bus = EventBus(tape)
    inst._pending_output = None
    inst._host_alive_ts = 0.0
    return inst


def test_daemon_next_script_passthrough_ac10(tmp_path):
    """AC10 daemon：next 共享循环透传 script（auto_executed 在回复）+ 停在 agent。"""
    inst = _bare_daemon(tmp_path, _wf_asb())
    r1 = asyncio.run(inst.next())
    assert r1["done"] is False and r1["node"] == "a"
    assert "auto_executed" not in r1
    inst.observe("OUT-A")
    r2 = asyncio.run(inst.next())
    assert r2["done"] is False and r2["node"] == "b"
    assert [e["node"] for e in r2["auto_executed"]] == ["s"]
    assert "s-out" in r2["auto_executed"][0]["stdout_tail"]
    inst.bus.close()


def test_daemon_next_script_failure_in_try_scope_ac10(tmp_path):
    """AC10 daemon B4：循环内 script 失败被 daemon 的 except InSessionError 接住 →
    fail_in_session 信封（error_kind + tape workflow_failed）。"""
    wf = Workflow(
        name="daemon_fail_wf", entry="s",
        nodes=[
            ScriptNode(name="s", command="echo {{ inputs.absent }}",
                       routes=[Route(to="b")]),
            AgentNode(name="b", executor="opencode", model="d/d",
                      prompt="do B", routes=[Route(to="$end")]),
        ],
    )
    inst = _bare_daemon(tmp_path, wf, "r-daemon-fail")
    r = asyncio.run(inst.next())
    assert r["done"] is True
    assert r["error_kind"] == "internal_error"   # render 失败映射（§2.6 行 4）
    assert "auto_executed" not in r              # 首个 script 即失败
    types = [e.type for e in inst.tape.replay()]
    assert types[-1] == "workflow_failed"
    assert "error" not in types
    inst.bus.close()
