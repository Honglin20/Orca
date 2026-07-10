"""test_run_manager.py —— RunManager 真并发 + max_concurrent 排队 + 懒加载元数据（SPEC §6.2 / 计划 A1.2）。

覆盖意图（非仅行为）：
  - **真并发**：3 个慢 run 同时 running（asyncio.gather，sem 不串行化）。
  - **max_concurrent 排队**：max=2，start 4 → 同时 running ≤ 2，余 queued。
  - **懒加载红线**：list_runs 返回 RunMeta，断言无 events 字段（SPEC §0.1 铁律 2）。
  - **status 转换**：queued → running → completed（mock run 成功）/ failed（raise）。
  - **元数据 == replay_state**：progress 的 done 数与 replay_state(tape).node_status 一致
    （SPEC §9 决策 6）。
  - **get_run_events 懒加载**：唯一来源 tape.replay（断言与 tape.replay() 相等）。
  - **生命周期干净**：run 终态后 gate_handler.stop + bus.close（无 leaked task）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from orca.events.replay import replay_state
from orca.iface.web.run_manager import RunHandle, RunManager, RunMeta
from orca.run.orchestrator import Orchestrator

from tests.iface.web.conftest import make_manager, run_async


# ── 真并发（SPEC §6.2 / §0.1 铁律 4）─────────────────────────────────────


def test_start_run_returns_run_id_nonblocking(tmp_path, yaml_path):
    """start_run 返回 run_id 且不阻塞——await 后 run 已注册（queued/running）。"""
    manager = make_manager(tmp_path)

    async def go():
        run_id = await manager.start_run(str(yaml_path), {}, None, None)
        assert isinstance(run_id, str) and run_id
        # 已注册（queued 或 running，不阻塞等完成）
        assert manager.get_handle(run_id) is not None
        await manager.shutdown()
        return run_id

    rid = run_async(go())
    assert rid.startswith("demo-")


def test_real_concurrency(tmp_path, yaml_path):
    """真并发：3 个慢 run 同时 running（asyncio.gather，sem 不串行化）。

    用 AsyncMock patch Orchestrator.run 注入 sleep，让 3 个 run 同时进入 running 段，
    断言此时 list_runs 有 3 个 running（而非一个一个跑）。
    """
    manager = make_manager(tmp_path, max_concurrent=3)
    started = asyncio.Event()
    running_count = {"n": 0}

    async def slow_run(self):
        running_count["n"] += 1
        if running_count["n"] == 3:
            started.set()
        await asyncio.sleep(0.15)  # 让 3 个 run 重叠

    async def go():
        with patch.object(Orchestrator, "run", slow_run):
            ids = await asyncio.gather(
                *[manager.start_run(str(yaml_path), {}, None, None) for _ in range(3)]
            )
        await asyncio.sleep(0.02)  # 让 task 进入 sem
        metas = manager.list_runs()
        running = [m for m in metas if m.status == "running"]
        assert len(running) == 3, f"真并发失败，running={len(running)}（期望 3）"
        await manager.shutdown()
        return ids

    run_async(go())


def test_max_concurrent_queueing(tmp_path, yaml_path):
    """max_concurrent 排队：max=2，start 4 → 同时 running ≤ 2，余 queued（SPEC §6.2）。"""
    manager = make_manager(tmp_path, max_concurrent=2)
    hold = asyncio.Event()

    async def slow_run(self):
        await hold.wait()  # 阻住 2 个 sem 名额，让后续排队

    async def go():
        with patch.object(Orchestrator, "run", slow_run):
            await asyncio.gather(
                *[manager.start_run(str(yaml_path), {}, None, None) for _ in range(4)]
            )
            await asyncio.sleep(0.05)  # 让前 2 个进 sem
            metas = manager.list_runs()
            running = [m for m in metas if m.status == "running"]
            queued = [m for m in metas if m.status == "queued"]
            assert len(running) == 2, f"running={len(running)}（期望 ≤2）"
            assert len(queued) == 2, f"queued={len(queued)}（期望 2）"
        hold.set()  # 放行
        await manager.shutdown()

    run_async(go())


# ── 懒加载红线（SPEC §0.1 铁律 2）────────────────────────────────────────


def test_list_runs_no_events_field(tmp_path, yaml_path):
    """list_runs 返回 RunMeta，断言无 events 字段（懒加载红线，SPEC §0.1 铁律 2）。"""
    manager = make_manager(tmp_path)

    async def go():
        await manager.start_run(str(yaml_path), {}, None, None)
        # 等完成
        metas = manager.list_runs()
        for m in metas:
            # RunMeta 是 dataclass，无 events 字段
            assert not hasattr(m, "events"), "RunMeta 不应有 events 字段（懒加载红线）"
            # dict 形态也无 events（routes 层 _meta_to_dict 也保证）
        await manager.shutdown()

    run_async(go())


def test_runmeta_dataclass_fields():
    """RunMeta 字段集 = 元数据 7 项，无 events（SPEC §2.2）。"""
    import dataclasses

    fields = {f.name for f in dataclasses.fields(RunMeta)}
    assert fields == {
        "run_id", "workflow_name", "status", "progress", "cost", "elapsed", "error"
    }
    assert "events" not in fields


# ── status 转换（SPEC §6.2）──────────────────────────────────────────────


def test_status_transition_completed(tmp_path, yaml_path):
    """status：queued → running → completed（真实 demo run 成功）。"""
    manager = make_manager(tmp_path)

    async def go():
        rid = await manager.start_run(str(yaml_path), {}, None, None)
        await manager.wait_done(rid, timeout=10.0)
        handle = manager.get_handle(rid)
        assert handle.status == "completed"
        assert handle.error is None
        await manager.shutdown()

    run_async(go())


def test_status_transition_failed(tmp_path, yaml_path):
    """status：running → failed（mock Orchestrator.run raise → failed + error 记录）。"""
    manager = make_manager(tmp_path)

    async def go():
        with patch.object(Orchestrator, "run", AsyncMock(side_effect=RuntimeError("boom"))):
            rid = await manager.start_run(str(yaml_path), {}, None, None)
            await manager.wait_done(rid, timeout=5.0)
        handle = manager.get_handle(rid)
        assert handle.status == "failed"
        assert handle.error is not None
        assert "boom" in handle.error
        await manager.shutdown()

    run_async(go())


# ── 元数据从 tape 派生（SPEC §9 决策 6）──────────────────────────────────


def test_metadata_progress_matches_replay_state(tmp_path, yaml_path):
    """元数据 progress 的 done 数 == replay_state(tape).node_status 的 done 数（§9 决策 6）。"""
    manager = make_manager(tmp_path)

    async def go():
        rid = await manager.start_run(str(yaml_path), {}, None, None)
        await manager.wait_done(rid, timeout=10.0)
        metas = manager.list_runs()
        meta = next(m for m in metas if m.run_id == rid)
        handle = manager.get_handle(rid)
        state = replay_state(handle.tape)
        done_from_state = sum(1 for s in state.node_status.values() if s == "done")
        done_from_meta = int(meta.progress.split("/")[0])
        assert done_from_meta == done_from_state, (
            f"元数据 done={done_from_meta} ≠ replay_state done={done_from_state}"
        )
        # workflow_name 也来自 state（tape 派生）
        assert meta.workflow_name == "demo"
        await manager.shutdown()

    run_async(go())


def test_get_run_events_matches_tape_replay(tmp_path, yaml_path):
    """get_run_events 唯一来源 = tape.replay（断言相等，SPEC §0.1 铁律 1）。"""
    manager = make_manager(tmp_path)

    async def go():
        rid = await manager.start_run(str(yaml_path), {}, None, None)
        await manager.wait_done(rid, timeout=10.0)
        events = manager.get_run_events(rid)
        handle = manager.get_handle(rid)
        tape_events = list(handle.tape.replay())
        assert len(events) == len(tape_events)
        assert [e.seq for e in events] == [e.seq for e in tape_events]
        assert [e.type for e in events] == [e.type for e in tape_events]
        await manager.shutdown()

    run_async(go())


def test_get_run_events_unknown_run_raises(tmp_path):
    """未知 run_id → KeyError（fail loud，不静默返回空）。"""
    manager = make_manager(tmp_path)

    async def go():
        with pytest.raises(KeyError):
            manager.get_run_events("nope")
        await manager.shutdown()

    run_async(go())


def test_get_handle_unknown_returns_none(tmp_path):
    """未知 run_id → None（不 raise，WS / routes 据此 404）。"""
    manager = make_manager(tmp_path)

    async def go():
        assert manager.get_handle("nope") is None
        await manager.shutdown()

    run_async(go())


# ── 生命周期干净（无 leaked task）─────────────────────────────────────────


def test_shutdown_stops_gate_handlers(tmp_path, yaml_path):
    """shutdown 后所有 handle 的 gate_handler 已 stop（_gate_started=False）。"""
    manager = make_manager(tmp_path)

    async def go():
        rid = await manager.start_run(str(yaml_path), {}, None, None)
        await manager.wait_done(rid, timeout=10.0)
        await manager.shutdown()
        handle = manager.get_handle(rid)
        assert handle._gate_started is False
        # task 已 done
        assert handle._task is not None and handle._task.done()

    run_async(go())


# ── phase-10 技术债回填：setup_outputs 注入 + resume 边界 ────────────────────


def _setup_workflow_yaml(tmp_path: Path) -> Path:
    """带 setup phase 的 workflow（setup_outputs 注入路径用）。"""
    p = tmp_path / "setup_wf.yaml"
    p.write_text(
        """
