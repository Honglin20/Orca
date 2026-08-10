"""test_back_navigation.py —— 详情页 TopBar ← 返回主页真机验证。

bug：TopBar 返回按钮原用 ``window.location.href = "/"``（整页刷新，慢 + 丢 store 状态，
体感"没法后退"）。修复：react-router ``useNavigate`` → ``navigate("/")``（SPA 内导航，
快，保留 store）。本测试真机断言：进详情页 → 点 ← → URL 回 ``/`` + 主页 board-card 重渲染。

注 1：tape topology 含 ``routes: []``——详情页 ``AgentsRail`` 的 ``selectAgentGroups``
迭代 ``def.routes``（``selectors.ts:171``），缺失则 ``routes is not iterable`` 崩。
真实 run 的 topology 有 routes（workflow 定义），fixture 漏写会白屏。
注 2：点击用 ``eval_on_selector``——详情页 elapsed tick 高频重渲染致 button DOM 短暂
detach，Playwright strict click 反复 retry 抓不到；JS click 一次性触发 onClick，真人
点击同理不受 tick 影响。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_PLAYWRIGHT_AVAILABLE = True
try:
    from playwright.async_api import async_playwright  # noqa: F401
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False


def _make_project(parent: Path, name: str = "demo") -> Path:
    p = parent / name
    (p / "workflows").mkdir(parents=True, exist_ok=True)
    return p


def _write_tape(tape_path: Path, run_id: str, workflow_name: str = "demo") -> None:
    tape_path.parent.mkdir(parents=True, exist_ok=True)
    ts = time.time()
    lines = [
        json.dumps({"seq": 1, "type": "workflow_started", "node": None, "session_id": None,
            "timestamp": ts, "data": {"inputs": {}, "node_count": 1, "entry": "a",
            "workflow_name": workflow_name, "run_id": run_id,
            # topology 必含 routes（AgentsRail selectAgentGroups 迭代 def.routes）
            "topology": {"entry": "a", "nodes": [{"name": "a", "kind": "script"}], "routes": []}}}),
        json.dumps({"seq": 2, "type": "node_started", "node": "a", "session_id": "s1",
            "timestamp": ts, "data": {}}),
        json.dumps({"seq": 3, "type": "node_completed", "node": "a", "session_id": "s1",
            "timestamp": ts, "data": {"output": {}}}),
        json.dumps({"seq": 4, "type": "workflow_completed", "node": None, "session_id": None,
            "timestamp": ts, "data": {"elapsed": 0.5, "outputs": {}}}),
    ]
    tape_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed(tmp_path: Path, manager, count: int = 1) -> list[str]:
    from orca.runtime import register_project

    proj = _make_project(tmp_path, "demo")
    register_project(proj)
    rids = []
    for i in range(count):
        rid = f"demo-run-{i:02d}"
        _write_tape(proj / "runs" / f"{rid}.jsonl", rid)
        rids.append(rid)
    manager.discover_runs()
    return rids


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_back_button_navigates_to_list(live_server, tmp_path):
    """详情页 TopBar ← 按钮 → 回主页 /（SPA 内 navigate，不整页刷新）。"""
    base_url, manager = live_server
    _seed(tmp_path, manager, 1)

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(base_url)
            await page.wait_for_selector("[data-testid=board-card]", timeout=5000)
            await page.click("[data-testid=board-card]")
            await page.wait_for_url("**/runs/*", timeout=5000)
            # 详情页 TopBar 渲染 + 加载稳定（events 流入初期重渲染）
            await page.wait_for_selector("[data-testid=top-bar]", timeout=5000)
            await page.wait_for_timeout(500)
            # eval_on_selector JS click（绕过 strict click 的 detach 抖动，等价真人点击）
            await page.eval_on_selector(
                "[data-testid=top-bar] button[aria-label='返回 run 列表']",
                "el => el.click()",
            )
            # SPA navigate → URL 回 / + 主页 board-card 重新渲染（无整页刷新）
            await page.wait_for_url("**/", timeout=8000)
            await page.wait_for_selector("[data-testid=board-card]", timeout=8000)
            assert page.url.rstrip("/").endswith(""), f"URL 应为 /，实际 {page.url}"
            await browser.close()

    asyncio.run(go())
