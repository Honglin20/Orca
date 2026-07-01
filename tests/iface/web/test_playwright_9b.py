"""test_playwright_9b.py —— phase 9b 前端骨架 playwright 验收（SPEC §7.6 / plan B5）。

``@pytest.mark.integration``：默认 CI 不跑。需安装 playwright + 浏览器。

断言四条 UI 铁律：
  1. **后退语义**（铁律 3）：导航 A → B → goBack → 回 A（不是主页）
  2. **懒加载**（铁律 1）：首页**不**调 /events；点 run 才调
  3. **URL 直接访问**（铁律 3）：/runs/<id> 可直接打开
  4. **新 run 表单**：填表 → 提交 → 跳转详情页

复用 phase 9a 的 live_server fixture（启动真 uvicorn + manager）。
"""

from __future__ import annotations

import asyncio
import socket
import threading
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
    """启动真 uvicorn server（同 test_playwright.py 模式），yield (base_url, manager)。"""
    import uvicorn

    manager = RunManager(runs_dir=tmp_path / "runs")
    app = create_app(manager)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    loop = asyncio.new_event_loop()

    def run():
        loop.run_until_complete(server.serve())

    t = threading.Thread(target=run, daemon=True)
    t.start()
    # loop 在 daemon 线程跑 server.serve()，主线程不能对它 run_until_complete（已 running）；
    # 改轮询端口等 server accept 就绪。
    import time
    _deadline = time.time() + 5.0
    while time.time() < _deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    base_url = f"http://127.0.0.1:{port}"
    yield base_url, manager
    server.should_exit = True
    t.join(timeout=5.0)
    loop.run_until_complete(manager.shutdown())
    loop.close()


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_back_button_semantics(live_server, tmp_path):
    """后退 = 浏览器原生后退（铁律 3）：A → B → goBack → 回 A。SPEC §7.1。"""
    base_url, manager = live_server
    yaml_path = _demo_yaml(tmp_path)

    async def go():
        rid_a = await manager.start_run(str(yaml_path), {}, None, None)
        rid_b = await manager.start_run(str(yaml_path), {}, None, None)
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(f"{base_url}/runs/{rid_a}")
            await page.goto(f"{base_url}/runs/{rid_b}")
            await page.go_back()
            assert rid_a in page.url  # 后退回 A，不是主页
            assert rid_b not in page.url
            await browser.close()

    asyncio.run(go())


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_back_to_list_not_blank(live_server, tmp_path):
    """从列表进 A，后退 → 回列表（不是空白主页，反 AgentHarness）。SPEC §7.1。"""
    base_url, manager = live_server
    yaml_path = _demo_yaml(tmp_path)

    async def go():
        rid = await manager.start_run(str(yaml_path), {}, None, None)
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(base_url)  # 列表（URL = base_url + "/"）
            await page.goto(f"{base_url}/runs/{rid}")  # 进详情
            await page.go_back()
            # 后退回列表：path 为 "/"（非空白、非详情页）
            assert page.url == f"{base_url}/" or page.url == base_url
            assert "/runs/" not in page.url
            await browser.close()

    asyncio.run(go())


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_lazy_loading_home_no_events(live_server, tmp_path):
    """懒加载（铁律 1）：首页加载**不**调 /events（抓网络请求断言）。SPEC §7.2。"""
    base_url, manager = live_server
    yaml_path = _demo_yaml(tmp_path)

    async def go():
        await manager.start_run(str(yaml_path), {}, None, None)
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            requests: list[str] = []
            page.on("request", lambda r: requests.append(r.url))
            await page.goto(base_url)  # 首页
            await page.wait_for_timeout(500)
            # 首页不应调 /events（懒加载红线）
            assert not any("/events" in r for r in requests), (
                f"首页不应拉 /events，但抓到：{[r for r in requests if '/events' in r]}"
            )
            # 但 /api/runs（元数据）应被调
            assert any("/api/runs" in r and "/events" not in r for r in requests)
            await browser.close()

    asyncio.run(go())


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_lazy_loading_click_loads_events(live_server, tmp_path):
    """懒加载：点开 run 才调 /api/runs/<id>/events。SPEC §7.2。"""
    base_url, manager = live_server
    yaml_path = _demo_yaml(tmp_path)

    async def go():
        rid = await manager.start_run(str(yaml_path), {}, None, None)
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            requests: list[str] = []
            page.on("request", lambda r: requests.append(r.url))
            await page.goto(base_url)
            await page.wait_for_timeout(300)
            before = sum(1 for r in requests if "/events" in r)
            # 点击第一个 run-item
            await page.click("[data-testid=run-item]")
            await page.wait_for_timeout(800)
            after = sum(1 for r in requests if "/events" in r)
            assert after > before, "点开 run 应触发 /events 拉取"
            await browser.close()

    asyncio.run(go())


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_direct_url_access(live_server, tmp_path):
    """URL 可直接访问（铁律 3）：/runs/<id> 直接打开。SPEC §7.1。"""
    base_url, manager = live_server
    yaml_path = _demo_yaml(tmp_path)

    async def go():
        rid = await manager.start_run(str(yaml_path), {}, None, None)
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(f"{base_url}/runs/{rid}")
            # 详情页应渲染（含 run id 片段）
            await page.wait_for_timeout(500)
            content = await page.content()
            assert rid[:8] in content, "详情页应显示 run id"
            await browser.close()

    asyncio.run(go())


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_new_run_form(live_server, tmp_path):
    """新 run 表单：填 yaml_path → 提交 → 跳转到 /runs/<new_id>。SPEC §7.1 / plan B5。"""
    base_url, _manager = live_server
    yaml_path = _demo_yaml(tmp_path)

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(f"{base_url}/runs/new")
            await page.fill("input[placeholder='workflows/demo.yaml']", str(yaml_path))
            await page.click("button[type=submit]")
            # run_id 形如 ``demo-20260701-075614-7f6455``（slug-ts-nanoid），不是 run-*。
            # demo.yaml 的 name=demo → slug=demo；等 URL 跳出 /runs/new 即表单已提交 + 导航。
            await page.wait_for_url("**/runs/demo-*-*", timeout=5000)
            assert "/runs/new" not in page.url, "表单提交后应离开 /runs/new"
            assert "/runs/demo-" in page.url, f"应跳转到 /runs/demo-<ts>-<nanoid>，实际 {page.url}"
            await browser.close()

    asyncio.run(go())
