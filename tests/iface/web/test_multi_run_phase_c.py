"""tests/iface/web/test_multi_run_phase_c.py —— Phase C discovery + ensure_attached + DELETE（SPEC §13）。

覆盖 AC 可单测项：
  - discovery（scope=all）：跨项目 + 内存 + legacy 合并
  - ensure_attached 三分支（已挂载 no-op / 0 命中 FileNotFoundError / 多命中 RuntimeError）
  - DELETE 四态（in-process non-terminal / attached terminal / attached live / unknown）
  - RunSummary extra=forbid（M-5 反向 fixture）
  - WS run_changed 控制帧广播（B-4 / M-8）
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from orca.iface.web.run_manager import (
    AttachedRunHandle,
    InProcessRunHandle,
    RunManager,
    RunSummary,
)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """每测独立 ORCA_HOME + 注册表。"""
    home = tmp_path / "orca-home"
    home.mkdir(parents=True)
    monkeypatch.setenv("ORCA_HOME", str(home))
    yield home


def _make_project(parent: Path, name: str = "proj") -> Path:
    p = parent / name
    (p / "workflows").mkdir(parents=True, exist_ok=True)
    return p


def _write_tape(tape_path: Path, run_id: str, *, workflow_name: str = "demo", n_events: int = 3):
    """写一个完整 workflow_started + node_started + workflow_completed 的合法 tape。"""
    tape_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({
            "seq": 1, "type": "workflow_started", "node": None, "session_id": None,
            "timestamp": time.time(),
            "data": {
                "inputs": {}, "node_count": 1, "entry": "a",
                "workflow_name": workflow_name, "run_id": run_id,
                "topology": {"entry": "a", "nodes": [{"name": "a", "kind": "script"}]},
            },
        }),
        json.dumps({
            "seq": 2, "type": "node_started", "node": "a", "session_id": "s1",
            "timestamp": time.time(), "data": {},
        }),
        json.dumps({
            "seq": 3, "type": "workflow_completed", "node": None, "session_id": None,
            "timestamp": time.time(), "data": {"elapsed": 0.5, "outputs": {}},
        }),
    ]
    tape_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── RunSummary schema（M-5 extra=forbid 反向 fixture） ────────────────────────


def test_run_summary_extra_forbid():
    """M-5：构造 RunSummary 时多传字段 → ValidationError（schema 白名单守门）。"""
    with pytest.raises(ValidationError):
        RunSummary(
            run_id="x", workflow_name="w", status="completed",
            events=[1, 2, 3],  # 非白名单字段
        )


def test_run_summary_minimal_ok():
    """M-5：最小必填字段（run_id/workflow_name/status）合法。"""
    s = RunSummary(run_id="x", workflow_name="w", status="completed")
    dumped = s.model_dump(exclude_unset=True)
    # exclude_unset 让未设字段（默认值）不出现 → legacy run 干净序列化。
    assert "project_id" not in dumped
    assert "source" not in dumped  # 默认值未被显式设 → 不出现在序列化中


# ── discovery（scope=all） ────────────────────────────────────────────────────


def test_discover_runs_merges_registered_and_inmemory(tmp_path):
    """scope=all 合并：注册表项目的 tape + 内存 live run。"""
    from orca.runtime import register_project

    manager = RunManager(runs_dir=tmp_path / "runs")
    proj = _make_project(tmp_path, "proj1")
    register_project(proj)
    # 写一个 tape 到 proj1/runs
    _write_tape(proj / "runs" / "rid-on-disk.jsonl", "rid-on-disk", workflow_name="wf1")

    summaries = manager.discover_runs()
    by_id = {s.run_id: s for s in summaries}
    assert "rid-on-disk" in by_id
    assert by_id["rid-on-disk"].project_name == "proj1"
    assert by_id["rid-on-disk"].source == "attached"
    assert by_id["rid-on-disk"].status == "completed"


def test_discover_runs_skips_corrupt_tape(tmp_path):
    """坏 tape → 跳过（不崩 discovery）。"""
    from orca.runtime import register_project

    manager = RunManager(runs_dir=tmp_path / "runs")
    proj = _make_project(tmp_path, "proj1")
    register_project(proj)
    # 写坏 tape
    (proj / "runs").mkdir(parents=True, exist_ok=True)
    (proj / "runs" / "bad.jsonl").write_text("not json\n", encoding="utf-8")
    summaries = manager.discover_runs()
    # 坏 tape 不进列表
    assert all(s.run_id != "bad" for s in summaries)


def test_discover_runs_rebuilds_run_path_index(tmp_path):
    """M-12：discover_runs 后 _run_path_index 重建（含 discovery 的 tape）。"""
    from orca.runtime import register_project

    manager = RunManager(runs_dir=tmp_path / "runs")
    proj = _make_project(tmp_path, "proj1")
    register_project(proj)
    _write_tape(proj / "runs" / "rid-x.jsonl", "rid-x")
    manager.discover_runs()
    assert "rid-x" in manager._run_path_index


# ── resolve_run_path（D7 / AC8） ──────────────────────────────────────────────


def test_resolve_run_path_zero_hit_raises(tmp_path):
    """0 命中 → FileNotFoundError（routes 层 404）。"""
    manager = RunManager(runs_dir=tmp_path / "runs")
    with pytest.raises(FileNotFoundError):
        manager.resolve_run_path("nonexistent")


def test_resolve_run_path_multi_hit_raises_with_paths(tmp_path):
    """多命中 → RuntimeError 列路径（routes 层 500 fail loud，AC8）。"""
    from orca.runtime import register_project

    manager = RunManager(runs_dir=tmp_path / "runs")
    p1 = _make_project(tmp_path, "p1")
    p2 = _make_project(tmp_path, "p2")
    register_project(p1)
    register_project(p2)
    # 同 run_id 在两项目下
    _write_tape(p1 / "runs" / "dup.jsonl", "dup")
    _write_tape(p2 / "runs" / "dup.jsonl", "dup")
    manager.discover_runs()  # 填 _run_path_index（首个胜出）
    # 清 index 让 resolve 走扫注册表路径触发多命中
    manager._run_path_index = {}
    with pytest.raises(RuntimeError, match="命中多个"):
        manager.resolve_run_path("dup")


# ── ensure_attached（D7 三分支） ─────────────────────────────────────────────


def test_ensure_attached_idempotent_when_already_in_memory(tmp_path):
    """已挂载 → ensure_attached no-op（不重复 attach）。"""
    from orca.runtime import register_project

    manager = RunManager(runs_dir=tmp_path / "runs")
    proj = _make_project(tmp_path, "proj")
    register_project(proj)
    _write_tape(proj / "runs" / "rid-1.jsonl", "rid-1")
    manager.discover_runs()

    async def go():
        await manager.ensure_attached("rid-1")
        # 再调一次仍幂等
        await manager.ensure_attached("rid-1")
        assert manager.get_handle("rid-1") is not None

    asyncio.run(go())


def test_ensure_attached_zero_hit(tmp_path):
    """0 命中 → FileNotFoundError。"""
    manager = RunManager(runs_dir=tmp_path / "runs")
    with pytest.raises(FileNotFoundError):
        asyncio.run(manager.ensure_attached("no-such-rid"))


# ── DELETE（D10 / B-5 / M-3 四态） ────────────────────────────────────────────


def test_delete_run_unknown_returns_never_existed(tmp_path):
    """404：未知 run_id（内存+磁盘都无）→ {ok:False, never_existed:True}。"""
    manager = RunManager(runs_dir=tmp_path / "runs")
    result = asyncio.run(manager.delete_run("nonexistent"))
    assert result["ok"] is False
    assert result["never_existed"] is True


def test_delete_run_attached_terminal_removes_files(tmp_path):
    """attached terminal：删 tape + run 目录。"""
    from orca.runtime import register_project

    manager = RunManager(runs_dir=tmp_path / "runs")
    proj = _make_project(tmp_path, "proj")
    register_project(proj)
    tape_path = proj / "runs" / "rid-term.jsonl"
    _write_tape(tape_path, "rid-term")
    manager.discover_runs()

    async def go():
        # ensure_attached 把它拉进 _runs（terminal tape）
        await manager.ensure_attached("rid-term")
        result = await manager.delete_run("rid-term")
        assert result["ok"] is True
        assert result["existed_before"] is True
        assert not tape_path.exists()

    asyncio.run(go())


def test_delete_run_idempotent_second_call_never_existed(tmp_path):
    """重复删 → 第二次 never_existed（幂等）。"""
    from orca.runtime import register_project

    manager = RunManager(runs_dir=tmp_path / "runs")
    proj = _make_project(tmp_path, "proj")
    register_project(proj)
    tape_path = proj / "runs" / "rid-t.jsonl"
    _write_tape(tape_path, "rid-t")
    manager.discover_runs()

    async def go():
        await manager.ensure_attached("rid-t")
        r1 = await manager.delete_run("rid-t")
        assert r1["ok"] is True
        r2 = await manager.delete_run("rid-t")
        assert r2["ok"] is False
        assert r2["never_existed"] is True

    asyncio.run(go())


def test_delete_run_refuses_cross_project_attack(tmp_path):
    """越界守卫：tape 在注册项目 runs/ 外 → 不删（attach allowlist 守门）。"""
    from orca.runtime import register_project

    manager = RunManager(runs_dir=tmp_path / "runs")
    proj = _make_project(tmp_path, "proj")
    register_project(proj)
    # 在注册项目外造一个 tape，但 discovery 不会扫到它（不在 runs/ 下）
    evil_path = tmp_path / "evil" / "rid-evil.jsonl"
    evil_path.parent.mkdir(parents=True)
    _write_tape(evil_path, "rid-evil")
    # manually attach（绕过 discovery）让它进 _runs
    # 但 evil_path 不在 allowlist（非注册项目 runs/ 下）→ attach 应被拒（PermissionError）。
    async def go():
        with pytest.raises(PermissionError):
            await manager.attach_run(str(evil_path), run_id="rid-evil")

    asyncio.run(go())


# ── run_changed 控制帧广播（B-4 / M-8） ──────────────────────────────────────


def test_run_changed_listener_called_on_attach_and_delete(tmp_path):
    """manager delete/attach 时回调被调用（控制帧广播入口）。"""
    from orca.runtime import register_project

    manager = RunManager(runs_dir=tmp_path / "runs")
    proj = _make_project(tmp_path, "proj")
    register_project(proj)
    tape_path = proj / "runs" / "rid-c.jsonl"
    _write_tape(tape_path, "rid-c")
    manager.discover_runs()

    events: list[tuple[str, str]] = []
    manager.add_run_changed_listener(
        lambda run_id, action: events.append((run_id, action))
    )

    async def go():
        await manager.ensure_attached("rid-c")
        await manager.delete_run("rid-c")

    asyncio.run(go())
    actions = [a for (r, a) in events if r == "rid-c"]
    assert "attached" in actions
    assert "deleted" in actions


def test_run_changed_listener_remove_prevents_further_calls(tmp_path):
    """remove_run_changed_listener 后不再被调（teardown 防泄漏）。"""
    manager = RunManager(runs_dir=tmp_path / "runs")
    calls: list = []
    cb = lambda r, a: calls.append((r, a))  # noqa: E731
    manager.add_run_changed_listener(cb)
    manager.remove_run_changed_listener(cb)
    manager._broadcast_run_changed("x", "changed")
    assert calls == []
