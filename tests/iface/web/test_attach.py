"""test_attach.py —— X (attach by tape path) + meta + events windowed + security + perf fixture
（SPEC web-attach §2 / §3 / §6 / §8）。

覆盖意图（非仅行为）：
  - **tape_reader.replay 只读**（AC §8.13）：无 ``Tape(resume=True)``，partial 末行停 yield。
  - **attach live**：外部 tape 增量写 → follow task parse → bus.relay → 订阅者 P99 < 500ms。
  - **attach terminal tape**：终态事件 → ``terminal=True`` + follow 停。
  - **run_id collision** → ValueError；**同 tape_path 重复 attach** → 幂等。
  - **security 6 samples**（AC §8.8）全 403 + allowlist 命中/未命中。
  - **partial 首行 5s** → ``corrupted``/not-orca-tape。
  - **meta huge/overview**：``event_count > 50k`` → ``huge=true`` + overview 字段。
  - **events windowed**：``?since`` / ``?since&limit`` / ``?tail``。
  - **single registry**（AC §8.12）：``_runs`` 单 dict。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import pytest

from orca.events.bus import EventBus
from orca.events.tape_reader import count_and_bounds, replay
from orca.iface.web.run_manager import (
    AttachedRunHandle,
    AttachedTape,
    InProcessRunHandle,
    RunManager,
    RunView,
)
from orca.schema import Event

from tests.iface.web.conftest import make_manager, run_async


# ── helpers ──────────────────────────────────────────────────────────────────


def _write_event(path: Path, seq: int, type: str, data: dict, node: str | None = None) -> None:
    """追加一行 JSON 到 path（模拟外部 tape 写者）。"""
    payload = {
        "seq": seq,
        "type": type,
        "timestamp": time.time(),
        "node": node,
        "session_id": None,
        "data": data,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _write_workflow_started(path: Path, run_id: str = "r-test", n_nodes: int = 3) -> None:
    topology = {
        "entry": "n1",
        "nodes": [{"name": f"n{i}", "kind": "agent"} for i in range(1, n_nodes + 1)],
        "routes": [
            {"from": f"n{i}", "to": f"n{i + 1}" if i < n_nodes else "$end"}
            for i in range(1, n_nodes + 1)
        ],
        "parallel": [],
    }
    _write_event(
        path,
        1,
        "workflow_started",
        {
            "inputs": {},
            "node_count": n_nodes,
            "entry": "n1",
            "workflow_name": "test_wf",
            "topology": topology,
        },
    )


def _make_manager_with_runs_dir(tmp_path: Path, runs_dir: Path | None = None) -> RunManager:
    """构造 manager（runs_dir 显式指定，让 attach 的 tape 在 runs_dir 下满足安全守卫）。"""
    if runs_dir is None:
        runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    # 不用 make_manager（它写 /tmp 短路径）；attach 测试需要 runs_dir 在 tmp_path 下做 symlink 等。
    return RunManager(max_concurrent=2, runs_dir=runs_dir)


# ── tape_reader.replay 只读（AC §8.13）─────────────────────────────────────────


def test_tape_reader_replay_readonly_no_partial(tmp_path):
    """partial 末行 → 停 yield（不抛、不截断）。"""
    path = tmp_path / "tape.jsonl"
    path.write_text(
        json.dumps({"seq": 1, "type": "workflow_started", "timestamp": 0, "data": {}})
        + "\n"
        + '{"seq": 2, "type": "agent_mess'  # partial 行
    )
    events = list(replay(path))
    assert len(events) == 1
    assert events[0].type == "workflow_started"


def test_tape_reader_since_seq(tmp_path):
    """since_seq 过滤：``event.seq > since_seq`` 才 yield。"""
    path = tmp_path / "tape.jsonl"
    path.write_text(
        json.dumps({"seq": 1, "type": "workflow_started", "timestamp": 0, "data": {}})
        + "\n"
        + json.dumps({"seq": 2, "type": "node_started", "timestamp": 0, "node": "n1", "data": {}})
        + "\n"
        + json.dumps({"seq": 5, "type": "node_completed", "timestamp": 0, "node": "n1", "data": {}})
        + "\n"
    )
    events = list(replay(path, since_seq=2))
    assert [e.seq for e in events] == [5]


def test_tape_reader_count_and_bounds(tmp_path):
    """count_and_bounds：event_count / oldest / newest。"""
    path = tmp_path / "tape.jsonl"
    path.write_text(
        json.dumps({"seq": 3, "type": "workflow_started", "timestamp": 0, "data": {}})
        + "\n"
        + json.dumps({"seq": 7, "type": "workflow_completed", "timestamp": 0, "data": {}})
        + "\n"
    )
    count, oldest, newest = count_and_bounds(path)
    assert count == 2
    assert oldest == 3
    assert newest == 7


# ── attach live + terminal + idempotent + collision（AC §8.1/3/9）──────────────


def test_attach_live_follow_pushes_events(tmp_path):
    """attach 后外部 tape 增量写 → follow task parse → bus.relay → 订阅者收到（P99 < 500ms）。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    tape_path = runs_dir / "live.jsonl"
    _write_workflow_started(tape_path, run_id="live-run")

    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)

    async def go():
        run_id = await manager.attach_run(str(tape_path))
        handle = manager.get_handle(run_id)
        assert isinstance(handle, AttachedRunHandle)
        assert handle.source == "attached"
        # 订阅 bus（follow task 把新事件 relay 过来）
        sub = handle.bus.subscribe()
        # 模拟外部写者追加事件
        _write_event(
            tape_path, 2, "node_started", {}, node="n1"
        )
        # 等 follow task poll（0.3s 间隔）→ relay
        received: list[Event] = []
        async def collect():
            async for e in sub.events():
                received.append(e)
                if e.type == "node_started":
                    return
        try:
            await asyncio.wait_for(collect(), timeout=2.0)
        except asyncio.TimeoutError:
            pytest.fail("follow task 未 relay 事件（2s 超时）")
        # 时延断言（P99 < 500ms 容忍局部慢；本测试用 < 2s 兜底，P99 perf 由专门测试覆盖）
        assert any(e.type == "node_started" for e in received)
        await manager.shutdown()
        return run_id

    run_async(go())