name: setup_wf
description: setup phase demo（测试用）
setup:
  - name: collector
    kind: agent
    prompt: "collect the host"
entry: a
nodes:
  - name: a
    kind: script
    command: "echo {{ setup.collector.output.host }}"
    routes:
      - to: $end
outputs:
  result: "{{ a.output.stdout }}"
""",
        encoding="utf-8",
    )
    return p


def test_start_run_injects_setup_outputs_to_completion(tmp_path):
    """setup_outputs 透传 RunManager → orchestrator → render，跑到 completed。

    端到端验证 phase-10 技术债回填：MCP 边界收集的 setup_outputs 真注入 runtime，
    execute phase 能消费 ``{{ setup.* }}``。
    """
    yaml_path = _setup_workflow_yaml(tmp_path)
    manager = make_manager(tmp_path)

    async def go():
        rid = await manager.start_run(
            str(yaml_path), {}, None, None,
            setup_outputs={"collector": {"host": "orbittest"}},
        )
        await manager.wait_done(rid, timeout=10.0)
        handle = manager.get_handle(rid)
        assert handle.status == "completed"
        completed = [e for e in manager.get_run_events(rid)
                     if e.type == "workflow_completed"][0]
        assert "orbittest" in completed.data["outputs"]["result"]
        await manager.shutdown()

    run_async(go())


def test_start_run_resume_with_setup_phase_fails_loud(tmp_path):
    """resume + setup workflow → fail loud（setup_outputs 未持久化，边界声明）。"""
    yaml_path = _setup_workflow_yaml(tmp_path)
    manager = make_manager(tmp_path)

    async def go():
        with pytest.raises(ValueError, match="setup workflow 暂不支持 resume"):
            await manager.start_run(str(yaml_path), {}, None, None, resume=True)
        await manager.shutdown()

    run_async(go())
