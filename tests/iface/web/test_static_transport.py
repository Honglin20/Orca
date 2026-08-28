"""test_static_transport.py —— 传输层头契约：gzip + Cache-Control（SPEC 2026-08-28 C1）。

覆盖（C1.1-C1.4）：
  - ``Accept-Encoding: gzip`` + ≥1024B 资产 → ``Content-Encoding: gzip``；
  - 无 Accept-Encoding 客户端 → 明文回退（无 Content-Encoding）；
  - ``/assets/*`` → ``Cache-Control: public, max-age=31536000, immutable``；
  - SPA fallback ``/`` → ``Cache-Control: no-cache``（index.html 686B <1024 必不压缩）；
  - API JSON 不新增自定义 Cache-Control（C1.4）。

前置（SPEC A4）：``static/assets`` 非空（已 build）——StaticFiles 挂载仅在 assets 目录
存在时成立；库内构建产物入库惯例保证非空（git log 003a98e）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from orca.iface.web import server as server_mod
from orca.iface.web.server import create_app

_ASSETS_DIR = server_mod._STATIC_DIR / "assets"

# StaticFiles 挂载仅当 assets 目录存在；空目录同样无资产可测——两者都跳过（fail-soft，
# SPEC A4 前置 = 先 npm run build）。
_assets_ok = _ASSETS_DIR.is_dir() and any(_ASSETS_DIR.iterdir())

pytestmark = pytest.mark.skipif(
    not _assets_ok, reason="static/assets 不存在或为空——先 cd orca/iface/web/frontend && npm run build"
)


def _large_asset() -> tuple[str, int]:
    """取一个 ≥1024B 的 hashed 资产（gzip 断言对象，SPEC A5 同口径）。"""
    for p in sorted(_ASSETS_DIR.iterdir()):
        if p.is_file() and p.stat().st_size >= 1024:
            return p.name, p.stat().st_size
    pytest.fail("static/assets 无 ≥1024B 文件——构建产物异常")


def test_asset_gzip_and_immutable_cache(manager):
    """>=1024B 资产：gzip + immutable（C1.1 + C1.2）。"""
    name, size = _large_asset()
    app = create_app(manager)
    with TestClient(app) as client:
        resp = client.get(f"/assets/{name}", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"
    # httpx TestClient 自动解压响应体——解压后与磁盘原文件逐字节一致（压缩未损坏内容）。
    assert resp.content == (_ASSETS_DIR / name).read_bytes()
    assert len(resp.content) == size


def test_asset_plain_without_accept_encoding(manager):
    """无 gzip 能力客户端 → 明文回退（C1.1 失败路径），immutable 头照常（C1.2）。"""
    name, _ = _large_asset()
    app = create_app(manager)
    with TestClient(app) as client:
        # httpx 默认自带 Accept-Encoding: gzip——显式 identity 模拟无 gzip 能力客户端。
        resp = client.get(f"/assets/{name}", headers={"Accept-Encoding": "identity"})
    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_spa_fallback_no_cache_and_not_gzipped(manager):
    """SPA 入口 no-cache（C1.3）；index.html 686B < minimum_size=1024 → 即便客户端
    带 Accept-Encoding 也不压缩（C1.1 尺寸阈值）。"""
    app = create_app(manager)
    with TestClient(app) as client:
        resp = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
    assert "content-encoding" not in resp.headers


def test_api_json_no_custom_cache_control(manager):
    """API JSON 不新增自定义 Cache-Control（C1.4——保持框架默认/缺省）。"""
    app = create_app(manager)
    with TestClient(app) as client:
        resp = client.get("/api/runs")
    assert resp.status_code == 200
    assert "cache-control" not in resp.headers