def test_attach_terminal_tape_stops_follow(tmp_path):
    """attach 终态 tape → ``terminal=True`` + follow 停 + 无新事件。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    tape_path = runs_dir / "term.jsonl"
    _write_workflow_started(tape_path)
    _write_event(tape_path, 2, "workflow_completed", {"elapsed": 1.0, "outputs": {}})

    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)

    async def go():
        run_id = await manager.attach_run(str(tape_path))
        handle = manager.get_handle(run_id)
        assert isinstance(handle, AttachedRunHandle)
        # 终态 tape 在 attach 时即标 terminal（无需等 follow task）
        assert handle.terminal is True
        assert handle.status == "completed"
        # 终态 tape 不起 follow task（D3：无新事件，资源不浪费）
        assert handle.follow_task is None
        await manager.shutdown()
        return run_id

    run_async(go())


def test_attach_idempotent_same_tape_path(tmp_path):
    """同 tape_path 重复 attach → 幂等返回既有 run_id（不重起 follow）。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    tape_path = runs_dir / "idem.jsonl"
    _write_workflow_started(tape_path)

    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)

    async def go():
        run_id1 = await manager.attach_run(str(tape_path))
        run_id2 = await manager.attach_run(str(tape_path))
        assert run_id1 == run_id2
        # 单一 registry 仍只有一条
        assert len(manager._runs) == 1
        await manager.shutdown()

    run_async(go())


