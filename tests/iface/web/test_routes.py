"""test_routes.py —— 懒加载 REST 路由（SPEC §6.3 / 计划 A3.5）。

覆盖意图：
  - ``GET /api/runs`` 返回 list[RunMeta]，**断言 body 无 events 字段**（懒加载红线）。
  - ``GET /api/runs/<id>/events`` 返回事件数组（懒加载，run 完成后有事件）。
  - ``GET /api/runs/<id>`` 返回 meta + RunState 快照。
  - ``POST /api/run`` 启动 → ``{run_id, status: "queued"}``。
  - 未知 run_id → 404。
  - 不存在 yaml → 400（ConfigurationError / FileNotFoundError）。

用 httpx AsyncClient + ASGITransport（不启动真 server，同事件循环）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from orca.iface.web.server import create_app

from tests.iface.web.conftest import run_async


def _client_factory(manager):
    """build app + ASGITransport async context manager factory。"""
    app = create_app(manager)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ── GET /api/runs 懒加载（SPEC §0.1 铁律 2）──────────────────────────────


def test_get_runs_no_events_in_body(tmp_path, yaml_path):
    """GET /api/runs 返回 list[RunMeta]，body 无 events 字段（懒加载红线）。"""
    from tests.iface.web.conftest import make_manager

    manager = make_manager(tmp_path)

    async def go():
        await manager.start_run(str(yaml_path), {}, None, None)
        async with _client_factory(manager) as client:
            resp = await client.get("/api/runs")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        for item in data:
            # 懒加载红线：每个 item 无 events 字段
            assert "events" not in item, f"/api/runs body 泄露 events: {item}"
            # 元数据字段齐
            assert {"run_id", "workflow_name", "status", "progress", "cost", "elapsed", "error"} <= set(item)
        await manager.shutdown()

    run_async(go())


def test_get_runs_after_completion_has_status(tmp_path, yaml_path):
    """run 完成后 GET /api/runs 反映 status=completed（实时，SPEC §6.2）。"""
    from tests.iface.web.conftest import make_manager

    manager = make_manager(tmp_path)

    async def go():
        rid = await manager.start_run(str(yaml_path), {}, None, None)
        await manager.wait_done(rid, timeout=10.0)
        async with _client_factory(manager) as client:
            resp = await client.get("/api/runs")
        data = resp.json()
        target = next(i for i in data if i["run_id"] == rid)
        assert target["status"] == "completed"
        await manager.shutdown()

    run_async(go())


# ── GET /api/runs/<id>/events（懒加载全量，SPEC §3.1）─────────────────────


def test_get_run_events_returns_array(tmp_path, yaml_path):
    """GET /api/runs/<id>/events 返回事件数组（懒加载）。"""
    from tests.iface.web.conftest import make_manager

    manager = make_manager(tmp_path)

    async def go():
        rid = await manager.start_run(str(yaml_path), {}, None, None)
        await manager.wait_done(rid, timeout=10.0)
        async with _client_factory(manager) as client:
            resp = await client.get(f"/api/runs/{rid}/events")
        assert resp.status_code == 200
        events = resp.json()
        assert isinstance(events, list)
        assert len(events) > 0
        # 事件结构
        types = [e["type"] for e in events]
        assert "workflow_started" in types
        assert "workflow_completed" in types
        await manager.shutdown()

    run_async(go())


def test_get_run_events_unknown_404(tmp_path):
    """未知 run_id → 404（fail loud）。"""
    from tests.iface.web.conftest import make_manager

    manager = make_manager(tmp_path)

    async def go():
        async with _client_factory(manager) as client:
            resp = await client.get("/api/runs/nope/events")
        assert resp.status_code == 404
        await manager.shutdown()

    run_async(go())


# ── GET /api/runs/<id>（meta + state 快照，SPEC §3.1）─────────────────────


def test_get_run_returns_meta_and_state(tmp_path, yaml_path):
    """GET /api/runs/<id> 返回 {meta, state}，meta 无 events，state 是 RunState dump。"""
    from tests.iface.web.conftest import make_manager

    manager = make_manager(tmp_path)

    async def go():
        rid = await manager.start_run(str(yaml_path), {}, None, None)
        await manager.wait_done(rid, timeout=10.0)
        async with _client_factory(manager) as client:
            resp = await client.get(f"/api/runs/{rid}")
        assert resp.status_code == 200
        body = resp.json()
        assert "meta" in body and "state" in body
        assert "events" not in body["meta"]
        assert body["state"]["status"] == "completed"
        assert body["meta"]["run_id"] == rid
        await manager.shutdown()

    run_async(go())


def test_get_run_unknown_404(tmp_path):
    """未知 run_id → 404。"""
    from tests.iface.web.conftest import make_manager

    manager = make_manager(tmp_path)

    async def go():
        async with _client_factory(manager) as client:
            resp = await client.get("/api/runs/nope")
        assert resp.status_code == 404
        await manager.shutdown()

    run_async(go())


# ── POST /api/run（SPEC §3.3）─────────────────────────────────────────────


def test_post_run_starts_queued(tmp_path, yaml_path):
    """POST /api/run 启动 → {run_id, status: "queued"}。"""
    from tests.iface.web.conftest import make_manager

    manager = make_manager(tmp_path)

    async def go():
        async with _client_factory(manager) as client:
            resp = await client.post("/api/run", json={"yaml_path": str(yaml_path)})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "queued"
        assert body["run_id"]
        await manager.shutdown()

    run_async(go())


def test_post_run_bad_yaml_400(tmp_path):
    """不存在 yaml → 400（fail loud）。"""
    from tests.iface.web.conftest import make_manager

    manager = make_manager(tmp_path)

    async def go():
        async with _client_factory(manager) as client:
            resp = await client.post("/api/run", json={"yaml_path": str(tmp_path / "nope.yaml")})
        assert resp.status_code == 400
        await manager.shutdown()

    run_async(go())


def test_post_run_invalid_yaml_400(tmp_path):
    """畸形 yaml → 400（ConfigurationError 透传）。"""
    from tests.iface.web.conftest import make_manager

    manager = make_manager(tmp_path)
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\nentry: nope\n", encoding="utf-8")  # entry 指向不存在的 node

    async def go():
        async with _client_factory(manager) as client:
            resp = await client.post("/api/run", json={"yaml_path": str(bad)})
        assert resp.status_code == 400
        await manager.shutdown()

    run_async(go())
