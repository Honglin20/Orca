"""test_interrupt_orchestrator.py —— Orchestrator _handle_interrupt + guidance 注入（phase 11 §4）。

覆盖（计划 P1.1 Step B 验收）：
  - continue 分支累积 guidance 进 ctx（_make_ctx 注入 → render_prompt 含 [User Guidance]）
  - abort 分支 raise WorkflowAborted
  - skip 分支推进下一 node（不执行当前）
  - E2E（fake executor + fake interrupt_handler）：tape 含 interrupt_requested +
    interrupt_resolved{guidance} 配对，重 spawn 的 prompt_rendered preview 含 [User Guidance]
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import yaml

from orca.compile import load_workflow
from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.exec.context import RunContext
from orca.gates.interrupt import InterruptHandler
from orca.gates.types import InterruptRequest
from orca.run.errors import WorkflowAborted
from orca.run.orchestrator import Orchestrator


def run_async(coro):
    return asyncio.run(coro)


def _linear_wf_yaml(tmp_path) -> str:
    """2-node 线性 wf：a (agent) → b (script) → $end。a 有 when=None 兜底 route 供 skip 测试。"""
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump({
        "name": "t",
        "entry": "a",
        "nodes": [
            {"name": "a", "kind": "agent", "prompt": "do A",
             "routes": [{"to": "b"}]},
            {"name": "b", "kind": "script", "command": "echo b",
             "routes": [{"to": "$end"}]},
        ],
    }), encoding="utf-8")
    return str(p)


def _make_orch(tmp_path, interrupt_handler=None) -> Orchestrator:
    wf = load_workflow(_linear_wf_yaml(tmp_path))
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    return Orchestrator(wf, bus, inputs={}, run_id="r1", interrupt_handler=interrupt_handler)


class _FakeInterruptHandler:
    """InterruptHandler 替身：preset (action, guidance)，request 立即返回。

    避免真起 broadcaster（单测 _handle_interrupt 的分支逻辑，不测 handler 内部）。
    """

    def __init__(self, action: str, guidance: str | None = None) -> None:
        self._action = action
        self._guidance = guidance

    async def request(self, ireq: InterruptRequest) -> tuple[str, str | None]:
        return (self._action, self._guidance)


# ── _handle_interrupt 分支 ──────────────────────────────────────────────────


def test_handle_interrupt_continue_accumulates_guidance(tmp_path):
    """continue + guidance → _guidance_acc 追加 → _make_ctx 注入 ctx.user_guidance。"""

    async def scenario():
        orch = _make_orch(tmp_path, interrupt_handler=_FakeInterruptHandler("continue", "用 CPU"))
        ireq = InterruptRequest(
            id="i1", node="a", run_id="r1", session_id="s1", elapsed_at_request=1.0,
        )
        orch._interrupt_pending = ireq
        action = await orch._handle_interrupt("a", {})
        assert action == "continue"
        assert orch._guidance_acc == ["用 CPU"]
        # _make_ctx 注入 guidance
        ctx = orch._make_ctx({})
        assert ctx.user_guidance == ("用 CPU",)
        orch.bus.close()

    run_async(scenario())


def test_handle_interrupt_continue_no_guidance_no_accumulation(tmp_path):
    """continue 无 guidance → _guidance_acc 不变。"""

    async def scenario():
        orch = _make_orch(tmp_path, interrupt_handler=_FakeInterruptHandler("continue", None))
        ireq = InterruptRequest(id="i1", node="a", run_id="r1", elapsed_at_request=1.0)
        orch._interrupt_pending = ireq
        await orch._handle_interrupt("a", {})
        assert orch._guidance_acc == []
        orch.bus.close()

    run_async(scenario())


def test_handle_interrupt_abort_returns_abort(tmp_path):
    """abort → _handle_interrupt 返回 abort（drive_loop 据 raise WorkflowAborted）。"""

    async def scenario():
        orch = _make_orch(tmp_path, interrupt_handler=_FakeInterruptHandler("abort"))
        ireq = InterruptRequest(id="i1", node="a", run_id="r1", elapsed_at_request=1.0)
        orch._interrupt_pending = ireq
        action = await orch._handle_interrupt("a", {})
        assert action == "abort"
        orch.bus.close()

    run_async(scenario())


def test_handle_interrupt_skip_returns_skip(tmp_path):
    """skip → _handle_interrupt 返回 skip（drive_loop 据此推进下一 node）。"""

    async def scenario():
        orch = _make_orch(tmp_path, interrupt_handler=_FakeInterruptHandler("skip"))
        ireq = InterruptRequest(id="i1", node="a", run_id="r1", elapsed_at_request=1.0)
        orch._interrupt_pending = ireq
        action = await orch._handle_interrupt("a", {})
        assert action == "skip"
        orch.bus.close()

    run_async(scenario())


def test_handle_interrupt_consumes_pending(tmp_path):
    """_handle_interrupt 消费 _interrupt_pending（调用后置 None）。"""

    async def scenario():
        orch = _make_orch(tmp_path, interrupt_handler=_FakeInterruptHandler("continue", "g"))
        ireq = InterruptRequest(id="i1", node="a", run_id="r1", elapsed_at_request=1.0)
        orch._interrupt_pending = ireq
        await orch._handle_interrupt("a", {})
        assert orch._interrupt_pending is None
        orch.bus.close()

    run_async(scenario())


# ── request_interrupt 公开方法（SPEC §2.3 测试A）─────────────────────────────


def test_request_interrupt_sets_pending(tmp_path):
    """request_interrupt(ireq) 设置 _interrupt_pending；带 answer 同时设 _interrupt_answer。"""

    async def scenario():
        orch = _make_orch(tmp_path, interrupt_handler=_FakeInterruptHandler("continue"))
        ireq = InterruptRequest(id="i1", node="a", run_id="r1", elapsed_at_request=1.0)
        assert orch._interrupt_pending is None
        # CLI 单壳路径：带 answer
        orch.request_interrupt(ireq, answer=("continue", "g1"))
        assert orch._interrupt_pending is ireq
        assert orch._interrupt_answer == ("continue", "g1")
        # 多壳路径：不带 answer
        ireq2 = InterruptRequest(id="i2", node="a", run_id="r1", elapsed_at_request=1.0)
        orch.request_interrupt(ireq2)
        assert orch._interrupt_answer is None
        orch.bus.close()

    run_async(scenario())


def test_request_interrupt_without_handler_warns(tmp_path, caplog):
    """无 interrupt_handler 注入 → request_interrupt 不设 pending + warning（fail loud）。"""
    caplog.set_level("WARNING")

    async def scenario():
        orch = _make_orch(tmp_path, interrupt_handler=None)
        ireq = InterruptRequest(id="i1", node="a", run_id="r1", elapsed_at_request=1.0)
        orch.request_interrupt(ireq)
        assert orch._interrupt_pending is None  # 未设
        orch.bus.close()

    run_async(scenario())
    assert "i1" in caplog.text


# ── E2E：fake executor + 真 InterruptHandler，tape 配对 + prompt_rendered 含 guidance ──


class _RecordingAgentExecutor:
    """记录 prompt_rendered 的 fake agent executor（避免真 spawn claude）。

    每次执行 emit node_started → prompt_rendered（preview=实际 prompt 末尾 200 字符，
    含 [User Guidance] 段当 ctx 有 guidance）→ node_completed。prompt 由 render_prompt 真渲染，
    故 guidance 注入路径被真实覆盖。
    """

    def __init__(self):
        self.rendered_prompts: list[str] = []

    async def exec(self, node, ctx: RunContext):
        import time
        from orca.exec.render import render_prompt
        from orca.schema import Event

        session_id = uuid.uuid4().hex
        prompt = render_prompt(node, ctx)
        self.rendered_prompts.append(prompt)

        def _ev(t: str, data: dict) -> Event:
            return Event(seq=0, type=t, timestamp=time.time(),  # type: ignore[arg-type]
                         node=node.name, session_id=session_id, data=data)

        yield _ev("node_started", {"executor": "fake", "kind": "agent"})
        yield _ev("prompt_rendered", {
            "node": node.name, "session_id": session_id, "preview": prompt[-200:],
        })
        yield _ev("node_completed", {"output": {"result": "fake-output"}, "elapsed": 0.01})


def test_e2e_interrupt_continue_guidance_renders_in_respawn(tmp_path):
    """E2E（SPEC §10.2 item3 B5）：continue + guidance → tape interrupt 配对 + 重 spawn
    prompt_rendered preview 含 [User Guidance]。

    驱动 orchestrator 用真 InterruptHandler（resolve 同步触发）+ fake agent executor
    （真 render_prompt 覆盖 guidance 注入路径）。
    """
    import yaml as _yaml

    # wf: a (agent) → $end（最小，单 node）
    p = tmp_path / "wf.yaml"
    p.write_text(_yaml.safe_dump({
        "name": "t", "entry": "a",
        "nodes": [{"name": "a", "kind": "agent", "prompt": "do A",
                   "routes": [{"to": "$end"}]}],
    }), encoding="utf-8")
    wf = load_workflow(str(p))
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    interrupt_handler = InterruptHandler(bus)
    fake_exec = _RecordingAgentExecutor()

    # monkeypatch make_executor 让 agent node 用 fake executor
    import orca.exec.factory as factory_mod
    import orca.run.executor_adapter as adapter_mod
    orig_make_executor = factory_mod.make_executor
    factory_mod.make_executor = lambda node: fake_exec
    # execute_and_emit 从 factory 取 executor；它 import 的是模块级引用，patch factory_mod 即可

    async def scenario():
        await interrupt_handler.start()
        try:
            orch = Orchestrator(
                wf, bus, inputs={}, run_id="r1", interrupt_handler=interrupt_handler,
            )
            # CLI 单壳路径（SPEC §3.1 真实时序）：在 drive_loop 启动前，用户已通过 modal 答完，
            # request_interrupt 携带 answer。drive_loop 第一轮（node=a 边界）消费 pending →
            # _handle_interrupt record_resolved（emit requested + 入队 resolved）→ 累积 guidance →
            # node a 用含 guidance 的 ctx 执行。无 await-future 死锁（review §2.1 修复验证）。
            ireq = InterruptRequest(
                id="i1", node="a", run_id="r1", session_id="sess-a",
                elapsed_at_request=0.5,
            )
            orch.request_interrupt(ireq, answer=("continue", "skip weights"))

            drive_task = asyncio.create_task(orch._drive_loop())
            await asyncio.wait_for(drive_task, timeout=5.0)
        finally:
            await interrupt_handler.stop()
            factory_mod.make_executor = orig_make_executor
            bus.close()

    run_async(scenario())

    # 验证 tape
    types = [e.type for e in tape.replay()]
    assert "interrupt_requested" in types
    resolved = [e for e in tape.replay() if e.type == "interrupt_resolved"]
    assert len(resolved) == 1
    assert resolved[0].data["action"] == "continue"
    assert resolved[0].data["guidance"] == "skip weights"

    # 验证 node a 的 prompt_rendered 含 [User Guidance] + guidance 文本（SPEC §10.2 item3 B5）。
    # 架构：interrupt 在 node a 边界**前**消费（pending 在 drive_loop 首轮就被 _handle_interrupt
    # 吃掉），故 node a 只执行一次——但那次执行的 ctx 已含累积的 guidance（_make_ctx 注入），
    # render_prompt 拼 [User Guidance] 段。这是 continue + guidance 的可观测证据。
    prompt_events = [e for e in tape.replay() if e.type == "prompt_rendered"]
    assert len(prompt_events) == 1, f"node a 应只跑一次（边界前消费 interrupt），got {len(prompt_events)}"
    preview = prompt_events[0].data["preview"]
    assert "[User Guidance]" in preview
    assert "skip weights" in preview
    assert "Incorporate this guidance" in preview
