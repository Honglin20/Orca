"""test_shutdown.py —— ``POST /api/shutdown`` 端点测试（SPEC tars-close §2 / AC4）。

覆盖意图（非仅行为）：
  - **loopback 白名单**：``127.0.0.1`` / ``::1`` / ``::ffff:127.0.0.1`` / ``localhost`` → 200；
    非 loopback（``192.168.x.x``）→ 403。
  - **句柄 wire（B1）**：``app.state.uvicorn_server`` 默认 ``None``（create_app init）→ 500；
    注入真句柄后置位 ``should_exit=True`` 并返 ``{shutting_down, pid}``。
  - **零 cli import**：本端点经 ``app.state`` + stdlib 工作，不恶化 ``test_web_does_not_import_cli``。

机制说明：TestClient 默认 ``client=("testclient", ...)``，故 loopback 断言需显式传
``client=(host, port)`` 才能模拟真实来源 IP（spec-review major：含 IPv4-mapped IPv6）。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orca.iface.web.run_manager import RunManager
from orca.iface.web.routes import build_attach_router
from orca.iface.web.server import create_app


# ── fixtures ──────────────────────────────────────────────────────────────────


class _FakeUvicornServer:
    """最小 uvicorn.Server 替身（只暴露 ``should_exit`` flag 给 shutdown 端点）。"""

    def __init__(self) -> None:
        self.should_exit = False


@pytest.fixture
def app_with_shutdown(tmp_path: Path) -> tuple[FastAPI, _FakeUvicornServer, RunManager]:
    """构造挂了 attach router 的 FastAPI + 注入 fake uvicorn 句柄。

    返回 ``(app, fake_server, manager)``；test 自行用 ``TestClient(app, client=(host, port))``
    模拟不同 client IP。teardown 跑 ``manager.shutdown`` 防 leaked task。
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    manager = RunManager(max_concurrent=2, runs_dir=runs_dir)
    app = create_app(manager)
    fake = _FakeUvicornServer()
    app.state.uvicorn_server = fake
    try:
        yield app, fake, manager
    finally:
        asyncio.run(manager.shutdown())


# ── AC4：loopback 白名单 + 非 loopback 403 ──────────────────────────────────────


@pytest.mark.parametrize(
    "loopback_host",
    ["127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"],
)
def test_shutdown_loopback_variants_return_200(
    app_with_shutdown, loopback_host: str,
) -> None:
    """loopback 白名单（含 IPv4-mapped IPv6）→ 200 + 置位 ``should_exit`` + 返 pid。

    SPEC AC4 / spec-review major：``::ffff:127.0.0.1`` 是 dual-stack socket 把 IPv4 client
    报成 IPv6 形式，不收会假阳性 403。
    """
    app, fake, manager = app_with_shutdown
    client = TestClient(app, client=(loopback_host, 50000))
    fake.should_exit = False  # 重置（多 variant 共用 fixture）
    r = client.post("/api/shutdown")
    assert r.status_code == 200, f"{loopback_host}: 期望 200，实得 {r.status_code}"
    body = r.json()
    assert body["shutting_down"] is True
    assert body["pid"] == os.getpid()
    assert fake.should_exit is True, f"{loopback_host}: should_exit 未置位"


def test_shutdown_non_loopback_returns_403(app_with_shutdown) -> None:
    """非 loopback（192.168.x.x）→ 403（白名单拒绝；与 no-op auth 并存）。"""
    app, fake, manager = app_with_shutdown
    client = TestClient(app, client=("192.168.1.5", 50000))
    fake.should_exit = False
    r = client.post("/api/shutdown")
    assert r.status_code == 403
    # 关键负向：非 loopback 不应触发 should_exit（shutdown 未授权）
    assert fake.should_exit is False, "非 loopback 不应触发 should_exit"


# ── B1：句柄未 wire → 500 ──────────────────────────────────────────────────────


def test_shutdown_handle_none_returns_500(tmp_path: Path) -> None:
    """句柄 None（create_app init / lifecycle 异常未 wire）→ 500 fail loud。

    守门 B1 第二段：create_app 必须 init ``uvicorn_server = None``（让端点在未 wire 时
    fail loud 而非 AttributeError）；run_server / _serve_and_run_inprocess 创建 server
    后覆写为真句柄。
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    manager = RunManager(max_concurrent=2, runs_dir=runs_dir)
    app = create_app(manager)
    # 显式断言 create_app init 为 None（B1 contract）。
    assert app.state.uvicorn_server is None
    client = TestClient(app, client=("127.0.0.1", 50000))
    try:
        r = client.post("/api/shutdown")
        assert r.status_code == 500
        assert "not wired" in r.json()["detail"]
    finally:
        asyncio.run(manager.shutdown())


# ── 端点经真 attach router 挂载（contract：路由名 + 注册路径）────────────────────


def test_shutdown_route_registered_via_attach_router() -> None:
    """``POST /api/shutdown`` 经 ``build_attach_router`` 挂在 ``/api`` 前缀下（contract）。

    守门「端点确实挂上」：遍历 attach router 路由表，断言 ``/api/shutdown`` POST 存在。
    防止未来 refactor 漏挂或改前缀。
    """
    mgr = RunManager(max_concurrent=1, runs_dir="/tmp/orca-shutdown-route-contract/runs")
    try:
        router = build_attach_router(mgr)
        paths = {(route.path, tuple(route.methods)) for route in router.routes}
        assert ("/api/shutdown", ("POST",)) in paths, (
            f"POST /api/shutdown 未挂载在 attach router；实际路由：{paths}"
        )
    finally:
        asyncio.run(mgr.shutdown())


# ── 兼容：health 端点不受 shutdown 改动影响（回归守门）──────────────────────────


def test_health_still_works_alongside_shutdown(app_with_shutdown) -> None:
    """新增 ``/api/shutdown`` 不破坏既有 ``/api/health``（同 router 共存回归）。"""
    app, _, manager = app_with_shutdown
    client = TestClient(app, client=("127.0.0.1", 50000))
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["app"] == "orca"
    assert "orca_home_fp" in body
