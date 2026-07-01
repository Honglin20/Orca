"""tests/exec/test_wait.py —— WaitExecutor + parse_duration（SPEC §9.7）。

覆盖 INTENT（非仅行为）：
  - parse_duration：四单位 + 纯秒 + 非法 fail loud
  - WaitExecutor 完整生命周期（node_started → wait_started → wait_completed → node_completed）
  - interruptible=True 可被 wait-handle set 立即打断（interrupted=True）
  - interruptible=False 不被打断（必须等满）
  - 非法 duration / 超上限 → node_failed（fail loud，error_type / phase 正确）

约定（同 tests/exec/）：本仓库不用 pytest-asyncio，异步统一 ``asyncio.run``。
小 duration（0.05s）保确定性 + 快速。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.exec.context import RunContext
from orca.exec.wait import WaitExecutor, parse_duration
from orca.schema import Event, WaitNode


def _run(coro):
    return asyncio.run(coro)


def _ctx() -> RunContext:
    return RunContext(inputs={}, outputs={}, run_id="r1")


def _bus(tmp_path: Path) -> EventBus:
    return EventBus(Tape(tmp_path / "events.jsonl", run_id="r1"))


async def _collect(executor: WaitExecutor, node: WaitNode, ctx: RunContext) -> list[Event]:
    return [ev async for ev in executor.exec(node, ctx)]


# ── parse_duration ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("30s", 30.0),
        ("5m", 300.0),
        ("2h", 7200.0),
        ("1d", 86400.0),
        ("30", 30.0),  # 纯数字 = 秒
        ("30S", 30.0),  # 大写归一
        ("  10s  ", 10.0),  # strip
        ("1.5m", 90.0),  # 小数
    ],
)
def test_parse_duration_units(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("bad", ["abc", "", "   ", "30x", "s", "-5s", "ms"])
def test_parse_duration_invalid_raises(bad: str):
    """非法 duration（空 / 未知单位 / 非数字 / 负）→ ValueError（fail loud）。"""
    with pytest.raises(ValueError):
        parse_duration(bad)


# ── WaitExecutor：完整睡眠（interruptible=False 主路径最简）─────────────────


def test_wait_executor_sleeps_for_duration(tmp_path):
    """duration=0.05s + interruptible=False → 等 ~0.05s，interrupted=False，node_completed。"""
    bus = _bus(tmp_path)
    try:
        node = WaitNode(name="w", duration="0.05s", interruptible=False)
        executor = WaitExecutor(bus)
        start = time.monotonic()
        events = _run(_collect(executor, node, _ctx()))
        elapsed = time.monotonic() - start
    finally:
        bus.close()

    types = [e.type for e in events]
    assert types == [
        "node_started",
        "wait_started",
        "wait_completed",
        "node_completed",
    ]
    completed = events[-1]
    assert completed.data["output"] == {"interrupted": False}
    wait_done = events[-2]
    assert wait_done.data["interrupted"] is False
    # 确实等了（≈0.05s，允许宽松下界避免抖动 false-negative）
    assert elapsed >= 0.04


def test_wait_executor_jinja2_renders_duration(tmp_path):
    """duration 是 Jinja2 模板，引用 inputs（SPEC §9.7.2 支持 Jinja2 渲染）。"""
    bus = _bus(tmp_path)
    try:
        ctx = RunContext(inputs={"secs": "0.05"}, outputs={}, run_id="r1")
        node = WaitNode(name="w", duration="{{ inputs.secs }}s", interruptible=False)
        executor = WaitExecutor(bus)
        events = _run(_collect(executor, node, ctx))
    finally:
        bus.close()
    started = next(e for e in events if e.type == "wait_started")
    assert started.data["duration_seconds"] == pytest.approx(0.05)


# ── interruptible=True 可被打断 ──────────────────────────────────────────────


def test_wait_executor_interruptible_can_be_cancelled(tmp_path):
    """register wait-handle → set 它 → wait 立即结束（interrupted=True）。

    这是 Ctrl+G 打断 wait 的自动化证明（EventBus.notify_all_waits 路径）。
    意图：长 duration（2s）+ 中途 notify 应在 << 2s 内返回。
    """
    bus = _bus(tmp_path)
    try:
        node = WaitNode(name="w", duration="2s", interruptible=True)
        executor = WaitExecutor(bus)

        async def scenario() -> tuple[list[Event], float]:
            task = asyncio.create_task(_collect(executor, node, _ctx()))
            # 让 wait_started emit + register handle（asyncio 调度让出）
            await asyncio.sleep(0.05)
            # 模拟 Ctrl+G：notify 所有 wait handle
            bus.notify_all_waits()
            start = time.monotonic()
            events = await task
            return events, time.monotonic() - start

        events, _ = _run(scenario())
    finally:
        bus.close()

    types = [e.type for e in events]
    assert "wait_completed" in types
    assert "node_completed" in types
    wait_done = next(e for e in events if e.type == "wait_completed")
    assert wait_done.data["interrupted"] is True
    completed = next(e for e in events if e.type == "node_completed")
    assert completed.data["output"] == {"interrupted": True}


def test_wait_executor_unregisters_handle_after_completion(tmp_path):
    """wait 结束后 handle 从 bus 注销（正常完成 / 打断都注销，防泄漏 + 防二次 notify）。"""
    bus = _bus(tmp_path)
    try:
        node = WaitNode(name="w", duration="0.05s", interruptible=True)
        executor = WaitExecutor(bus)
        _run(_collect(executor, node, _ctx()))
        # 完成 → 注销干净，notify 返 0
        assert bus.notify_all_waits() == 0
    finally:
        bus.close()


def test_wait_executor_not_interruptible_waits_full(tmp_path):
    """interruptible=False → 即使有 handle 被 notify，wait 仍等满（不被打断）。

    SPEC §9.7.5：interruptible=False 必须等满，Ctrl+G 等下一 node 边界生效。
    """
    bus = _bus(tmp_path)
    try:
        node = WaitNode(name="w", duration="0.1s", interruptible=False)
        executor = WaitExecutor(bus)

        async def scenario() -> list[Event]:
            task = asyncio.create_task(_collect(executor, node, _ctx()))
            await asyncio.sleep(0.02)
            # interruptible=False 不会注册 handle，notify 返 0（无 handle 可唤醒）
            assert bus.notify_all_waits() == 0
            return await task

        events = _run(scenario())
    finally:
        bus.close()
    wait_done = next(e for e in events if e.type == "wait_completed")
    assert wait_done.data["interrupted"] is False


# ── fail loud：非法 duration / 超上限 ──────────────────────────────────────


def test_wait_executor_invalid_duration_fails(tmp_path):
    """duration='abc' → node_failed{error_type:RenderError, phase:render}。"""
    bus = _bus(tmp_path)
    try:
        node = WaitNode(name="w", duration="abc")
        executor = WaitExecutor(bus)
        events = _run(_collect(executor, node, _ctx()))
    finally:
        bus.close()
    failed = next(e for e in events if e.type == "node_failed")
    assert failed.data["error_type"] == "RenderError"
    assert failed.data["phase"] == "render"
    # 不应 emit wait_started / wait_completed（解析阶段就失败）
    types = [e.type for e in events]
    assert "wait_started" not in types
    assert "wait_completed" not in types


def test_wait_executor_exceeds_max_duration_fails(tmp_path):
    """duration='25h' 超过 24h 硬上限 → node_failed{error_type:ConfigError, phase:config}。"""
    bus = _bus(tmp_path)
    try:
        node = WaitNode(name="w", duration="25h")
        executor = WaitExecutor(bus)
        events = _run(_collect(executor, node, _ctx()))
    finally:
        bus.close()
    failed = next(e for e in events if e.type == "node_failed")
    assert failed.data["error_type"] == "ConfigError"
    assert failed.data["phase"] == "config"


# ── 生命周期 + session_id 一致 ──────────────────────────────────────────────


def test_wait_lifecycle_session_id_consistent(tmp_path):
    """所有事件同 session_id（铁律 5）；node_started.kind='wait'。"""
    bus = _bus(tmp_path)
    try:
        node = WaitNode(name="w", duration="0.05s", interruptible=False)
        executor = WaitExecutor(bus)
        events = _run(_collect(executor, node, _ctx()))
    finally:
        bus.close()
    sids = {e.session_id for e in events}
    assert len(sids) == 1
    started = events[0]
    assert started.type == "node_started"
    assert started.data["kind"] == "wait"


# ── 集成：wait → script 经 orchestrator 主循环 ────────────────────────────────


def test_orchestrator_runs_wait_then_downstream(tmp_path):
    """2-node workflow ``wait(0.05s) → script``：tape 含 wait_started+wait_completed，
    下游 script 能读到 ``{{ wait_node.output.interrupted }}``。

    INTENT：wait 经 orchestrator 主循环跑通（make_executor 分派 + bus 注入），
    下游 node 能消费 wait 的 output（与 examples/with_wait.yaml 同形态）。
    """
    from orca.run.orchestrator import Orchestrator
    from orca.schema import Route, ScriptNode, Workflow

    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    wf = Workflow(
        name="wait_e2e",
        entry="w",
        nodes=[
            WaitNode(name="w", duration="0.05s", interruptible=True, routes=[Route(to="s")]),
            ScriptNode(
                name="s",
                command='echo "interrupted={{ w.output.interrupted }}"',
                routes=[Route(to="$end")],
            ),
        ],
        outputs={"result": "{{ s.output.stdout }}"},
    )
    orch = Orchestrator(wf, bus)
    state = _run(orch.run())

    assert state.status == "completed"
    types = [e.type for e in tape.replay()]
    assert types.count("wait_started") == 1
    assert types.count("wait_completed") == 1
    assert "node_completed" in types  # 下游 script 跑了
    # 下游 script 读到 wait.output.interrupted=False
    completed = next(e for e in tape.replay() if e.type == "workflow_completed")
    assert "interrupted=False" in completed.data["outputs"]["result"]


# ── 补充覆盖：interruptible 正向跑满 + factory fail loud + 常量契约 ──────────


def test_wait_executor_interruptible_runs_full_when_not_notified(tmp_path):
    """interruptible=True 但无 notify → 跑满 duration，interrupted=False（正向分支）。

    覆盖 asyncio.wait FIRST_COMPLETED 中 sleep_task 进 done、int_task 进 pending 的路径
    （既有测试只覆盖 notify 先到的反向分支）。
    """
    bus = _bus(tmp_path)
    try:
        node = WaitNode(name="w", duration="0.05s", interruptible=True)
        executor = WaitExecutor(bus)
        events = _run(_collect(executor, node, _ctx()))
    finally:
        bus.close()
    wait_done = next(e for e in events if e.type == "wait_completed")
    assert wait_done.data["interrupted"] is False
    # 完成 → 注销干净（防泄漏）
    assert bus.notify_all_waits() == 0


def test_make_executor_wait_without_bus_fails_loud(monkeypatch):
    """make_executor(WaitNode, bus=None) → ValueError（打断契约不能静默失效）。

    SPEC §9.7.6：interruptible wait 没 bus 无法注册 handle。factory fail loud。
    """
    from orca.exec.factory import make_executor

    node = WaitNode(name="w", duration="1s")
    with pytest.raises(ValueError, match="bus"):
        make_executor(node)  # bus 默认 None


def test_make_executor_wait_with_bus_returns_waitexecutor(tmp_path):
    """make_executor(WaitNode, bus=<EventBus>) → WaitExecutor（happy path 分派）。"""
    from orca.exec.factory import make_executor
    from orca.exec.wait import WaitExecutor

    bus = _bus(tmp_path)
    try:
        node = WaitNode(name="w", duration="1s")
        executor = make_executor(node, bus=bus)
        assert isinstance(executor, WaitExecutor)
    finally:
        bus.close()


def test_max_duration_seconds_constant():
    """SPEC §9.7.5：MAX_DURATION_SECONDS == 86400（24h）—— 锁契约防误改。"""
    from orca.exec.wait import MAX_DURATION_SECONDS

    assert MAX_DURATION_SECONDS == 24 * 60 * 60


def test_parallel_group_two_waits_independent(tmp_path):
    """SPEC §9.7.5 row 4：parallel group 内两个 interruptible wait 各自 sleep，
    notify_all_waits 同时唤醒两者（互不干扰，每个 wait 独立 handle）。

    INTENT：并行 wait 的独立打断语义（不是「一个被打断其他跟着断」，而是各自独立）。
    """
    from orca.run.parallel import run_parallel_group
    from orca.schema import ParallelGroup, Route, ScriptNode, WaitNode as WN, Workflow

    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)
    wf = Workflow(
        name="par_wait",
        entry="__unused__",
        nodes=[
            WN(name="wa", duration="2s", interruptible=True, routes=[Route(to="$end")]),
            WN(name="wb", duration="2s", interruptible=True, routes=[Route(to="$end")]),
            ScriptNode(name="__unused__", command="echo", routes=[]),
        ],
        parallel=[
            ParallelGroup(
                name="grp", branches=["wa", "wb"], failure_mode="fail_fast",
                routes=[Route(to="$end")],
            )
        ],
    )

    async def scenario():
        from orca.exec.factory import make_executor as real_make_executor

        # 不 patch：wait 走真 WaitExecutor（factory 拿 bus 分派）。parallel.run_one 调
        # make_executor(node, agent_tools_server, bus=bus) —— 已透传 bus（parallel.py:79）。
        task = asyncio.create_task(
            run_parallel_group(
                wf.parallel[0], _ctx(), bus, wf, agent_tools_server=None,
            )
        )
        await asyncio.sleep(0.1)  # 让两个 wait 都进入 sleep + 注册 handle
        woken = bus.notify_all_waits()
        result = await asyncio.wait_for(task, timeout=3.0)
        return woken, result

    woken, result = _run(scenario())
    bus.close()

    # 两个 wait 都被唤醒（独立 handle，notify_all_waits 一次 set 两者）
    assert woken == 2
    assert result["count"] == 2
    assert result["succeeded"] == 2
    # 各自 output.interrupted=True
    assert result["outputs"]["wa"]["interrupted"] is True
    assert result["outputs"]["wb"]["interrupted"] is True
