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
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from orca.events.replay import replay_state
from orca.iface.web.run_manager import RunHandle, RunManager, RunMeta
from orca.run.orchestrator import Orchestrator

from tests.iface.web.conftest import (
    FakeWebSocket,
    demo_linear_yaml,
    make_manager,
    run_async,
)


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


# ── run-visibility：marker-free 注册 → discovery/attach/WS 可见（AC4a/AC4b/AC5c） ──


def _write_tape_event(path: Path, seq: int, etype: str, data: dict) -> None:
    """追加一行 JSON 到 tape（合成事件，测试用）。"""
    import time as _time
    payload = {
        "seq": seq,
        "type": etype,
        "timestamp": _time.time(),
        "node": None,
        "session_id": None,
        "data": data,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def test_ensure_attached_marker_free_project(tmp_path, monkeypatch):
    """AC4a：marker-free 注册项目 → ensure_attached 成功 + get_run_events 非空。

    run-visibility §4.1 A/C：无 marker 项目经可信自注册（``require_marker=False``）后，
    discovery 的 tape 在 attach allowlist（``is_registered_runs_dir``）内 → ``ensure_attached``
    不抛 + ``get_run_events`` 返回非空。
    """
    from orca.runtime import register_project

    monkeypatch.setenv("ORCA_HOME", str(tmp_path / ".orca_home"))
    proj = tmp_path / "proj"
    proj.mkdir()  # 无 workflows/ / .orca/config.json
    register_project(proj, require_marker=False)
    runs_dir = proj / "runs"
    runs_dir.mkdir()

    # 合成 terminal tape（workflow_started + workflow_completed → 无 follow task，确定性）。
    run_id = "demo-ac4a"
    tape_path = runs_dir / f"{run_id}.jsonl"
    _write_tape_event(tape_path, 1, "workflow_started", {"workflow_name": "demo"})
    _write_tape_event(tape_path, 2, "workflow_completed", {})

    manager = make_manager(tmp_path)

    async def go():
        await manager.ensure_attached(run_id)  # 不抛（marker-free 注册命中 allowlist）
        handle = manager.get_handle(run_id)
        assert handle is not None
        events = manager.get_run_events(run_id)
        assert len(events) >= 2  # ws + wc
        assert [e.type for e in events][:2] == ["workflow_started", "workflow_completed"]
        await manager.shutdown()

    run_async(go())


def test_bus_emit_reaches_ws_subscriber_marker_free(tmp_path, yaml_path, monkeypatch):
    """AC4b：marker-free 注册项目的 run → handle.bus.emit 经 WS pump 确定性收到。

    run-visibility §4.1 C：``start_run`` 切 ``require_marker=False`` → 无 marker 项目也能注册 →
    handle 存在 → WS ``_handle_subscribe`` 成功订阅 → ``bus.emit`` fan-out → pump → WS client 收到。
    用 ``EventBus.emit``（非 publish）；emit 第一动作 ``tape.append``（非绕过 tape，只是不经
    文件轮询 pump）。``FakeWebSocket``（conftest 共享）单 loop 驱动（确定性，codebase 约定见
    ``test_ws.py``）。
    """
    from orca.iface.web.ws_handler import WebServer
    from orca.runtime import list_registered

    monkeypatch.setenv("ORCA_HOME", str(tmp_path / ".orca_home"))
    proj = tmp_path / "proj"
    proj.mkdir()  # 无 marker

    manager = make_manager(tmp_path)
    hold = asyncio.Event()

    async def slow_run(self):
        await hold.wait()  # 阻住 run，让 handle 存在 + bus 活着

    async def go():
        with patch.object(Orchestrator, "run", slow_run):
            run_id = await manager.start_run(
                str(yaml_path), {}, None, None, project_path=str(proj),
            )
            await asyncio.sleep(0.05)  # 让 task 进 sem + running
        handle = manager.get_handle(run_id)
        assert handle is not None, "start_run 后 handle 应存在"
        # marker-free 注册命中（_resolve_project_path_for_run require_marker=False）
        registered = list_registered()
        assert any(
            Path(meta["path"]) == proj.resolve() for meta in registered.values()
        ), f"marker-free 项目未注册：{registered}"

        # WS subscribe → emit → 收到（完整 push chain）
        server = WebServer(manager)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)  # let accept
        ws.feed({"type": "subscribe", "run_id": run_id})
        await asyncio.sleep(0.02)  # let subscribe + pump start
        await handle.bus.emit("node_started", {"node": "a"}, node="a")
        msg = await ws.client_recv(timeout=1.0)
        assert msg["type"] == "node_started"
        assert msg["run_id"] == run_id  # 带 run_id 标签

        # 清理
        ws.feed_disconnect()
        await asyncio.sleep(0.02)
        await server._cleanup(ws)
        endpoint_task.cancel()
        try:
            await endpoint_task
        except (asyncio.CancelledError, Exception):
            pass
        hold.set()  # 放行 run
        await manager.shutdown()

    run_async(go())


