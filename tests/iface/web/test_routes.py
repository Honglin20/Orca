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


# ── GET /api/runs/<id>/assets/<path>（SPEC §0 D10）────────────────────────


def test_get_run_asset_serves_file(tmp_path, yaml_path):
    """GET /api/runs/<id>/assets/<rel> 返回 run 私有资源字节流（D10 happy path）。

    意图：前端 markdown ``![](diagram.png)`` rewrite 后能从后端拉到文件。
    """
    from tests.iface.web.conftest import make_manager

    manager = make_manager(tmp_path)

    async def go():
        rid = await manager.start_run(str(yaml_path), {}, None, None)
        await manager.wait_done(rid, timeout=10.0)
        # 造一个 asset 文件（agent 本应写到此处；测试直接落盘）
        assets_dir = manager.runs_dir / rid / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        (assets_dir / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
        async with _client_factory(manager) as client:
            resp = await client.get(f"/api/runs/{rid}/assets/diagram.png")
        assert resp.status_code == 200
        assert resp.content.startswith(b"\x89PNG")
        await manager.shutdown()

    run_async(go())


def test_get_run_asset_unknown_run_404(tmp_path):
    """未知 run_id → 404（不暴露 fs）。"""
    from tests.iface.web.conftest import make_manager

    manager = make_manager(tmp_path)

    async def go():
        async with _client_factory(manager) as client:
            resp = await client.get("/api/runs/no-such-run/assets/x.png")
        assert resp.status_code == 404
        await manager.shutdown()

    run_async(go())


def test_get_run_asset_missing_file_404(tmp_path, yaml_path):
    """run 存在但 asset 不存在 → 404（fail loud）。"""
    from tests.iface.web.conftest import make_manager

    manager = make_manager(tmp_path)

    async def go():
        rid = await manager.start_run(str(yaml_path), {}, None, None)
        await manager.wait_done(rid, timeout=10.0)
        async with _client_factory(manager) as client:
            resp = await client.get(f"/api/runs/{rid}/assets/never.png")
        assert resp.status_code == 404
        await manager.shutdown()

    run_async(go())


def test_get_run_asset_traversal_404(tmp_path, yaml_path):
    """``..`` 越界 → 404（path escape 守卫，SPEC §0 D10 fail loud）。"""
    from tests.iface.web.conftest import make_manager

    manager = make_manager(tmp_path)

    async def go():
        rid = await manager.start_run(str(yaml_path), {}, None, None)
        await manager.wait_done(rid, timeout=10.0)
        async with _client_factory(manager) as client:
            # 试图逃逸到 tape 文件本身
            resp = await client.get(f"/api/runs/{rid}/assets/..%2f{rid}.jsonl")
        assert resp.status_code == 404
        await manager.shutdown()

    run_async(go())


def test_resolve_asset_path_unknown_run_returns_none(tmp_path):
    """RunManager.resolve_asset_path 单元：未知 run_id → None。"""
    from tests.iface.web.conftest import make_manager

    manager = make_manager(tmp_path)
    assert manager.resolve_asset_path("nope", "x.png") is None


def test_resolve_asset_path_traversal_returns_none(tmp_path, yaml_path):
    """RunManager.resolve_asset_path 单元：``..`` 越界 → None。

    RunManager 已知 run_id 但路径越界 → None（不抛，不暴露 fs 细节）。
    """
    from tests.iface.web.conftest import make_manager, run_async

    manager = make_manager(tmp_path)

    async def go():
        rid = await manager.start_run(str(yaml_path), {}, None, None)
        await manager.wait_done(rid, timeout=10.0)
        # ``..`` 应被 resolve + relative_to 守卫拦
        assert manager.resolve_asset_path(rid, f"../{rid}.jsonl") is None
        # 绝对路径 escape 同样拦
        assert manager.resolve_asset_path(rid, "/etc/passwd") is None
        # 空路径
        assert manager.resolve_asset_path(rid, "  ") is None
        await manager.shutdown()

    run_async(go())


def test_resolve_asset_path_rejects_symlink(tmp_path, yaml_path):
    """symlink（即便指向 assets_root 内）→ None（防御纵深，防 symlink 逃逸）。

    SPEC §0 D10 安全 follow-up：``is_file()`` 跟随 symlink 判定为 True，故需显式
    ``is_symlink()`` 拒绝。否则 agent 误/恶意在 assets 内放 symlink 指向 /etc/passwd 等
    敏感文件，会被 ``FileResponse`` 直接吐给前端。
    """
    from tests.iface.web.conftest import make_manager, run_async

    manager = make_manager(tmp_path)

    async def go():
        rid = await manager.start_run(str(yaml_path), {}, None, None)
        await manager.wait_done(rid, timeout=10.0)
        assets_dir = manager.runs_dir / rid / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        target = assets_dir / "real.png"
        target.write_bytes(b"\x89PNG\r\n\x1a\nREAL")
        link = assets_dir / "link.png"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("filesystem does not support symlinks")
        # 拒绝 symlink 即便指向 assets_root 内合法文件
        assert manager.resolve_asset_path(rid, "link.png") is None
        # 真 hardlink / 普通文件仍可解析（resolve 后路径对齐，避免 /tmp → /private/tmp 差异）
        assert manager.resolve_asset_path(rid, "real.png") == target.resolve()
        await manager.shutdown()

    run_async(go())

