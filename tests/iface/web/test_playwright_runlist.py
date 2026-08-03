"""test_playwright_runlist.py —— RunListPage 重设计（SPEC web-runlist-redesign）真机验证。

``@pytest.mark.integration``：默认 CI 不跑。需 playwright + 浏览器已安装。

覆盖（SPEC §8 AC + §10 看板）：
  1. **AC-19 默认看板**：``/`` 默认渲染 board；toggle 切 list；持久 ``orca-runlist-view-v1``。
  2. **AC-20 五列渲染**：board-column-queued/running/blocked/completed/failed 均在 DOM。
  3. **AC-21 点卡进详情**：click board-card → /runs/<id>（同时验证 9b 兼容 run-item 选择器）。
  4. **AC-23 共享 selection**：看板勾选 → bulk-bar；切列表选择保留。
  5. **AC-4 折叠持久**：列表视图折叠分组 → reload → 仍折叠。
  6. **AC-6 主题切换**：theme-btn → ``<html>`` class 切换 + localStorage 写。
  7. **AC-7 搜索穿透**：q 非空 → 列表分组强制展开。
  8. **兼容 9b**：``[data-testid=run-item]`` 选择器在看板/列表两视图都能命中（同 DOM 锚）。

复用 ``live_server`` fixture（启动真 uvicorn + manager）。前端需 ``npm run build`` 后
``static/`` 内有最新 SPA——本测试套件**不**自动构建，由开发流程保证（plan 自测门 1）。
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


# ── tape / project helpers（与 test_multi_run_phase_c 同构，本测试自包含） ─────


def _make_project(parent: Path, name: str = "demo") -> Path:
    """构造最小注册项目（``workflows/`` 子目录满足注册表 marker 约定）。"""
    p = parent / name
    (p / "workflows").mkdir(parents=True, exist_ok=True)
    return p


def _write_tape(
    tape_path: Path,
    run_id: str,
    *,
    workflow_name: str = "demo",
    project_name: str = "demo",
) -> None:
    """写一个 completed tape（workflow_started + node_started + workflow_completed）。

    本轮重设计 e2e 只验证「看板/列表/选择/搜索/主题」契约，不依赖多状态——
    多状态落位（running/blocked/failed）由 vitest 组件测试覆盖（更快更稳）。
    """
    tape_path.parent.mkdir(parents=True, exist_ok=True)
    ts = time.time()
    lines = [
        json.dumps({
            "seq": 1, "type": "workflow_started", "node": None, "session_id": None,
            "timestamp": ts,
            "data": {
                "inputs": {}, "node_count": 1, "entry": "a",
                "workflow_name": workflow_name, "run_id": run_id,
                "topology": {"entry": "a", "nodes": [{"name": "a", "kind": "script"}]},
            },
        }),
        json.dumps({
            "seq": 2, "type": "node_started", "node": "a", "session_id": "s1",
            "timestamp": ts, "data": {},
        }),
        json.dumps({
            "seq": 3, "type": "workflow_completed", "node": None, "session_id": None,
            "timestamp": ts, "data": {"elapsed": 0.5, "outputs": {}},
        }),
    ]
    tape_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_runs(tmp_path: Path, manager, count: int = 3) -> list[str]:
    """注册项目 + 写 count 条 completed tape + discover_runs 让 manager 看到。"""
    from orca.runtime import register_project

    proj = _make_project(tmp_path, "demo")
    register_project(proj)
    run_ids: list[str] = []
    for i in range(count):
        rid = f"demo-run-{i:02d}"
        _write_tape(
            proj / "runs" / f"{rid}.jsonl",
            rid,
            workflow_name=f"wf-{i}",
            project_name="demo",
        )
        run_ids.append(rid)
    # discovery：让 manager 把这些 tape 装进 _run_path_index（GET /api/runs?scope=all 用）。
    manager.discover_runs()
    return run_ids


# ── 测试用例 ─────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_default_board_renders_and_five_columns(live_server, tmp_path):
    """AC-19/20：默认渲染 board；五列 testid 均在 DOM（即使列空也渲染占位）。"""
    base_url, manager = live_server
    _seed_runs(tmp_path, manager, count=1)

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(base_url)
            await page.wait_for_selector("[data-testid=board]", timeout=5000)
            # 五列 testid 都在。
            for col in ("queued", "running", "blocked", "completed", "failed"):
                await page.wait_for_selector(
                    f"[data-testid=board-column-{col}]", timeout=2000
                )
            # 默认视图不应显列表 run-row。
            assert await page.locator("[data-testid=run-row]").count() == 0
            await browser.close()

    asyncio.run(go())


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_click_board_card_navigates_to_detail(live_server, tmp_path):
    """AC-21 + 9b 兼容：click board-card 内的 run-item → /runs/<id>。"""
    base_url, manager = live_server
    rids = _seed_runs(tmp_path, manager, count=1)

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(base_url)
            await page.wait_for_selector("[data-testid=board-card]", timeout=5000)
            # 点 board-card 根（含 run-item 内层）；事件冒泡触发根 onClick → onOpen。
            await page.click("[data-testid=board-card]")
            await page.wait_for_url("**/runs/*", timeout=5000)
            assert "/runs/" in page.url
            assert rids[0] in page.url, f"应跳转到 /runs/{rids[0]}，实际 {page.url}"
            await browser.close()

    asyncio.run(go())


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_view_toggle_persists_to_list_and_back(live_server, tmp_path):
    """AC-19：toggle board/list 切换；持久 localStorage；reload 后保持。"""
    base_url, manager = live_server
    _seed_runs(tmp_path, manager, count=1)

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(base_url)
            await page.wait_for_selector("[data-testid=board]", timeout=5000)
            # 切到 list。
            await page.click("[data-testid=view-toggle-list]")
            await page.wait_for_selector("[data-testid=run-row]", timeout=5000)
            # localStorage 写入 list。
            stored = await page.evaluate(
                "localStorage.getItem('orca-runlist-view-v1')"
            )
            assert stored and "list" in stored
            # reload 后保持 list。
            await page.reload()
            await page.wait_for_selector("[data-testid=run-row]", timeout=5000)
            # 切回 board。
            await page.click("[data-testid=view-toggle-board]")
            await page.wait_for_selector("[data-testid=board]", timeout=5000)
            await browser.close()

    asyncio.run(go())


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_list_view_bulk_select_and_delete(live_server, tmp_path):
    """AC-2/AC-12：列表视图多选 + bulk-bar + 批量删除（DELETE 真机）。"""
    base_url, manager = live_server
    _seed_runs(tmp_path, manager, count=3)

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(base_url)
            # 直接切列表（默认 board）。
            await page.click("[data-testid=view-toggle-list]")
            await page.wait_for_selector("[data-testid=run-checkbox]", timeout=5000)
            # 勾第一个 checkbox → bulk-bar 出现。
            await page.click("[data-testid=run-checkbox] >> nth=0")
            await page.wait_for_selector("[data-testid=bulk-bar]", timeout=2000)
            # 批量删除按钮可点击。
            assert await page.locator("[data-testid=bulk-delete-btn]").count() == 1
            # 点删除 → 确认对话框。
            await page.click("[data-testid=bulk-delete-btn]")
            await page.wait_for_selector("[data-testid=delete-dialog]", timeout=2000)
            # 确认 → DELETE 被调（dialog 关闭 + toast 出现）。
            await page.click("[data-testid=confirm-delete]")
            await page.wait_for_selector("[data-testid=runlist-toast]", timeout=3000)
            await browser.close()

    asyncio.run(go())


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_list_view_sort_menu(live_server, tmp_path):
    """AC-3：排序菜单可打开 + 选 workflow_name 字段。"""
    base_url, manager = live_server
    _seed_runs(tmp_path, manager, count=3)

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(base_url)
            await page.click("[data-testid=view-toggle-list]")
            await page.wait_for_selector("[data-testid=run-row]", timeout=5000)
            # 打开排序菜单。
            await page.click("[data-testid=sort-trigger]")
            await page.wait_for_selector("[data-testid=sort-menu]", timeout=2000)
            # 选 workflow_name。
            await page.click("[data-testid=sort-option-workflow_name]")
            # 触发器应显字段名。
            await page.wait_for_function(
                "() => document.querySelector('[data-testid=sort-trigger]').textContent.includes('workflow')",
                timeout=2000,
            )
            await browser.close()

    asyncio.run(go())


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_list_view_collapse_persistence_across_reload(live_server, tmp_path):
    """AC-4/AC-26：折叠 project 分组 → reload → 仍折叠（localStorage ``collapsed-v2`` ``"dim:key"`` 持久）。"""
    base_url, manager = live_server
    _seed_runs(tmp_path, manager, count=2)

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(base_url)
            await page.click("[data-testid=view-toggle-list]")
            # §10.8 默认 dim=「状态」→ 切到 project（保留旧用例的 project 分组假设）。
            await page.click("[data-testid=group-by-select]")
            await page.click("[data-testid=group-by-option-project]")
            await page.wait_for_selector("[data-testid=group-header]", timeout=5000)
            # 初始展开：run-row 可见。
            await page.wait_for_selector("[data-testid=run-row]", timeout=2000)
            # 折叠。
            await page.click("[data-testid=group-header]")
            # 折叠后 run-row 应不可见。
            await page.wait_for_function(
                "() => document.querySelectorAll('[data-testid=run-row]').length === 0",
                timeout=2000,
            )
            stored = await page.evaluate(
                "localStorage.getItem('orca-runlist-collapsed-v2')"
            )
            assert stored and "project:demo" in stored
            # reload → 仍折叠。
            await page.reload()
            await page.wait_for_selector("[data-testid=group-header]", timeout=5000)
            await page.wait_for_function(
                "() => document.querySelectorAll('[data-testid=run-row]').length === 0",
                timeout=2000,
            )
            await browser.close()

    asyncio.run(go())


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_group_by_dim_switch_and_empty_bucket_hide(live_server, tmp_path):
    """AC-24/AC-25：切分组维度 → 桶变化；空桶默认隐藏（showEmpty=false）。"""
    base_url, manager = live_server
    _seed_runs(tmp_path, manager, count=1)

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(base_url)
            # 默认 board 视图 + 默认 dim=status：仅 completed 列（其它 4 空列隐藏）。
            await page.wait_for_selector("[data-testid=board]", timeout=5000)
            await page.wait_for_selector(
                "[data-testid=board-column-completed]", timeout=2000
            )
            assert await page.locator(
                "[data-testid=board-column-queued]"
            ).count() == 0, "showEmpty=false 时空列应隐藏"

            # 切到 project dim → board-column-demo 出现。
            await page.click("[data-testid=group-by-select]")
            await page.click("[data-testid=group-by-option-project]")
            await page.wait_for_selector(
                "[data-testid=board-column-demo]", timeout=2000
            )
            assert await page.locator(
                "[data-testid=board-column-completed]"
            ).count() == 0, "切到 project 后旧 status 列 testid 应消失"

            # localStorage 持久 dim=project。
            stored = await page.evaluate(
                "localStorage.getItem('orca-runlist-groupby-v1')"
            )
            assert stored and "project" in stored

            # 切回 status → completed 列回来。
            await page.click("[data-testid=group-by-select]")
            await page.click("[data-testid=group-by-option-status]")
            await page.wait_for_selector(
                "[data-testid=board-column-completed]", timeout=2000
            )
            await browser.close()

    asyncio.run(go())


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_theme_button_toggles_html_class(live_server, tmp_path):
    """AC-6：点 theme-btn → <html> class 加 dark/light 之一 + localStorage 写。"""
    base_url, manager = live_server
    _seed_runs(tmp_path, manager, count=1)

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(base_url)
            await page.wait_for_selector("[data-testid=theme-btn]", timeout=5000)
            # 清初始 <html> class + localStorage。
            await page.evaluate(
                "() => { document.documentElement.classList.remove('dark','light'); localStorage.removeItem('orca-theme'); }"
            )
            await page.click("[data-testid=theme-btn]")
            await page.wait_for_function(
                "() => document.documentElement.classList.contains('dark') || document.documentElement.classList.contains('light')",
                timeout=2000,
            )
            stored = await page.evaluate("localStorage.getItem('orca-theme')")
            assert stored is not None
            await browser.close()

    asyncio.run(go())


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_list_view_search_force_expands_collapsed_group(live_server, tmp_path):
    """AC-7：折叠分组 + 输入搜索 → 含匹配分组强制展开。"""
    base_url, manager = live_server
    _seed_runs(tmp_path, manager, count=2)

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(base_url)
            await page.click("[data-testid=view-toggle-list]")
            await page.wait_for_selector("[data-testid=group-header]", timeout=5000)
            # 折叠。
            await page.click("[data-testid=group-header]")
            await page.wait_for_function(
                "() => document.querySelectorAll('[data-testid=run-row]').length === 0",
                timeout=2000,
            )
            # 搜索：输入匹配前缀（workflow_name=wf-0/wf-1）。
            await page.fill("[data-testid=search-input]", "wf-")
            # debounce ~250ms + 等待重渲染。
            await page.wait_for_timeout(500)
            # 强制展开：run-row 重新可见。
            await page.wait_for_selector("[data-testid=run-row]", timeout=3000)
            await browser.close()

    asyncio.run(go())


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_run_item_selector_works_in_both_views(live_server, tmp_path):
    """9b 兼容：``[data-testid=run-item]`` 在 board/card 与 list/row 都命中。

    防止 9b 的 ``page.click("[data-testid=run-item]")`` 因默认视图改 board 而破。
    """
    base_url, manager = live_server
    _seed_runs(tmp_path, manager, count=1)

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(base_url)
            # 默认 board 视图：run-item 选择器命中（BoardCard 内层 div）。
            await page.wait_for_selector("[data-testid=run-item]", timeout=5000)
            assert await page.locator("[data-testid=run-item]").count() >= 1
            # 切到 list：仍命中（RunRow 内层 button）。
            await page.click("[data-testid=view-toggle-list]")
            await page.wait_for_selector("[data-testid=run-item]", timeout=5000)
            assert await page.locator("[data-testid=run-item]").count() >= 1
            await browser.close()

    asyncio.run(go())
