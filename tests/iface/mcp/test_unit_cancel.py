"""test_unit_cancel.py —— RunManager.cancel_run 单元测试（SPEC phase-10 §5.3 / §D1.4）。

覆盖意图（非仅行为）：
  - **cancel running run**：status 转 cancelled + tape 含 workflow_cancelled 事件 +
    task 被 cancel。
  - **cancel 已终态 run**（completed）→ 返回 False（业务可恢复）。
  - **cancel 后 run_summary**：status="cancelled"（runtime + tape 派生一致）。
  - **cancel 后 replay_state**：tape 派生 status="cancelled"（**唯一真相**，重启后仍见）。
  - **未知 run_id → KeyError**（fail loud，SPEC §6.0 铁律 4）。

做法：用 ``hold`` pattern 让 Orchestrator.run 阻塞（teardown 不先跑，bus/tape 保持
open）。cancel_run 内部 cancel task → emit workflow_cancelled 写 tape → status 转
cancelled → teardown。teardown 后 tape 已 close，但 replay 仍可读（tape.close 只关写
句柄，文件可读）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from orca.events.replay import replay_state

from tests.iface.web.conftest import demo_linear_yaml, make_manager, run_async


def _patch_hold_orchestrator(hold: asyncio.Event):
    """返回永不返回的 Orchestrator.run patch（hold.wait 阻塞，让 run 停在 running）。"""

    async def hang(self):
        await hold.wait()

    return patch("orca.run.orchestrator.Orchestrator.run", hang)


def test_cancel_running_run_transitions_status_and_writes_tape(tmp_path):
    """cancel running run → status 转 cancelled + tape 含 workflow_cancelled 事件。"""
    manager = make_manager(tmp_path)

    async def go():
        yaml_path = demo_linear_yaml(tmp_path)
        hold = asyncio.Event()
        with _patch_hold_orchestrator(hold):
            run_id = await manager.start_run(str(yaml_path), {}, None, None)
            await asyncio.sleep(0.05)  # 进 running

            # cancel_run 会 cancel task + emit workflow_cancelled + 设 status + teardown
            ok = await manager.cancel_run(run_id, reason="test_cancel")
            assert ok is True

            # tape 应含 workflow_cancelled 事件（唯一真相）。
            # teardown 已关 bus，但 tape.replay 仍可读（文件可读）。
            event_types = [e.type for e in manager.get_handle(run_id).tape.replay()]
            assert "workflow_cancelled" in event_types, (
                f"tape 应含 workflow_cancelled，实得 {event_types}"
            )

            # runtime status 转 cancelled
            handle = manager.get_handle(run_id)
            assert handle is not None
            assert handle.status == "cancelled"
            hold.set()  # 已 cancel 的 task 不再 await hold，但 set 让 _run_with_sem 的
            # finally 不在 cancel 后还等；task 已 cancel 不会跑到 hold.wait。
        await manager.shutdown()

    run_async(go())


def test_cancel_already_completed_returns_false(tmp_path):
    """cancel 已 completed run → 返回 False（业务可恢复，不抛）。"""
    manager = make_manager(tmp_path)

    async def go():
        yaml_path = demo_linear_yaml(tmp_path)
        hold = asyncio.Event()
        with _patch_hold_orchestrator(hold):
            run_id = await manager.start_run(str(yaml_path), {}, None, None)
            await asyncio.sleep(0.05)
            handle = manager.get_handle(run_id)
            # 手动设 status=completed（模拟编排刚完成；_run_with_sem 还卡在 hold）
            handle.status = "completed"

            # cancel 已 completed 的 run → False
            ok = await manager.cancel_run(run_id, reason="late_cancel")
            assert ok is False

            # status 不变（仍是 completed）
            assert handle.status == "completed"
            hold.set()
        await manager.shutdown()

    run_async(go())


def test_cancel_then_run_summary_shows_cancelled(tmp_path):
    """cancel 后 run_summary → status="cancelled"（runtime + tape 派生一致）。"""
    manager = make_manager(tmp_path)

    async def go():
        yaml_path = demo_linear_yaml(tmp_path)
        hold = asyncio.Event()
        with _patch_hold_orchestrator(hold):
            run_id = await manager.start_run(str(yaml_path), {}, None, None)
            await asyncio.sleep(0.05)

            handle = manager.get_handle(run_id)
            # emit workflow_started 让 run_summary 有 current_node 等
            await handle.bus.emit("workflow_started", data={"workflow_name": "demo"})

            ok = await manager.cancel_run(run_id, reason="user_cancelled")
            assert ok is True

            # run_summary 应反映 cancelled（teardown 后 tape 仍可读）
            summary = manager.run_summary(run_id)
            assert summary is not None
            assert summary["status"] == "cancelled"
            hold.set()
        await manager.shutdown()

    run_async(go())


def test_cancel_then_replay_state_shows_cancelled(tmp_path):
    """cancel 后 replay_state(tape).status == "cancelled"（tape 是唯一真相，
    进程重启后仍见 cancelled，不漂移）。SPEC §5.4 决策 9。"""
    manager = make_manager(tmp_path)

    async def go():
        yaml_path = demo_linear_yaml(tmp_path)
        hold = asyncio.Event()
        with _patch_hold_orchestrator(hold):
            run_id = await manager.start_run(str(yaml_path), {}, None, None)
            await asyncio.sleep(0.05)
            handle = manager.get_handle(run_id)
            await handle.bus.emit("workflow_started", data={"workflow_name": "demo"})

            ok = await manager.cancel_run(run_id)
            assert ok is True

            # replay_state 应派生出 cancelled（tape 含 workflow_cancelled → reducer 转 cancelled）
            # teardown 后 tape 已 close（写句柄关），但读（replay）仍可。
            state = replay_state(handle.tape)
            assert state.status == "cancelled", (
                f"replay_state 应派生 cancelled（tape 唯一真相），实得 {state.status}"
            )
            hold.set()
        await manager.shutdown()

    run_async(go())


def test_cancel_unknown_run_raises_key_error(tmp_path):
    """未知 run_id → KeyError（fail loud，SPEC §6.0 铁律 4）。"""
    manager = make_manager(tmp_path)

    async def go():
        with pytest.raises(KeyError):
            await manager.cancel_run("nonexistent-id")

    run_async(go())
