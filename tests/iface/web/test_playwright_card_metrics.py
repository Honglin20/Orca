"""test_playwright_card_metrics.py —— 卡片 event_count（log 行数）+ chart_count 真机 DOM 验证。

SPEC `docs/specs/2026-08-10-card-event-log-align.md`：主页卡片 event_count 应 == log 行数
（node 级生命周期事件，排除 tool_call/message），chart_count == 去重后图表数。本测试用真
uvicorn server + Playwright 浏览器驱动前端 SPA，断言 BoardCard / RunRow DOM 渲染正确数值。

tape 刻意含（覆盖 F1 双分支 + 排除项 + F4 去重 edge case）：
  - retry_started/succeeded（走 `_META_BULK_MARKERS` fast-path + 在 log 白名单 → 验双分支计数）
  - agent_tool_call ×5 + agent_message（排除项，不计入）
  - custom chart ×2 同 title（去重→1）+ ×1 无 title（→1）→ chart_count=2

log 事件计数：workflow_started + node_started + retry_started + retry_succeeded + node_completed +
workflow_completed = 6（tool_call/message 不算）。这是闭环之前"前端浏览器端"环境限制遗留的真机验证。
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
    """构造最小注册项目（workflows/ 子目录满足注册表 marker 约定）。"""
    p = parent / name
    (p / "workflows").mkdir(parents=True, exist_ok=True)
    return p


def _ev(seq: int, t: str, node: str | None = None, data: dict | None = None) -> str:
    return json.dumps({
        "seq": seq, "type": t, "node": node, "session_id": None,
        "timestamp": time.time(), "data": data or {},
    })


def _chart_ev(seq: int, title: str, chart_type: str = "line", node: str = "a") -> str:
    return json.dumps({
        "seq": seq, "type": "custom", "node": node, "session_id": None,
        "timestamp": time.time(),
        "data": {"kind": "chart", "chart": {
            "title": title, "chart_type": chart_type, "label": "g1",
        }},
    })


def _write_rich_tape(tape_path: Path, run_id: str) -> None:
    """写含 retry（fast-path 白名单）+ tool_call（排除）+ chart（去重）的 tape。"""
    tape_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        _ev(1, "workflow_started", data={
            "inputs": {}, "node_count": 1, "entry": "a",
            "workflow_name": "rich_wf", "run_id": run_id,
            "topology": {"entry": "a", "nodes": [{"name": "a", "kind": "script"}]},
        }),
        _ev(2, "node_started", "a"),
        # retry 走 fast-path bulk marker + 在 log 白名单（验双分支计数）
        _ev(3, "retry_started", "a"),
        _ev(4, "retry_succeeded", "a"),
        # 工具调用 / 消息：不进 log（排除项，不计入 event_count）
        *[_ev(s, "agent_tool_call", "a", {"tool": "bash"}) for s in (5, 6, 7, 8, 9)],
        _ev(10, "agent_message", "a", {"text": "hi"}),
        # chart：2 同 title（去重→1）+ 1 无 title（identity=line#seq → 1）→ chart_count=2
        _chart_ev(11, "Loss Curve"),
        _chart_ev(12, "Loss Curve"),  # 同 title → 去重
        _chart_ev(13, ""),  # 无 title → 独立 identity
        _ev(14, "node_completed", "a", {"output": {}}),
        _ev(15, "workflow_completed", data={"elapsed": 4.0, "outputs": {}}),
    ]
    tape_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_rich_run(tmp_path: Path, manager) -> str:
    from orca.runtime import register_project

    proj = _make_project(tmp_path, "demo")
    register_project(proj)
    rid = "rich-run-01"
    _write_rich_tape(proj / "runs" / f"{rid}.jsonl", rid)
    manager.discover_runs()  # 让 manager 把 tape 装进 _run_path_index
    return rid


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_board_card_metrics(live_server, tmp_path):
    """BoardCard 第三行 DOM 文本含 "6 事件"（log 行数）+ "2 图表"（去重）。"""
    base_url, manager = live_server
    _seed_rich_run(tmp_path, manager)

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(base_url)
            await page.wait_for_selector("[data-testid=board-card]", timeout=5000)
            text = await page.locator("[data-testid=board-card]").first.text_content()
            assert "6 事件" in text, (
                f"event_count 应为 6（log 行数：retry fast-path 计入 + tool_call/message 排除），"
                f"实际 BoardCard 文本：{text!r}"
            )
            # chart_count：BoardCard 用图标 span（title="图表数（去重后）"，无文字后缀），
            # 与 event_count "事件" 文字故意不对称（图标更紧凑）——定位图标 span 验值 == "2"。
            chart_text = await page.locator(
                '[title="图表数（去重后）"]'
            ).first.text_content()
            assert chart_text.strip() == "2", (
                f"chart_count 应为 2（同 title 去重 + 无 title），实际：{chart_text!r}"
            )
            await browser.close()

    asyncio.run(go())


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_run_row_metrics_list_view(live_server, tmp_path):
    """列表视图 RunRow metrics：[title=事件数]==6 + [title=图表]==2（viewport 宽让 md:flex 显示）。"""
    base_url, manager = live_server
    _seed_rich_run(tmp_path, manager)

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.goto(base_url)
            # 默认 board → 切 list
            await page.click("[data-testid=view-toggle-list]")
            await page.wait_for_selector("[data-testid=run-row]", timeout=5000)
            ec = await page.locator('[title="事件数"] .tabular-nums').first.text_content()
            assert ec.strip() == "6", f"RunRow event_count metric 应为 6，实际：{ec!r}"
            cc = await page.locator('[title="图表"] .tabular-nums').first.text_content()
            assert cc.strip() == "2", f"RunRow chart_count metric 应为 2，实际：{cc!r}"
            await browser.close()

    asyncio.run(go())