def test_start_run_detect_ancestor_tape_lands_detect_root(tmp_path, monkeypatch):
    """AC5c：detect 跳祖先时 tape 落 detect_root/runs + 注册 detect_root + discovery 命中。

    run-visibility §4.2 第 5 行契约：cwd=``<proj>/sub`` + ``<proj>/workflows/`` →
    ``detect_project_root`` 跳到 ``<proj>`` → tape 落 ``<proj>/runs``（非 ``<proj>/sub/runs``）
    → 注册 ``<proj>`` → discovery 扫 ``<proj>/runs`` 命中。scrub ``ORCA_PROJECT_ROOT``
    （与 AC3 口径一致，不靠 env 钉 detect）。
    """
    from orca.runtime import list_registered

    monkeypatch.setenv("ORCA_HOME", str(tmp_path / ".orca_home"))
    monkeypatch.delenv("ORCA_PROJECT_ROOT", raising=False)

    proj = tmp_path / "proj"
    (proj / "workflows").mkdir(parents=True)  # marker 在 proj 级
    sub = proj / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)  # cwd=<proj>/sub → detect 跳祖先 <proj>

    yaml_path = demo_linear_yaml(tmp_path)
    manager = make_manager(tmp_path)

    async def go():
        run_id = await manager.start_run(str(yaml_path), {}, None, None)  # project_path=None → detect
        await manager.wait_done(run_id, timeout=10.0)

        # 注册 detect_root（<proj>，非 <proj>/sub）。
        registered = list_registered()
        assert any(
            Path(meta["path"]) == proj.resolve() for meta in registered.values()
        ), f"未注册 detect_root {proj}：{registered}"
        # tape 落 detect_root/runs（非 sub/runs）。
        tape_path = proj / "runs" / f"{run_id}.jsonl"
        assert tape_path.is_file(), f"tape 未落 detect_root/runs：{tape_path}"
        assert not (sub / "runs").exists(), "tape 不应落 sub/runs"
        # discovery 命中（扫 <proj>/runs）。
        summaries = manager.discover_runs()
        discovered_ids = [s.run_id for s in summaries]
        assert run_id in discovered_ids, f"discovery 未命中 {run_id}：{discovered_ids}"
        await manager.shutdown()

    run_async(go())


# ── _scan_terminal_type / _probe_head_and_terminal × workflow_resumed ──────
# SPEC 2026-08-11 §2.4：resumed run（wf_failed 后跟 wf_resumed）须被判非终态，
# 否则 attach_run 不起 follow + meta.status=failed（违背 AC10 web 可见 running）。


def test_scan_terminal_type_resumed_returns_none(tmp_path):
    """resumed tape [ws, wf_failed, wf_resumed, ns] → None（resume 重新激活 → 非终态）。"""
    from orca.iface.web.run_manager import _scan_terminal_type
    tape = tmp_path / "run.jsonl"
    _write_tape_event(tape, 1, "workflow_started", {})
    _write_tape_event(tape, 2, "workflow_failed", {})
    _write_tape_event(tape, 3, "workflow_resumed", {})
    _write_tape_event(tape, 4, "node_started", {})
    assert _scan_terminal_type(tape) is None


def test_scan_terminal_type_pure_failed_returns_type(tmp_path):
    """纯 wf_failed（无后续 resume）→ 'workflow_failed'（旧行为不变）。"""
    from orca.iface.web.run_manager import _scan_terminal_type
    tape = tmp_path / "run.jsonl"
    _write_tape_event(tape, 1, "workflow_started", {})
    _write_tape_event(tape, 2, "workflow_failed", {})
    assert _scan_terminal_type(tape) == "workflow_failed"


def test_scan_terminal_type_resume_then_complete_returns_type(tmp_path):
    """[wf_failed, wf_resumed, wf_completed] → 'workflow_completed'（resume 后真完成）。"""
    from orca.iface.web.run_manager import _scan_terminal_type
    tape = tmp_path / "run.jsonl"
    _write_tape_event(tape, 1, "workflow_failed", {})
    _write_tape_event(tape, 2, "workflow_resumed", {})
    _write_tape_event(tape, 3, "workflow_completed", {})
    assert _scan_terminal_type(tape) == "workflow_completed"


