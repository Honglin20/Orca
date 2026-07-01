"""test_skip_to_agent.py —— phase 11 §9 P4 Skip to Agent 端到端契约测试。

覆盖 SPEC §9（task1-4）+ §10.2 item12：
  - SKIP 无显式目标 + 兜底 route → 沿 route 跳（wave-1 行为不变，回归保护）。
  - SKIP + 显式目标 node → _drive_loop 直接跳该 node（不经 route 求值，避免 NoRouteMatch）。
  - SKIP + 不存在目标 → fail loud（ValueError，clear error，非静默崩溃）。
  - skipped node 的 None output 在下游 ``{{ skipped.output.field }}`` 求值时容错（SPEC §9.2）。
  - skip_target 写进 tape 的 interrupt_resolved.data（可观测，SPEC §9 task4）。

驱动方式（与 test_interrupt_e2e.py 同款）：真 Tape / EventBus / Orchestrator / InterruptHandler，
仅 claude spawn 用 fake executor（确定性）。断言走 tape 事件流 + replay_state。
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
from orca.events.tape import Tape
from orca.exec.context import RunContext
from orca.gates.interrupt import InterruptHandler
from orca.gates.types import InterruptRequest
from orca.run.orchestrator import Orchestrator


def run_async(coro):
    return asyncio.run(coro)


# ── helpers ──────────────────────────────────────────────────────────────────


def _diamond_wf_yaml(tmp_path) -> str:
    """菱形 wf：entry=a → b → c → $end（线性 3 node），用于 skip-to-target 测试。

    a (agent) → b (agent) → c (agent) → $end。skip a 直跳 c 时，b 也被绕过。
    """
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump({
        "name": "t",
        "entry": "a",
        "nodes": [
            {"name": "a", "kind": "agent", "prompt": "do A",
             "routes": [{"to": "b"}]},
            {"name": "b", "kind": "agent", "prompt": "do B",
             "routes": [{"to": "c"}]},
            {"name": "c", "kind": "agent", "prompt": "do C",
             "routes": [{"to": "$end"}]},
        ],
    }), encoding="utf-8")
    return str(p)


def _fallback_route_wf_yaml(tmp_path) -> str:
    """带兜底 route 的 wf：a 的 routes 含 when=None fallback（wave-1 skip 行为依赖）。

    a (agent): when output.ok → b ; when None → c （兜底）
    skip a（output=None）→ 走兜底 route → c。
    """
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump({
        "name": "t",
        "entry": "a",
        "nodes": [
            {"name": "a", "kind": "agent", "prompt": "do A",
             "routes": [
                 {"when": "output.ok", "to": "b"},
                 {"to": "c"},  # 兜底（when=None）
             ]},
            {"name": "b", "kind": "agent", "prompt": "do B",
             "routes": [{"to": "$end"}]},
            {"name": "c", "kind": "agent", "prompt": "do C",
             "routes": [{"to": "$end"}]},
        ],
    }), encoding="utf-8")
    return str(p)


def _skipped_field_wf_yaml(tmp_path) -> str:
    """b 引用 a.output.field（a 被 skip → output=None）→ 测 §9.2 容错。

    a (agent) → b (agent, prompt 引用 {{ a.output.field }}) → $end
    a 有兜底 route（when=None → b），b 的 prompt 渲染容忍 a.output=None。
    注意：render_template 用 StrictUndefined，a.output.field 在 None 上会 raise。
    本测试断言的是 **route 求值容错**（§9.2 字面），render 容错由 prompt 避免引用 None 字段
    或由 SPEC §9.2 注释的「兜底 route」承担（render 层 None.field 仍 raise，是设计行为）。
    故本 wf 的 b prompt 用 {{ a.output | default('<skipped>') }} 显式兜底，验证端到端不崩。
    """
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump({
        "name": "t",
        "entry": "a",
        "nodes": [
            {"name": "a", "kind": "agent", "prompt": "do A",
             "routes": [{"to": "b"}]},  # 兜底（when=None）
            {"name": "b", "kind": "agent",
             "prompt": "got a={{ a.output | default('<skipped>') }}",
             "routes": [{"to": "$end"}]},
        ],
    }), encoding="utf-8")
    return str(p)


class _RecordingAgentExecutor:
    """记录每次 spawn 的 fake agent executor（同 test_interrupt_e2e.py 的实现）。"""

    def __init__(self) -> None:
        self.spawned: list[str] = []
        self.prompts: list[str] = []

    async def exec(self, node, ctx: RunContext):
        from orca.exec.render import render_prompt
        from orca.schema import Event

        self.spawned.append(node.name)
        session_id = uuid.uuid4().hex
        prompt = render_prompt(node, ctx)
        self.prompts.append(prompt)

        def _ev(t: str, data: dict) -> Event:
            return Event(seq=0, type=t, timestamp=time.time(),  # type: ignore[arg-type]
                         node=node.name, session_id=session_id, data=data)

        yield _ev("node_started", {"executor": "fake", "kind": "agent"})
        yield _ev("prompt_rendered", {
            "node": node.name, "session_id": session_id, "preview": prompt[-200:],
        })
        yield _ev("node_completed", {"output": {"result": f"fake-{node.name}"}, "elapsed": 0.01})


def _patch_factory_to_fake(fake_exec: _RecordingAgentExecutor):
    import orca.exec.factory as factory_mod

    orig = factory_mod.make_executor
    factory_mod.make_executor = lambda node, agent_tools_server=None, bus=None: fake_exec
    return orig


def _restore_factory(orig):
    import orca.exec.factory as factory_mod
    factory_mod.make_executor = orig


def _make_orch(wf, bus, tmp_path, interrupt_handler, run_id="r1"):
    return Orchestrator(
        wf, bus, inputs={}, run_id=run_id, interrupt_handler=interrupt_handler,
    )


# ── task1: SKIP 无显式目标 + 兜底 route → 沿 route 跳（wave-1 不变量）──────────


def test_skip_to_route_next_when_fallback_exists(tmp_path):
    """SKIP 无 explicit target + 当前 node 有 when=None 兜底 route → 沿 route 跳到 c。

    SPEC §10.2 item12 前半：「若当前 node 有 when=None 兜底 route 则沿 route 跳」。
    回归保护：P4 引入 skip_target 后，无 skip_target 的路径仍走 wave-1 route 求值。
    """
    wf = load_workflow(_fallback_route_wf_yaml(tmp_path))
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    interrupt_handler = InterruptHandler(bus)
    fake_exec = _RecordingAgentExecutor()
    orig = _patch_factory_to_fake(fake_exec)

    async def scenario():
        await interrupt_handler.start()
        try:
            orch = _make_orch(wf, bus, tmp_path, interrupt_handler)
            ireq = InterruptRequest(
                id="i1", node="a", run_id="r1", session_id="sess-a",
                elapsed_at_request=0.5,
            )
            # 无 skip_target → 走 route 求值。
            orch.request_interrupt(ireq, answer=("skip", None))

            drive_task = asyncio.create_task(orch._drive_loop())
            await asyncio.wait_for(drive_task, timeout=5.0)
        finally:
            await interrupt_handler.stop()
            _restore_factory(orig)
            bus.close()

    run_async(scenario())

    types = [e.type for e in tape.replay()]
    assert "node_skipped" in types
    # a 被 skip（output=None）→ 走兜底 route（when=None）→ c。
    assert "a" not in fake_exec.spawned
    assert "c" in fake_exec.spawned
    # b 不应被跑（兜底 route 直跳 c）。
    assert "b" not in fake_exec.spawned


# ── task2: SKIP + 显式目标 → 直接跳（不经 route 求值）────────────────────────


def test_skip_to_explicit_target_jumps_there(tmp_path):
    """SKIP + skip_target="c" → _drive_loop 直接跳到 c，b 被绕过，a 标 skipped。

    SPEC §9 task1 + §10.2 item12 后半（无兜底 route 时用显式目标，避免 NoRouteMatch）。
    本 wf 是线性 a→b→c，a 无兜底 route（routes: [{to: b}]）。若走 route 求值，跳过 a 时
    output=None → resolve 求唯一 route（when=None 隐式？不，本 wf 的 route 无 when = 兜底）。
    实际上 ``{"to": "b"}`` 等价 when=None 兜底，故无 skip_target 也会跳到 b。本测试断言
    **显式 skip_target="c" 直跳 c**，绕过 b（route 求值不会产生此结果）。
    """
    wf = load_workflow(_diamond_wf_yaml(tmp_path))
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    interrupt_handler = InterruptHandler(bus)
    fake_exec = _RecordingAgentExecutor()
    orig = _patch_factory_to_fake(fake_exec)

    async def scenario():
        await interrupt_handler.start()
        try:
            orch = _make_orch(wf, bus, tmp_path, interrupt_handler)
            ireq = InterruptRequest(
                id="i2", node="a", run_id="r1", session_id="sess-a",
                elapsed_at_request=0.5,
            )
            orch.request_interrupt(
                ireq, answer=("skip", None), skip_target="c",
            )

            drive_task = asyncio.create_task(orch._drive_loop())
            await asyncio.wait_for(drive_task, timeout=5.0)
        finally:
            await interrupt_handler.stop()
            _restore_factory(orig)
            bus.close()

    run_async(scenario())

    events = list(tape.replay())
    types = [e.type for e in events]

    # a 标 skipped（node_skipped 写 Tape）。
    assert "node_skipped" in types
    skipped = next(e for e in events if e.type == "node_skipped")
    assert skipped.node == "a"

    # a 被跳过；c 续跑；**b 被绕过**（显式 target 直跳 c，不经 b）。
    assert "a" not in fake_exec.spawned
    assert "b" not in fake_exec.spawned, (
        f"显式 skip_target=c 应绕过 b, spawned={fake_exec.spawned}"
    )
    assert "c" in fake_exec.spawned

    # route_taken 记录 a → c（显式跳转，非 route 求值，但仍 emit 让 reducer 跟踪 current_node）。
    # 之后 c 跑完也 emit c → $end，故找 from=a 的那条。
    routes_taken = [e for e in events if e.type == "route_taken"]
    skip_route = next(r for r in routes_taken if r.data["from"] == "a")
    assert skip_route.data["to"] == "c"


# ── task3: SKIP + 不存在目标 → fail loud（ValueError，非 NoRouteMatch 崩溃）────


def test_skip_to_nonexistent_target_fails_loud(tmp_path):
    """skip_target="no_such_node" → ValueError（clear error），非静默 NoRouteMatch 崩溃。

    SPEC §9 task2 + §10.2 item12：目标不存在必须 fail loud，clear error message。
    断言 raise ValueError（含目标名），且 drive_loop 抛此异常（非 RouteError）。

    **tape 一致性断言**（review 🔴 修复回归保护）：校验在 record_resolved **之前**，
    故 tape **不含** interrupt_resolved 事件（避免脏 tape：resolved 承诺 skip 到不存在的
    node 但 workflow 却 failed，自相矛盾）。
    """
    wf = load_workflow(_diamond_wf_yaml(tmp_path))
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    interrupt_handler = InterruptHandler(bus)
    fake_exec = _RecordingAgentExecutor()
    orig = _patch_factory_to_fake(fake_exec)

    async def scenario():
        await interrupt_handler.start()
        try:
            orch = _make_orch(wf, bus, tmp_path, interrupt_handler)
            ireq = InterruptRequest(
                id="i3", node="a", run_id="r1", session_id="sess-a",
                elapsed_at_request=0.5,
            )
            orch.request_interrupt(
                ireq, answer=("skip", None), skip_target="no_such_node",
            )

            # drive_loop 内 _handle_interrupt 校验目标（先于 record_resolved）→ raise ValueError。
            with pytest.raises(ValueError, match="no_such_node"):
                await asyncio.wait_for(orch._drive_loop(), timeout=5.0)
        finally:
            await interrupt_handler.stop()
            _restore_factory(orig)
            bus.close()

    run_async(scenario())

    # tape 一致性：校验先于 emit，故无 interrupt_resolved（无脏 tape 孤儿事件）。
    events = list(tape.replay())
    resolved = [e for e in events if e.type == "interrupt_resolved"]
    assert resolved == [], (
        f"校验失败时不应写 interrupt_resolved（脏 tape），got {len(resolved)} 条"
    )


def test_skip_to_self_fails_loud(tmp_path):
    """skip_target == 当前 node → ValueError（不能 skip 到自己，会死循环）。"""
    wf = load_workflow(_diamond_wf_yaml(tmp_path))
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    interrupt_handler = InterruptHandler(bus)
    fake_exec = _RecordingAgentExecutor()
    orig = _patch_factory_to_fake(fake_exec)

    async def scenario():
        await interrupt_handler.start()
        try:
            orch = _make_orch(wf, bus, tmp_path, interrupt_handler)
            ireq = InterruptRequest(
                id="i4", node="a", run_id="r1", session_id="sess-a",
                elapsed_at_request=0.5,
            )
            orch.request_interrupt(
                ireq, answer=("skip", None), skip_target="a",
            )
            with pytest.raises(ValueError, match="自己"):
                await asyncio.wait_for(orch._drive_loop(), timeout=5.0)
        finally:
            await interrupt_handler.stop()
            _restore_factory(orig)
            bus.close()

    run_async(scenario())


# ── task4 (§9.2): skipped node 的 None output 在下游 route 求值容错 ─────────────


def test_skipped_node_output_none_tolerated_in_route_evaluation(tmp_path):
    """§9.2：skipped node 的 output=None，下游 route when 引用 ``output.field`` 求值失败 →
    视为该 route 不匹配，继续找兜底 route（非 RouteError 崩溃）。

    构造：a 被 skip（output=None）→ a 的 routes 有两条：when=output.x → b；兜底 → c。
    skip 时 output=None → output.x 求值 UndefinedError → 容错为不匹配 → 走兜底 → c。
    若不容错，resolve 会 raise RouteError → workflow_failed{NoRouteMatch}（SPEC §10.2 item12
    正是要避免的崩溃）。
    """
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump({
        "name": "t",
        "entry": "a",
        "nodes": [
            {"name": "a", "kind": "agent", "prompt": "do A",
             "routes": [
                 {"when": "output.x", "to": "b"},  # output=None 时求值失败
                 {"to": "c"},  # 兜底
             ]},
            {"name": "b", "kind": "agent", "prompt": "do B", "routes": [{"to": "$end"}]},
            {"name": "c", "kind": "agent", "prompt": "do C", "routes": [{"to": "$end"}]},
        ],
    }), encoding="utf-8")
    wf = load_workflow(str(p))
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    interrupt_handler = InterruptHandler(bus)
    fake_exec = _RecordingAgentExecutor()
    orig = _patch_factory_to_fake(fake_exec)

    async def scenario():
        await interrupt_handler.start()
        try:
            orch = _make_orch(wf, bus, tmp_path, interrupt_handler)
            ireq = InterruptRequest(
                id="i5", node="a", run_id="r1", session_id="sess-a",
                elapsed_at_request=0.5,
            )
            # 无 skip_target → 走 route 求值；a 的 output.x route 求值失败 → 走兜底 → c。
            orch.request_interrupt(ireq, answer=("skip", None))
            await asyncio.wait_for(orch._drive_loop(), timeout=5.0)
        finally:
            await interrupt_handler.stop()
            _restore_factory(orig)
            bus.close()

    run_async(scenario())

    # 未崩溃（无 RouteError）→ 走兜底到 c。
    assert "c" in fake_exec.spawned
    assert "b" not in fake_exec.spawned


# ── tape 可观测：skip_target 写进 interrupt_resolved.data ────────────────────


def test_skip_target_recorded_on_tape(tmp_path):
    """SPEC §9 task4：interrupt_resolved{action:"skip", skip_target:"c"} 写 Tape 可观测。

    断言 tape 含 interrupt_resolved 且 data.skip_target == "c"。
    """
    wf = load_workflow(_diamond_wf_yaml(tmp_path))
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    interrupt_handler = InterruptHandler(bus)
    fake_exec = _RecordingAgentExecutor()
    orig = _patch_factory_to_fake(fake_exec)

    async def scenario():
        await interrupt_handler.start()
        try:
            orch = _make_orch(wf, bus, tmp_path, interrupt_handler)
            ireq = InterruptRequest(
                id="i6", node="a", run_id="r1", session_id="sess-a",
                elapsed_at_request=0.5,
            )
            orch.request_interrupt(
                ireq, answer=("skip", None), skip_target="c",
            )
            await asyncio.wait_for(orch._drive_loop(), timeout=5.0)
        finally:
            await interrupt_handler.stop()
            _restore_factory(orig)
            bus.close()

    run_async(scenario())

    events = list(tape.replay())
    resolved = [e for e in events if e.type == "interrupt_resolved"]
    assert len(resolved) == 1
    assert resolved[0].data["action"] == "skip"
    assert resolved[0].data["skip_target"] == "c"


def test_skip_no_target_omits_skip_target_field(tmp_path):
    """无显式 skip_target → interrupt_resolved.data 不含 skip_target 键（向后兼容）。

    回归保护：wave-1 的 skip（route 求值）路径不应突然多出一个 None 字段。
    """
    wf = load_workflow(_fallback_route_wf_yaml(tmp_path))
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    interrupt_handler = InterruptHandler(bus)
    fake_exec = _RecordingAgentExecutor()
    orig = _patch_factory_to_fake(fake_exec)

    async def scenario():
        await interrupt_handler.start()
        try:
            orch = _make_orch(wf, bus, tmp_path, interrupt_handler)
            ireq = InterruptRequest(
                id="i7", node="a", run_id="r1", session_id="sess-a",
                elapsed_at_request=0.5,
            )
            orch.request_interrupt(ireq, answer=("skip", None))  # 无 skip_target
            await asyncio.wait_for(orch._drive_loop(), timeout=5.0)
        finally:
            await interrupt_handler.stop()
            _restore_factory(orig)
            bus.close()

    run_async(scenario())

    events = list(tape.replay())
    resolved = [e for e in events if e.type == "interrupt_resolved"]
    assert len(resolved) == 1
    assert resolved[0].data["action"] == "skip"
    assert "skip_target" not in resolved[0].data


# ── router 单元层 §9.2 容错（独立于 orchestrator 验证 resolve 行为）────────────


def test_router_resolve_tolerates_skipped_none_output():
    """router.resolve: output=None 时 when 引用 output.field 失败 → 视为不匹配，走兜底。

    独立于 orchestrator 验证 §9.2 容错（resolve 是纯函数，直接单测）。
    """
    from orca.exec.context import RunContext
    from orca.run.router import resolve
    from orca.schema import Route

    routes = [
        Route(when="output.field", to="b"),  # output=None → UndefinedError
        Route(when=None, to="c"),  # 兜底
    ]
    ctx = RunContext(inputs={}, outputs={}, run_id="r", task=None)
    # output=None（skipped）→ 第一条 when 求值失败 → 容错跳过 → 兜底 → c。
    assert resolve(routes, None, ctx) == "c"


def test_router_resolve_non_skipped_failure_still_fails_loud():
    """router.resolve: 非 skip 路径（output 非 None）的 when 求值失败仍 raise RouteError。

    回归保护：§9.2 容错**仅**对 output is None 生效；正常 output 的 when 错仍 fail loud。
    """
    from orca.exec.context import RunContext
    from orca.run.router import RouteError, resolve
    from orca.schema import Route

    routes = [Route(when="output.field.nested_deep", to="b")]  # 无兜底
    ctx = RunContext(inputs={}, outputs={}, run_id="r", task=None)
    # output 是 int 5 → 5.field.nested_deep 求值失败 → 非 skip 路径 → RouteError。
    with pytest.raises(RouteError):
        resolve(routes, 5, ctx)


# ── 多壳路径 skip_target 契约（review 🟡 补强）──────────────────────────────────


def test_multishell_path_returns_skip_target_none(tmp_path):
    """多壳路径（answer=None → await handler.request）的 skip_target 恒 None。

    SPEC §9 + §11.1：多壳路径（web/mcp）暂不支持显式 skip_target（需各壳实现
    NodeSelectModal 等价物，P3 web/mcp 接入时再补）。当前 ``_handle_interrupt`` 的
    else 分支返回 skip_target=None（走 route 求值，wave-1 行为）。

    回归保护：锁定此退化契约——未来 web/mcp 接入时若忘了补 NodeSelectModal 等价物，
    此 None 会静默退化而无告警；本测试让退化可见（断言 None + 走 route 求值到下一 node）。
    """
    wf = load_workflow(_diamond_wf_yaml(tmp_path))
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    interrupt_handler = InterruptHandler(bus)
    fake_exec = _RecordingAgentExecutor()
    orig = _patch_factory_to_fake(fake_exec)

    async def scenario():
        await interrupt_handler.start()
        try:
            orch = _make_orch(wf, bus, tmp_path, interrupt_handler)
            ireq = InterruptRequest(
                id="ms-skip", node="a", run_id="r1", session_id="sess-a",
                elapsed_at_request=0.5,
            )
            # 多壳路径：不带 answer（也不带 skip_target）。
            orch.request_interrupt(ireq)

            drive_task = asyncio.create_task(orch._drive_loop())
            # 等 node=a 边界 await handler.request 注册 future。
            for _ in range(50):
                if interrupt_handler.has_pending("ms-skip"):
                    break
                await asyncio.sleep(0.02)
            assert interrupt_handler.has_pending("ms-skip")
            # 某壳答 skip（无显式 target）。
            interrupt_handler.resolve("ms-skip", "skip", None, "web")
            await asyncio.wait_for(drive_task, timeout=5.0)
        finally:
            await interrupt_handler.stop()
            _restore_factory(orig)
            bus.close()

    run_async(scenario())

    # 多壳路径 skip → skip_target 恒 None → 走 route 求值（a 的兜底 route → b）。
    events = list(tape.replay())
    resolved = next(e for e in events if e.type == "interrupt_resolved")
    assert resolved.data["action"] == "skip"
    assert "skip_target" not in resolved.data  # 多壳路径不写 skip_target
    # a 被 skip，走 route 求值到 b（diamond wf a 的 route 直接到 b）。
    assert "a" not in fake_exec.spawned
    assert "b" in fake_exec.spawned


# ── parallel 组作为 skip 目标（review 🟡 补强）──────────────────────────────


def test_skip_to_parallel_group_target(tmp_path):
    """skip_target 是 parallel 组名 → 校验通过（_validate_skip_target 允许 parallel 组）+
    _drive_loop 下一轮进 _dispatch 的 parallel 分支。

    SPEC §9 task2「allow skipping to any defined node」——parallel 组也是 defined target。
    本测试锁定 parallel 组作为目标的端到端可用性（不崩，进 parallel 分派）。
    """
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump({
        "name": "t",
        "entry": "starter",
        "nodes": [
            {"name": "starter", "kind": "agent", "prompt": "start",
             "routes": [{"to": "grp"}]},
            {"name": "x", "kind": "agent", "prompt": "X", "routes": [{"to": "$end"}]},
            {"name": "y", "kind": "agent", "prompt": "Y", "routes": [{"to": "$end"}]},
        ],
        "parallel": [
            {"name": "grp", "branches": ["x", "y"], "routes": [{"to": "$end"}]},
        ],
    }), encoding="utf-8")
    wf = load_workflow(str(p))
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    interrupt_handler = InterruptHandler(bus)
    fake_exec = _RecordingAgentExecutor()
    orig = _patch_factory_to_fake(fake_exec)

    async def scenario():
        await interrupt_handler.start()
        try:
            orch = _make_orch(wf, bus, tmp_path, interrupt_handler)
            ireq = InterruptRequest(
                id="i-par", node="starter", run_id="r1", session_id="sess-a",
                elapsed_at_request=0.5,
            )
            # skip starter → 直跳 parallel 组 grp。
            orch.request_interrupt(
                ireq, answer=("skip", None), skip_target="grp",
            )
            await asyncio.wait_for(orch._drive_loop(), timeout=5.0)
        finally:
            await interrupt_handler.stop()
            _restore_factory(orig)
            bus.close()

    run_async(scenario())

    # starter 被 skip；grp parallel 组的 x / y 都跑（parallel 分派）。
    assert "starter" not in fake_exec.spawned
    assert "x" in fake_exec.spawned
    assert "y" in fake_exec.spawned
    # route_taken 记录 starter → grp。
    events = list(tape.replay())
    routes_taken = [e for e in events if e.type == "route_taken"]
    skip_route = next(r for r in routes_taken if r.data["from"] == "starter")
    assert skip_route.data["to"] == "grp"
