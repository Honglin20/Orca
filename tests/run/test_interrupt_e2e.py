"""test_interrupt_e2e.py —— 中断 e2e 契约测试（phase 11 §3/§4 wave-1 coverage gap 填充）。

补 implementer 已有测试的 e2e GAPS（不重复 unit/单分支测试）：
  - SKIP 分支经 orchestrator drive_loop 端到端：tape 含 node_skipped + 跳过的 node 不执行
    + 下游 node 续跑 + workflow_completed。
  - ABORT 分支经 orchestrator drive_loop 端到端：tape 含 workflow_failed
    {error_type: WorkflowAborted} + 终态 failed。
  - 中断配对不变量：每个 interrupt_requested 都有配对的 interrupt_resolved（CONTINUE/SKIP/ABORT
    三分支均覆盖）——这是 wave-1 e2e 审计发现的 critical bug 的回归保护（修复前
    abort/skip 分支的 interrupt_resolved 被 async broadcaster 与 bus.close() 竞态丢失）。
  - 多壳 await-future 路径经 orchestrator：``request_interrupt(ireq)``（不带 answer）
    → drive_loop node 边界 ``await handler.request`` 阻塞 → ``handler.resolve`` set_result
    → 编排恢复。P3 web/mcp 路径的回归保护（CLI 单壳 record_resolved 不应让它腐烂）。
  - prompt_rendered 不变量：每个 agent spawn 都 emit 一次 prompt_rendered（多 node 流）。

驱动方式（SPEC wave-1 测试约束）：真 Tape / 真 EventBus / 真 Orchestrator / 真 InterruptHandler，
仅 claude spawn 用 fake executor 替身（确定性，不依赖外部 LLM）。断言走 tape 事件流 +
replay_state（可观测结果），不戳私有方法。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest
import yaml

from orca.compile import load_workflow
from orca.events.bus import EventBus
from orca.events.replay import replay_state
from orca.events.tape import Tape
from orca.exec.context import RunContext
from orca.gates.interrupt import InterruptHandler
from orca.gates.types import InterruptRequest
from orca.run.orchestrator import Orchestrator


def run_async(coro):
    return asyncio.run(coro)


# ── helpers ──────────────────────────────────────────────────────────────────


def _linear_2agent_wf_yaml(tmp_path) -> str:
    """2-agent 线性 wf：a (agent) → b (agent) → $end。

    双 agent 让 SKIP / prompt_rendered-on-every-spawn 不变量能观测「下游 node 是否跑」。
    """
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump({
        "name": "t",
        "entry": "a",
        "nodes": [
            {"name": "a", "kind": "agent", "prompt": "do A",
             "routes": [{"to": "b"}]},
            {"name": "b", "kind": "agent", "prompt": "do B",
             "routes": [{"to": "$end"}]},
        ],
    }), encoding="utf-8")
    return str(p)


class _RecordingAgentExecutor:
    """记录每次 spawn 的 fake agent executor（不真 spawn claude）。

    每次 exec 记录 node 名 + 真 render_prompt 渲染（覆盖 guidance 注入路径），emit
    node_started → prompt_rendered → node_completed。执行顺序可观测（``spawned`` 列表）。
    """

    def __init__(self) -> None:
        self.spawned: list[str] = []

    async def exec(self, node, ctx: RunContext):
        from orca.exec.render import render_prompt
        from orca.schema import Event

        self.spawned.append(node.name)
        session_id = uuid.uuid4().hex
        prompt = render_prompt(node, ctx)

        def _ev(t: str, data: dict) -> Event:
            return Event(seq=0, type=t, timestamp=time.time(),  # type: ignore[arg-type]
                         node=node.name, session_id=session_id, data=data)

        yield _ev("node_started", {"executor": "fake", "kind": "agent"})
        yield _ev("prompt_rendered", {
            "node": node.name, "session_id": session_id, "preview": prompt[-200:],
        })
        yield _ev("node_completed", {"output": {"result": f"fake-{node.name}"}, "elapsed": 0.01})


def _patch_factory_to_fake(fake_exec: _RecordingAgentExecutor):
    """monkeypatch orca.exec.factory.make_executor 让所有 node 用同一 fake executor 实例。"""
    import orca.exec.factory as factory_mod

    orig = factory_mod.make_executor
    factory_mod.make_executor = lambda node, agent_tools_server=None, bus=None, **kwargs: fake_exec
    return orig


def _restore_factory(orig):
    import orca.exec.factory as factory_mod
    factory_mod.make_executor = orig


# ── SKIP 分支端到端（SPEC §3.1 / §10.2 item12）──────────────────────────────


def test_e2e_skip_advances_to_next_node_without_executing_current(tmp_path):
    """SKIP 经 drive_loop 端到端：当前 node 标 skipped（node_skipped 写 Tape）+ 不执行 +
    下游 node 续跑 + workflow_completed。

    驱动：request_interrupt(ireq, answer=("skip", None)) 在 drive_loop 启动前登记。
    node=a 边界消费 → emit node_skipped{a} → b 续跑 → completed。断言可观测：
      - tape 含 node_skipped（reason=user_interrupt_skip）；
      - fake executor 的 spawned 不含 a（a 被跳过未执行）但含 b；
      - 终态 completed。
    """
    wf = load_workflow(_linear_2agent_wf_yaml(tmp_path))
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    interrupt_handler = InterruptHandler(bus)
    fake_exec = _RecordingAgentExecutor()
    orig = _patch_factory_to_fake(fake_exec)

    async def scenario():
        await interrupt_handler.start()
        try:
            orch = Orchestrator(
                wf, bus, inputs={}, run_id="r1", interrupt_handler=interrupt_handler,
            )
            ireq = InterruptRequest(
                id="i1", node="a", run_id="r1", session_id="sess-a",
                elapsed_at_request=0.5,
            )
            orch.request_interrupt(ireq, answer=("skip", None))

            drive_task = asyncio.create_task(orch._drive_loop())
            await asyncio.wait_for(drive_task, timeout=5.0)
        finally:
            await interrupt_handler.stop()
            _restore_factory(orig)
            bus.close()

    run_async(scenario())

    # 断言 tape 可观测结果（drive_loop 直接驱动，不含 workflow_started/completed 生命周期
    # 事件——那由 run() 负责，与现有 continue e2e 同款驱动方式）。
    types = [e.type for e in tape.replay()]
    assert "node_skipped" in types, f"SKIP 应 emit node_skipped, got {types}"
    skipped = next(e for e in tape.replay() if e.type == "node_skipped")
    assert skipped.node == "a"
    assert skipped.data["reason"] == "user_interrupt_skip"

    # a 被跳过未执行；b 续跑（fake 记录执行顺序）。
    assert "a" not in fake_exec.spawned, f"a 应被 skip 不执行, spawned={fake_exec.spawned}"
    assert "b" in fake_exec.spawned, f"b 应续跑, spawned={fake_exec.spawned}"


# ── ABORT 分支端到端（SPEC §3.1 / §3.2 abort payload）───────────────────────


def test_e2e_abort_emits_workflow_failed_with_abort_reason(tmp_path):
    """ABORT 经 drive_loop 端到端：workflow 立即中止 → tape 含 workflow_failed
    {error_type: WorkflowAborted, node: <abort 时的 node>}。

    断言可观测：终态 failed + workflow_failed.data.error_type == "WorkflowAborted"。
    """
    wf = load_workflow(_linear_2agent_wf_yaml(tmp_path))
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    interrupt_handler = InterruptHandler(bus)
    fake_exec = _RecordingAgentExecutor()
    orig = _patch_factory_to_fake(fake_exec)

    async def scenario():
        await interrupt_handler.start()
        try:
            orch = Orchestrator(
                wf, bus, inputs={}, run_id="r1", interrupt_handler=interrupt_handler,
            )
            ireq = InterruptRequest(
                id="i1", node="a", run_id="r1", session_id="sess-a",
                elapsed_at_request=0.5,
            )
            orch.request_interrupt(ireq, answer=("abort", None))

            # drive_loop 会因 WorkflowAborted raise；orch.run() 接住 emit workflow_failed。
            # 直接调 run() 走完整 lifecycle（含 workflow_started + workflow_failed）。
            await asyncio.wait_for(orch.run(), timeout=5.0)
        finally:
            await interrupt_handler.stop()
            _restore_factory(orig)

    run_async(scenario())

    types = [e.type for e in tape.replay()]
    assert "workflow_failed" in types, f"ABORT 应 emit workflow_failed, got {types}"
    failed = next(e for e in tape.replay() if e.type == "workflow_failed")
    assert failed.data["kind"] == "business_gate"
    # node 在 data["node"]（make_workflow_failed 把 node 放 payload，非 event 顶层）。
    assert failed.data["node"] == "a"  # abort 时的 current node
    assert "workflow_completed" not in types  # 中止不完成

    # 终态 failed（replay_state 从 tape 派生）。
    state = replay_state(Tape(tmp_path / "events.jsonl", run_id="r1"))
    assert state.status == "failed"


# ── 中断配对不变量（SPEC §3.2 / 单 Tape 唯一真相源）─────────────────────────


@pytest.mark.parametrize("action,guidance", [
    ("continue", "调整方向"),
    ("continue", None),
    ("skip", None),
    ("abort", None),
])
def test_invariant_every_interrupt_requested_has_paired_resolved(
    tmp_path, action, guidance,
):
    """契约不变量：tape 上每个 interrupt_requested 都有配对的 interrupt_resolved
    （id 一致），guidance 在 resolved 事件中原样回传（round-trip）。

    三分支 + 有/无 guidance 全覆盖。这是「单 Tape 唯一真相源 + 配对完整性」的核心断言：
    任何分支都不应只写 requested 不写 resolved（会让三壳状态漂移）。

    回归保护：wave-1 e2e 审计发现 abort/skip（continue 偶发）分支的 interrupt_resolved
    被 async broadcaster 与 run() 的 bus.close() 竞态丢失——record_resolved 修复后
    resolved 同步写 Tape，本断言守护该不变量不被破坏。
    """
    wf = load_workflow(_linear_2agent_wf_yaml(tmp_path))
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    interrupt_handler = InterruptHandler(bus)
    fake_exec = _RecordingAgentExecutor()
    orig = _patch_factory_to_fake(fake_exec)

    async def scenario():
        await interrupt_handler.start()
        try:
            orch = Orchestrator(
                wf, bus, inputs={}, run_id="r1", interrupt_handler=interrupt_handler,
            )
            ireq = InterruptRequest(
                id="inv-1", node="a", run_id="r1", session_id="sess-a",
                elapsed_at_request=0.5,
            )
            orch.request_interrupt(ireq, answer=(action, guidance))
            # run() 会因 abort raise + emit workflow_failed；continue/skip 正常完成。
            await asyncio.wait_for(orch.run(), timeout=5.0)
        finally:
            await interrupt_handler.stop()
            _restore_factory(orig)

    run_async(scenario())

    events = list(tape.replay())
    requested = [e for e in events if e.type == "interrupt_requested"]
    resolved = [e for e in events if e.type == "interrupt_resolved"]
    assert len(requested) == 1, f"应恰好 1 个 requested, got {len(requested)}"
    assert len(resolved) == 1, f"应恰好 1 个 resolved, got {len(resolved)}"

    # 配对：id 一致。
    assert requested[0].data["interrupt_id"] == resolved[0].data["interrupt_id"]
    # action 一致。
    assert resolved[0].data["action"] == action
    # guidance round-trip（continue 带话 / 其余 None）。
    assert resolved[0].data["guidance"] == guidance


# ── 多壳 await-future 路径经 orchestrator（P3 web/mcp 回归保护，SPEC §11.1）──


def test_e2e_multishell_await_future_path_through_orchestrator(tmp_path):
    """多壳路径（``answer=None`` → ``await handler.request``）经 orchestrator 端到端可用。

    SPEC §11.1 偏离：CLI 单壳走 record_resolved（不经 await-future），多壳走 request/resolve
    await-future 竞速留给 P3 web/mcp。本测试是 P3 路径的**回归保护**——确认 orchestrator
    ``_handle_interrupt`` 的 else 分支（``await handler.request(ireq)``）仍正确驱动编排，
    不因 CLI 单壳路径的引入而腐烂。

    驱动：request_interrupt(ireq)（**不带** answer）→ drive_loop node=a 边界 await
    handler.request 阻塞 → 主 task 让出 → 外部 task 调 handler.resolve(...) set_result →
    编排恢复 continue + guidance。断言可观测：tape interrupt 配对 + b 续跑 + completed。
    """
    wf = load_workflow(_linear_2agent_wf_yaml(tmp_path))
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    interrupt_handler = InterruptHandler(bus)
    fake_exec = _RecordingAgentExecutor()
    orig = _patch_factory_to_fake(fake_exec)

    async def scenario():
        await interrupt_handler.start()
        try:
            orch = Orchestrator(
                wf, bus, inputs={}, run_id="r1", interrupt_handler=interrupt_handler,
            )
            ireq = InterruptRequest(
                id="ms-1", node="a", run_id="r1", session_id="sess-a",
                elapsed_at_request=0.5,
            )
            # 多壳路径：不带 answer。
            orch.request_interrupt(ireq)

            drive_task = asyncio.create_task(orch._drive_loop())

            # 等 node=a 边界的 await handler.request 注册 future（requested 写 Tape 后）。
            for _ in range(50):
                if interrupt_handler.has_pending("ms-1"):
                    break
                await asyncio.sleep(0.02)
            assert interrupt_handler.has_pending("ms-1"), "多壳路径 future 应已注册"

            # 模拟「某壳答了」：resolve set_result 唤醒 await。
            ok = interrupt_handler.resolve("ms-1", "continue", "多壳纠偏", "web")
            assert ok is True

            await asyncio.wait_for(drive_task, timeout=5.0)
        finally:
            await interrupt_handler.stop()
            _restore_factory(orig)
            bus.close()

    run_async(scenario())

    # 断言：配对完整 + guidance round-trip + 两个 node 都跑（continue 不跳过）。
    events = list(tape.replay())
    resolved = next(e for e in events if e.type == "interrupt_resolved")
    assert resolved.data["action"] == "continue"
    assert resolved.data["guidance"] == "多壳纠偏"
    assert resolved.data["resolved_by"] == "web"  # 多壳 source

    # a 重 spawn（continue）+ b 续跑都执行（prompt_rendered 2 个）。
    assert fake_exec.spawned == ["a", "b"], f"continue 应执行 a+b, got {fake_exec.spawned}"

    # a 的 prompt_rendered preview 含 [User Guidance]（guidance 经 await-future 路径注入）。
    prompt_events = [e for e in events if e.type == "prompt_rendered"]
    assert len(prompt_events) == 2
    a_preview = next(e for e in prompt_events if e.data["node"] == "a").data["preview"]
    assert "[User Guidance]" in a_preview
    assert "多壳纠偏" in a_preview


# ── prompt_rendered 不变量：每个 agent spawn 都 emit（SPEC §2.2 B5）─────────


def test_invariant_prompt_rendered_emitted_on_every_agent_spawn(tmp_path):
    """契约不变量：每个 agent spawn 都 emit 一个 prompt_rendered（一一对应）。

    SPEC §2.2 B5：prompt_rendered 是 guidance 注入的可观测证据。多 node 流应保证每个
    agent node 都有（漏 emit 会让三壳无法看到某次 spawn 的 prompt 拼接）。

    驱动：2-agent wf 正常跑（无 interrupt）→ 断言 prompt_rendered 数 == agent spawn 数 ==
    2，且每个 preview 有界（≤200 字符）。
    """
    wf = load_workflow(_linear_2agent_wf_yaml(tmp_path))
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    fake_exec = _RecordingAgentExecutor()
    orig = _patch_factory_to_fake(fake_exec)

    async def scenario():
        orch = Orchestrator(wf, bus, inputs={}, run_id="r1")
        try:
            await asyncio.wait_for(orch.run(), timeout=5.0)
        finally:
            _restore_factory(orig)
            bus.close()

    run_async(scenario())

    events = list(tape.replay())
    prompt_events = [e for e in events if e.type == "prompt_rendered"]
    assert len(prompt_events) == 2, f"2 agent 应 2 个 prompt_rendered, got {len(prompt_events)}"
    # 每个 prompt_rendered 都有 node 名 + preview 有界。
    nodes_with_prompt = {e.data["node"] for e in prompt_events}
    assert nodes_with_prompt == {"a", "b"}
    for e in prompt_events:
        assert len(e.data["preview"]) <= 200
        assert e.data["session_id"]  # 非空 session_id
    # spawn 数与 prompt_rendered 数一一对应。
    assert len(fake_exec.spawned) == len(prompt_events)


# ── emit-on-closed-bus fail-loud 契约（修复因果前提的回归保护）──────────────


def test_invariant_emit_on_closed_bus_raises_loud(tmp_path):
    """契约不变量：``bus.close()`` 后再 ``await bus.emit(...)`` 必须 raise（fail loud）。

    本测试锁定 record_resolved 同步 emit 修复的**因果前提**：closed bus 上的 emit 不静默
    no-op，而是抛 RuntimeError（Tape.append 第一动作校验 _closed）。修复前 async broadcaster
    的 emit 撞此 RuntimeError 被 except 吞成 error log，事件永久丢失——正是 critical bug 的
    根因。本断言确保该 fail-loud 契约不被破坏：若未来 EventBus/Tape 改成对 closed 状态
    静默返回，则竞态回归不会被配对不变量测试捕获（emit 不抛 = broadcaster 不吞 = 测试仍绿
    但事件实丢），故必须独立锁定。
    """
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    bus.close()  # 先关（模拟 run() 收尾）

    async def attempt_emit():
        await bus.emit("node_started", {"x": 1})

    # closed bus 的 emit 必须 fail loud（经 Tape.append 抛 RuntimeError）。
    with pytest.raises(RuntimeError, match="已 close"):
        run_async(attempt_emit())


# ─────────────────────────────────────────────────────────────────────────────
# 修复历史（wave-1 e2e coverage 审计 → critical bug 修复，2026-07-02）
# ─────────────────────────────────────────────────────────────────────────────
# 本文件原含 6 个 xfail(strict=True) 测试，记录 wave-1 实现的 critical bug：
# CLI 单壳路径 record_resolved 把 interrupt_resolved 投给 async broadcaster 异步 emit，
# abort/skip（continue 偶发）分支 drive_loop 极快结束 → run() 的 bus.close() 早于
# broadcaster flush → interrupt_resolved emit 撞 "Tape 已 close" 被吞，事件永久丢失
# （违反单 Tape 配对不变量 + 唯一真相源铁律）。
#
# 修复（Option A）：orca/gates/interrupt.py::record_resolved 改为同步 await bus.emit
# 写 interrupt_resolved 到 Tape（+ 同步 fan-out 订阅者），不经 async broadcaster。
# broadcaster 仅留给同步 resolve() 入口（它无法 await emit）。修复后 6 个 xfail
# 转 xpass，markers 移除，本断言组成为该不变量的回归保护。
# ─────────────────────────────────────────────────────────────────────────────