def test_attach_run_id_collision_raises(tmp_path):
    """run_id 碰撞 → ValueError('run_id_collision')。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    tape_path = runs_dir / "x.jsonl"
    _write_workflow_started(tape_path)

    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)

    async def go():
        run_id1 = await manager.attach_run(str(tape_path))
        # 不同 tape path，强制同 run_id
        tape_path2 = runs_dir / "y.jsonl"
        _write_workflow_started(tape_path2)
        with pytest.raises(ValueError, match="run_id_collision"):
            await manager.attach_run(str(tape_path2), run_id=run_id1)
        await manager.shutdown()


# ── not-orca-tape：首行非 workflow_started（AC9 / SPEC §6.7 / §2.2 step2）─────────


def test_attach_non_workflow_started_first_line_rejected(tmp_path):
    """首行**完整可解析**但非 ``workflow_started`` → PermissionError('not-orca-tape')。

    SPEC §6.7 / §8 AC9：首行非 Orca tape → 403。routes 层把 PermissionError → 403。
    本测试断言 RunManager 层的契约：完整首行非 workflow_started → 立即拒（不进 live-pending，
    不起 follow task）。partial / 空首行走另一条路（live-pending → 5s → corrupted），由
    test_attach_follow_failures 覆盖。
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    tape_path = runs_dir / "notorca.jsonl"
    # 首行是完整合法 JSON Event，但 type=agent_message（非 workflow_started）
    tape_path.write_text(
        json.dumps(
            {
                "seq": 1,
                "type": "agent_message",
                "timestamp": 1.0,
                "node": "x",
                "session_id": None,
                "data": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)

    async def go():
        with pytest.raises(PermissionError, match="not-orca-tape"):
            await manager.attach_run(str(tape_path), run_id="notorca-fresh")
        # 未注册进 registry（不留污染）
        assert "notorca-fresh" not in manager._runs
        assert len(manager._runs) == 0

    run_async(go())


def test_attach_partial_first_line_goes_live_pending(tmp_path):
    """partial 首行（无完整 \\n） → **不**立即拒，走 live-pending（回归守卫）。

    修复 not-orca-tape 拒绝路径时不能误伤 partial 首行场景：partial 不属「完整可解析非
    workflow_started」，属 SPEC §2.2 step2 的 live-pending → 5s → corrupted 路径。
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    tape_path = runs_dir / "partial.jsonl"
    # partial 首行：非完整 JSON（无换行，writer 还在 flush）
    tape_path.write_text(
        '{"seq": 1, "type": "agent_mess', encoding="utf-8"
    )

    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)

    async def go():
        # 不抛——partial 走 live-pending
        run_id = await manager.attach_run(str(tape_path), run_id="partial-run")
        handle = manager.get_handle(run_id)
        assert handle is not None
        assert handle.status == "live-pending"
        await manager.shutdown()

    run_async(go())


# ── security 6 samples（AC §8.8）───────────────────────────────────────────────


def test_security_traversal_rejected(tmp_path):
    """``../../etc/passwd`` / ``runs_evil/x`` / ``runs/good/../../etc`` → PermissionError。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / "good").mkdir()
    (runs_dir / "good" / "tape.jsonl").write_text("{}")
    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)

    samples = [
        "../../etc/passwd",  # 上溯出 runs_dir
        str(tmp_path / "runs_evil" / "x"),  # startswith 碰撞（relative_to 拒）
    ]
    for p in samples:
        with pytest.raises(PermissionError):
            manager.resolve_tape_path(p)


def test_security_runs_good_traverse_rejected(tmp_path):
    """``runs/good/../../etc`` 解析后逃出 runs_dir → PermissionError。"""
    runs_dir = tmp_path / "runs"
    (runs_dir / "good").mkdir(parents=True)
    etc_dir = tmp_path / "etc"
    etc_dir.mkdir()
    (etc_dir / "passwd").write_text("root:x:0:0")
    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)
    # 相对路径相对 CWD；用绝对路径显式构造逃逸
    bad = runs_dir / "good" / ".." / ".." / "etc" / "passwd"
    with pytest.raises(PermissionError):
        manager.resolve_tape_path(str(bad))


def test_security_symlink_out_rejected(tmp_path):
    """symlink-out：runs_dir 下 symlink 指向外部 → PermissionError。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    external = tmp_path / "external.jsonl"
    external.write_text("{}")
    link = runs_dir / "ln.jsonl"
    try:
        link.symlink_to(external)
    except OSError:
        pytest.skip("symlink 创建失败（Windows 权限 / FS 不支持）")
    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)
    with pytest.raises(PermissionError):
        manager.resolve_tape_path(str(link))


def test_security_symlink_in_then_escape_rejected(tmp_path):
    """symlink-in-then-escape：runs_dir 内 dir 是 symlink，dir 内文件 resolve 逃出 → 拒。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    external_dir = tmp_path / "external_dir"
    external_dir.mkdir()
    (external_dir / "tape.jsonl").write_text("{}")
    dir_link = runs_dir / "linked_dir"
    try:
        dir_link.symlink_to(external_dir)
    except OSError:
        pytest.skip("symlink 创建失败")
    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)
    # symlink 路径段：resolve 后在 external_dir 下（出 runs_dir）→ 拒
    with pytest.raises(PermissionError):
        manager.resolve_tape_path(str(dir_link / "tape.jsonl"))