def test_probe_head_and_terminal_resumed_returns_none_terminal(tmp_path):
    """resumed tape → (first_event=ws, terminal=None)（attach_run 据 terminal=None 起 follow）。"""
    from orca.iface.web.run_manager import _probe_head_and_terminal
    tape = tmp_path / "run.jsonl"
    _write_tape_event(tape, 1, "workflow_started", {})
    _write_tape_event(tape, 2, "workflow_failed", {})
    _write_tape_event(tape, 3, "workflow_resumed", {})
    first, terminal = _probe_head_and_terminal(tape)
    assert first is not None and first.type == "workflow_started"
    assert terminal is None


def test_probe_head_and_terminal_pure_failed_returns_terminal(tmp_path):
    """纯 wf_failed → (first_event=ws, terminal='workflow_failed')。"""
    from orca.iface.web.run_manager import _probe_head_and_terminal
    tape = tmp_path / "run.jsonl"
    _write_tape_event(tape, 1, "workflow_started", {})
    _write_tape_event(tape, 2, "workflow_failed", {})
    first, terminal = _probe_head_and_terminal(tape)
    assert terminal == "workflow_failed"


def test_probe_head_and_terminal_resume_then_complete_returns_terminal(tmp_path):
    """[wf_failed, wf_resumed, wf_completed] → terminal='workflow_completed'（resume 后真完成）。

    覆盖密度对齐 _scan_terminal_type（同模块两函数同三态覆盖）。
    """
    from orca.iface.web.run_manager import _probe_head_and_terminal
    tape = tmp_path / "run.jsonl"
    _write_tape_event(tape, 1, "workflow_failed", {})
    _write_tape_event(tape, 2, "workflow_resumed", {})
    _write_tape_event(tape, 3, "workflow_completed", {})
    _, terminal = _probe_head_and_terminal(tape)
    assert terminal == "workflow_completed"


# ── _scan_meta_overview × workflow_resumed（SPEC 2026-08-11 §2.4）──────────────
# run-list discovery 消费 _scan_meta_overview 的 run_status/ended_ts → resumed run
# 在列表页显示 stale failed（违 AC10）。workflow_resumed 从 BULK 重分类至
# OVERVIEW_AFFECTING，full-parse elif 认 failed→running 翻转 + 清 ended_ts。


def test_scan_meta_overview_resumed_flips_failed_to_running(tmp_path):
    """[ws, wf_failed, wf_resumed, ns] → run_status='running', ended_ts=None。

    意图（AC10 列表页）：_scan_meta_overview 是 run-list discovery 的 status 来源。
    resume-failed 前的 wf_failed 置 status=failed + ended_ts；wf_resumed 须翻回 running
    + 清 ended_ts（run 未结束，不冻结 elapsed）。否则列表页显示 stale failed + 冻结时间。
    """
    from orca.iface.web.run_manager import _scan_meta_overview
    tape = tmp_path / "run.jsonl"
    _write_tape_event(tape, 1, "workflow_started", {"workflow_name": "wf"})
    _write_tape_event(tape, 2, "workflow_failed", {"kind": "exec", "message": "x"})
    _write_tape_event(tape, 3, "workflow_resumed",
                      {"from_tape": str(tape), "resumed_node": "n1",
                       "reason": "recovered_from_failure", "replayed_events": 0})
    _write_tape_event(tape, 4, "node_started", {})
    _, _, _, overview_data = _scan_meta_overview(tape)
    assert overview_data is not None
    overview = overview_data["overview"]
    assert overview["run_status"] == "running", \
        "resumed run 须翻回 running（列表页 status 诚实）"
    assert overview["ended_ts"] is None, \
        "resume 清 stale 终态时间戳（run 未结束，不冻结 elapsed）"


def test_scan_meta_overview_resumed_on_running_is_noop(tmp_path):
    """[ws, wf_resumed] → run_status='running'（headless crash-resume：非 failed 状态不翻）。

    意图：对齐 reducer §1.3 —— wf_resumed 只翻 failed→running；running→running no-op。
    """
    from orca.iface.web.run_manager import _scan_meta_overview
    tape = tmp_path / "run.jsonl"
    _write_tape_event(tape, 1, "workflow_started", {"workflow_name": "wf"})
    _write_tape_event(tape, 2, "workflow_resumed",
                      {"from_tape": str(tape), "resumed_node": "n1",
                       "reason": "crash", "replayed_events": 0})
    _, _, _, overview_data = _scan_meta_overview(tape)
    overview = overview_data["overview"]
    assert overview["run_status"] == "running"




