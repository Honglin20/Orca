"""test_playwright.py —— playwright 端到端 API/WS 断言（SPEC §6.8 / 计划 A4.4）。

``@pytest.mark.integration``：默认 CI 不跑。需安装 playwright + 浏览器。

phase 9a 无 UI，但用 playwright 的 ``page.request``（fetch）+ ``page.evaluate``（WS 客户端）
测后端 API/WS。phase 9b 前端就位后，playwright 主要验 UI。
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import closing
from pathlib import Path

import pytest

from orca.iface.web.run_manager import RunManager
from orca.iface.web.server import create_app

pytestmark = pytest.mark.integration

_PLAYWRIGHT_AVAILABLE = True
try:
    from playwright.async_api import async_playwright  # noqa: F401
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _demo_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "demo.yaml"
    p.write_text(
        """
name: demo
entry: a
nodes:
  - name: a
    kind: script
    command: "echo hi"
    routes:
      - to: $end
""",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def live_server(tmp_path):
    """启动真 uvicorn server，yield (base_url, manager)。teardown 关 server。"""
    import uvicorn

    manager = RunManager(runs_dir=tmp_path / "runs")
    app = create_app(manager)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    loop = asyncio.new_event_loop()

    def run():
        loop.run_until_complete(server.serve())

    import threading
    t = threading.Thread(target=run, daemon=True)
    t.start()
    loop.run_until_complete(asyncio.sleep(0.3))
    base_url = f"http://127.0.0.1:{port}"
    yield base_url, manager, loop
    loop.call_soon_threadsafe(server.should_exit.set, True) if False else None
    # graceful：通过 should_exit
    server.should_exit = True
    t.join(timeout=5.0)
    loop.run_until_complete(manager.shutdown())
    loop.close()


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_playwright_runs_api(live_server, tmp_path):
    """playwright fetch('/api/runs') 返回非空元数据列表（懒加载断言）。SPEC §6.8。"""
    base_url, manager, loop = live_server
    yaml_path = _demo_yaml(tmp_path)

    async def go():
        await manager.start_run(str(yaml_path), {}, None, None)
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(base_url)
            resp = await page.request.fetch(f"{base_url}/api/runs")
            assert resp.status == 200
            data = await resp.json()
            assert isinstance(data, list)
            assert len(data) >= 1
            for item in data:
                assert "events" not in item  # 懒加载红线
            await browser.close()

    asyncio.run(go())


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_playwright_events_api(live_server, tmp_path):
    """playwright fetch('/api/runs/<id>/events') 返回事件数组。SPEC §6.8。"""
    base_url, manager, loop = live_server
    yaml_path = _demo_yaml(tmp_path)

    async def go():
        rid = await manager.start_run(str(yaml_path), {}, None, None)
        await manager.wait_done(rid, timeout=10.0)
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            resp = await page.request.fetch(f"{base_url}/api/runs/{rid}/events")
            assert resp.status == 200
            events = await resp.json()
            assert len(events) > 0
            await browser.close()

    asyncio.run(go())


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_playwright_ws_subscribe(live_server, tmp_path):
    """playwright evaluate WS 客户端 → subscribe + 收到事件（带 run_id）。SPEC §6.8。"""
    base_url, manager, loop = live_server
    yaml_path = _demo_yaml(tmp_path)

    async def go():
        rid = await manager.start_run(str(yaml_path), {}, None, None)
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            ws_url = base_url.replace("http://", "ws://") + "/ws"
            # 注入 WS 客户端 + subscribe + 抓第一条事件
            received = await page.evaluate(
                """
                async ({ws_url, run_id}) => {
                    const ws = new WebSocket(ws_url);
                    await new Promise(r => ws.onopen = r);
                    ws.send(JSON.stringify({type: "subscribe", run_id: run_id}));
                    const msg = await new Promise(resolve => {
                        ws.onmessage = (e) => resolve(JSON.parse(e.data));
                        setTimeout(() => resolve(null), 5000);
                    });
                    ws.close();
                    return msg;
                }
                """,
                {"ws_url": ws_url, "run_id": rid},
            )
            assert received is not None, "5s 内未收到 WS 事件"
            assert received.get("run_id") == rid
            await browser.close()

    asyncio.run(go())