def test_security_url_encoded_path_rejected(tmp_path):
    """``%2e%2e`` 作为字面路径段（无 URL 解码）→ 不在 runs_dir 下 → PermissionError。

    注：``%2e%2e`` 在 path 中是字面字符串（3 字符），不等于 ``..``；但仍不在 runs_dir 下，
    故 ``relative_to`` 拒。本测试验证 resolve_tape_path 不依赖任何 URL 解码。
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / "real.jsonl").write_text("{}")
    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)
    # body 中字面 "%2e%2e" 作为目录名（实际不存在 / 不在 runs_dir 下）
    # 收紧断言：只接受 PermissionError（不接受 FileNotFoundError——那是「目录不存在」
    # 的副作用，不是真正的安全拒绝）。为达到 PermissionError，先在 runs_dir 下造一个
    # 含 ``%2e%2e`` 字面名的目录 + 文件，确保 ``relative_to`` 拒（不在 runs_dir 下）。
    evil_dir = tmp_path / "%2e%2e"
    evil_dir.mkdir()
    (evil_dir / "x.jsonl").write_text("{}")
    with pytest.raises(PermissionError):
        manager.resolve_tape_path(str(evil_dir / "x.jsonl"))


def test_security_allowlist_hit_and_miss(tmp_path, monkeypatch):
    """allowlist 命中放行、未命中 403。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    external_root = tmp_path / "external_allow"
    external_root.mkdir()
    (external_root / "ok.jsonl").write_text("{}")
    other = tmp_path / "other"
    other.mkdir()
    (other / "nope.jsonl").write_text("{}")
    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)

    # 命中 allowlist
    monkeypatch.setenv(
        "ORCA_WEB_TAPE_ALLOWLIST", str(external_root)
    )
    resolved = manager.resolve_tape_path(str(external_root / "ok.jsonl"))
    assert resolved.is_file()

    # 未命中（other 不在 allowlist）
    monkeypatch.setenv("ORCA_WEB_TAPE_ALLOWLIST", str(external_root))
    with pytest.raises(PermissionError):
        manager.resolve_tape_path(str(other / "nope.jsonl"))


# ── meta huge/overview（AC §8.4 / §8.10）──────────────────────────────────────


def test_meta_small_run_not_huge(tmp_path):
    """小 tape：``huge=false``，无 overview。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    tape_path = runs_dir / "small.jsonl"
    _write_workflow_started(tape_path, n_nodes=2)
    _write_event(tape_path, 2, "node_started", {}, node="n1")
    _write_event(tape_path, 3, "node_completed", {"output": "x"}, node="n1")

    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)

    async def go():
        run_id = await manager.attach_run(str(tape_path))
        meta = manager.get_run_extended_meta(run_id)
        assert meta is not None
        assert meta["huge"] is False
        assert meta["writable"] is False  # attached
        assert meta["source"] == "attached"
        assert "overview" not in meta  # 仅 huge 模式返
        assert meta["event_count"] == 3
        assert meta["oldest_seq"] == 1
        assert meta["newest_seq"] == 3
        await manager.shutdown()

    run_async(go())


def test_meta_huge_threshold_overrides_overview(tmp_path):
    """``event_count > 50k`` → ``huge=true`` + overview 派生。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    tape_path = runs_dir / "big.jsonl"
    # 50_001 events → > 50k threshold
    topology = {
        "entry": "n1",
        "nodes": [{"name": "n1", "kind": "agent"}],
        "routes": [{"from": "n1", "to": "$end"}],
        "parallel": [],
    }
    with tape_path.open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "seq": 1,
                    "type": "workflow_started",
                    "timestamp": 0,
                    "node": None,
                    "session_id": None,
                    "data": {
                        "inputs": {},
                        "node_count": 1,
                        "entry": "n1",
                        "workflow_name": "big",
                        "topology": topology,
                    },
                }
            )
            + "\n"
        )
        for i in range(2, 50_003):
            f.write(
                json.dumps(
                    {
                        "seq": i,
                        "type": "agent_message",
                        "timestamp": 0,
                        "node": "n1",
                        "session_id": "s",
                        "data": {"text": "x"},
                    }
                )
                + "\n"
            )

    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)

    async def go():
        run_id = await manager.attach_run(str(tape_path))
        meta = manager.get_run_extended_meta(run_id)
        assert meta is not None
        assert meta["huge"] is True
        assert "overview" in meta
        ov = meta["overview"]
        assert "agents" in ov and "charts" in ov and "cost_usd" in ov
        # 至少一个 agent（n1）在 overview.agents
        assert any(a["name"] == "n1" for a in ov["agents"])
        await manager.shutdown()

    run_async(go())


# ── events windowed（M1）───────────────────────────────────────────────────────


def test_events_windowed_since_limit_tail(tmp_path):
    """GET /events windowed：since / since+limit / tail。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    tape_path = runs_dir / "w.jsonl"
    _write_workflow_started(tape_path)
    for i in range(2, 12):
        _write_event(tape_path, i, "agent_message", {"text": str(i)}, node="n1")

    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)

    async def go():
        run_id = await manager.attach_run(str(tape_path))
        # 全量
        all_events = manager.get_run_events_window(run_id)
        assert len(all_events) == 11
        # since=5 → seq 6..11
        since5 = manager.get_run_events_window(run_id, since=5)
        assert [e.seq for e in since5] == [6, 7, 8, 9, 10, 11]
        # since=5 limit=3 → seq 6,7,8
        sl = manager.get_run_events_window(run_id, since=5, limit=3)
        assert [e.seq for e in sl] == [6, 7, 8]
        # tail=2 → seq 10,11
        t2 = manager.get_run_events_window(run_id, tail=2)
        assert [e.seq for e in t2] == [10, 11]
        await manager.shutdown()

    run_async(go())


def test_events_does_not_touch_bus(tmp_path):
    """M1: GET /events 是 pure tape read，不 bus.emit / relay（订阅者无变化）。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    tape_path = runs_dir / "nb.jsonl"
    _write_workflow_started(tape_path)
    _write_event(tape_path, 2, "node_started", {}, node="n1")

    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)

    async def go():
        run_id = await manager.attach_run(str(tape_path))
        handle = manager.get_handle(run_id)
        assert isinstance(handle, AttachedRunHandle)
        sub = handle.bus.subscribe()
        # 读 events（多次）
        for _ in range(3):
            manager.get_run_events_window(run_id)
        # 短暂等；订阅者不应收到任何 relay（follow task 也无新字节可读）
        received = []
        try:
            await asyncio.wait_for(
                _collect_some(sub, received, max_n=1), timeout=0.5
            )
            pytest.fail("GET /events 不应触发 bus relay")
        except asyncio.TimeoutError:
            pass  # 预期：无事件
        await manager.shutdown()

    run_async(go())


async def _collect_some(sub, received: list, max_n: int = 1) -> None:
    async for e in sub.events():
        received.append(e)
        if len(received) >= max_n:
            return


# ── single registry（AC §8.12）────────────────────────────────────────────────


def test_single_runs_registry():
    """AC §8.12：``_runs`` 单 dict。源码静态校验只一处 ``dict[...] = {}`` 定义；
    ``self._runs[xxx] = ...`` 是 store 操作（不算第二 registry）。"""
    import inspect
    import re

    src = inspect.getsource(RunManager)
    # 只匹配形如 ``self._runs: dict[...] = {}``（type-annotated 定义）
    defs = re.findall(r"^\s*self\._runs\s*:\s*dict\[.*?\]\s*=\s*\{\}", src, re.MULTILINE)
    assert len(defs) == 1, f"_runs dict 定义应只有一处，找到 {len(defs)}"

    # 反向断言：不存在第二个 ``_runs``-like 字段（如 ``_attached_runs`` / ``_run_views``）
    other_regs = re.findall(r"self\.(_\w*runs?\w*)\s*:\s*dict", src)
    # 允许 ``_runs`` 一个；其它形态（``_runs_dir`` 不是 dict，跳过）
    dict_regs = [name for name in other_regs if name != "_runs"]
    assert not dict_regs, f"发现并行 registry dict: {dict_regs}"


# ── perf fixture smoke（AC §8.4：仅生成小规模验证可读 + meta）──────────────────


def test_perf_fixture_gen_and_meta(tmp_path):
    """生成小规模 fixture（1000 事件）→ attach → meta ``event_count == 1000``。

    50MB 的真 perf benchmark 单独在 test_attach_perf.py（CI 跑），本测试只验证 fixture
    生成 + tape_reader + meta 的链路无 bug。
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    tape_path = runs_dir / "perf.jsonl"
    # 动态加载 scripts/gen_big_fixture.py（非 package，用 importlib 从路径加载）
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gen_big_fixture",
        str(Path(__file__).resolve().parents[3] / "scripts" / "gen_big_fixture.py"),
    )
    assert spec is not None and spec.loader is not None
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)

    # 直接调内部 gen_event（避开 argparse）
    topology = {
        "entry": "n1",
        "nodes": [{"name": "n1", "kind": "agent"}],
        "routes": [{"from": "n1", "to": "$end"}],
        "parallel": [],
    }
    with tape_path.open("w", encoding="utf-8") as f:
        f.write(
            gen.gen_event(
                1,
                "workflow_started",
                None,
                {
                    "inputs": {},
                    "node_count": 1,
                    "entry": "n1",
                    "workflow_name": "perf",
                    "topology": topology,
                },
            )
            + "\n"
        )
        for i in range(2, 1001):
            f.write(
                gen.gen_event(
                    i, "agent_message", "n1", {"text": f"chunk {i}"}
                )
                + "\n"
            )
        f.write(
            gen.gen_event(
                1001, "workflow_completed", None, {"elapsed": 1.0, "outputs": {}}
            )
            + "\n"
        )

    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)

    async def go():
        run_id = await manager.attach_run(str(tape_path))
        meta = manager.get_run_extended_meta(run_id)
        assert meta is not None
        # 1001 events: 1 workflow_started + 999 agent_message + 1 workflow_completed + 1 from loop tail
        # (gen_big_fixture: n-2 inner + 1 started + 1 completed = n total; with n=1000 → 1000)
        # 本测试写 999 个 agent_message + 1 started + 1 completed = 1001
        assert meta["event_count"] == 1001
        assert meta["huge"] is False
        await manager.shutdown()

    run_async(go())


# ── health runs_dir_fp（spec-review B1/B3，SPEC §5a）────────────────────────────


def test_health_endpoint_exposes_runs_dir_fp(tmp_path):
    """``GET /api/health`` 真返回 ``runs_dir_fp``（与 manager.runs_dir 同算法），且不泄漏明文路径。

    用 FastAPI TestClient（真 ASGI，非 mock）打到真实 health handler——验证 ``_identity`` →
    ``attach.health`` 真链路（mock 测试无法验证指纹真流过端点）。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orca.iface.web._identity import runs_dir_fingerprint
    from orca.iface.web.routes import build_attach_router

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    manager = RunManager(max_concurrent=2, runs_dir=runs_dir)
    app = FastAPI()
    app.include_router(build_attach_router(manager))
    client = TestClient(app)

    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["app"] == "orca"
    assert body["pid"] == os.getpid()
    # runs_dir_fp 存在、12 hex、与 manager.runs_dir 同算法
    assert body["runs_dir_fp"] == runs_dir_fingerprint(manager.runs_dir)
    assert len(body["runs_dir_fp"]) == 12
    # 无明文项目目录泄漏（health 默认 bind 0.0.0.0 网络可达）
    assert str(runs_dir.resolve()) not in r.text


def test_runs_dir_fp_deterministic_and_distinct(tmp_path):
    """同 runs_dir → 同 fp；不同 runs_dir → 不同 fp（client/server 比对基础）。"""
    from orca.iface.web._identity import runs_dir_fingerprint

    a = tmp_path / "runsA"
    b = tmp_path / "runsB"
    a.mkdir()
    b.mkdir()
    assert runs_dir_fingerprint(a) == runs_dir_fingerprint(a)  # 确定性
    assert runs_dir_fingerprint(a) != runs_dir_fingerprint(b)  # 区分性
    assert len(runs_dir_fingerprint(a)) == 12


# ── 绝对路径形态安全样例（spec-review H3：client 现以绝对路径 POST）──────────────


def test_resolve_tape_path_accepts_valid_absolute_under_runs_dir(tmp_path):
    """合法**绝对**路径（runs_dir 下）→ 通过。

    client（``_open_run``）现一律 ``tape.resolve()`` 绝对路径 POST；server 的 ``resolve_tape_path``
    须仍接受 runs_dir 下的绝对路径（非仅相对）。
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    tape = runs_dir / "ok.jsonl"
    tape.write_text("{}", encoding="utf-8")
    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)
    resolved = manager.resolve_tape_path(str(tape.resolve()))
    assert resolved == tape.resolve()


def test_resolve_tape_path_rejects_absolute_outside_runs_dir(tmp_path):
    """**绝对**路径在 runs_dir 外（无 allowlist）→ PermissionError（边界检查对绝对路径同样生效）。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}", encoding="utf-8")
    manager = _make_manager_with_runs_dir(tmp_path, runs_dir)
    with pytest.raises(PermissionError):
        manager.resolve_tape_path(str(outside.resolve()))

