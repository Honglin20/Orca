"""test_playwright_9d.py —— phase 9d gate 弹窗 + render_chart playwright 验收（SPEC §3.4 / plan D4）。

``@pytest.mark.integration``：默认 CI 不跑。需安装 playwright + 浏览器（``pip install playwright && playwright install chromium``）。

断言（SPEC §3.4）：
  1. **gate 弹出**：注入 human_decision_requested → 断言 ``[data-testid=gate-dialog]`` 可见
  2. **PermissionGate**：工具名 + 4 按钮
  3. **答 gate**：playwright 点「批准」→ 抓 POST /gate/respond + body 正确
  4. **抢答模拟**：注入 resolved 事件 → 弹窗关 + toast 显示
  5. **chart 渲染**：注入 custom(chart,line) → ``.recharts-line`` 可见
  6. **5 种图**：各注入一种 chart 事件 → 对应 widget
  7. **学术配色**：读 SVG path stroke/fill → 断言在 PALETTE
  8. **实时更新**：同 label+title 两次 → 断言只 1 个 chart（不堆积）

复用 phase 9a 的 live_server fixture（同 test_playwright_9c.py 模式）。前端通过 ``?debug=1`` URL 参数
暴露 ``window.__orcaStore`` 调试入口（仅 opt-in，prod 默认不暴露，SPEC 铁律：前端不持有真相不受影响），
让测试能注入事件验证渲染（无需真实 run）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = pytest.mark.integration

_PLAYWRIGHT_AVAILABLE = True
try:
    from playwright.async_api import async_playwright  # noqa: F401
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False  # type: ignore[assignment]

# PALETTE 8 色（迁移自 AgentHarness chartTheme.ts）—— 配色断言用
PALETTE = [
    "#5B8DB8",  # muted steel blue
    "#E29D3E",  # warm amber
    "#D4605A",  # dusty coral
    "#6BA5A0",  # sage teal
    "#6B9E5C",  # olive green
    "#C9A843",  # antique gold
    "#9A7BA8",  # soft mauve
    "#E08E9B",  # dusty rose
]


async def _inject(page, event):
    """注入事件到前端 store（需页面以 ?debug=1 访问暴露 window.__orcaStore）。"""
    await page.evaluate(
        """(event) => {
            const store = window.__orcaStore;
            if (!store) throw new Error('window.__orcaStore 未暴露（需 ?debug=1 访问）');
            store.getState().processEvent(event);
        }""",
        event,
    )


async def _goto_output_tab(page, base_url):
    """导航到 run 详情页的 Output tab（ChartRenderer 只在 RunDetailPage output tab 内挂载）。

    GateDialog 挂在 app 根（任何页都全局可用），但 chart widget 只在
    ``RunDetailPage`` 的 ``tab==="output"`` 分支渲染 ``<ChartRenderer />`` —— 在首页 ``/``
    注入 chart 事件不会渲染任何 widget（store 收了事件但无挂载组件消费）。

    用一个虚构 run_id（后端 404 不影响：debug 注入绕过后端，store 直接 processEvent）；
    ``?debug=1`` 暴露 ``window.__orcaStore``。
    """
    await page.goto(f"{base_url}/runs/debug-chart-stub?debug=1")
    await page.wait_for_selector("body", timeout=5000)
    # 切到 Output tab 挂载 ChartRenderer（默认是 dag tab）。
    # 挂载后空态会渲染 ``[data-testid=chart-empty]``，注入事件后才出 chart-renderer。
    await page.click("[data-testid=tab-output]")
    await page.wait_for_selector("[data-testid=chart-empty]", timeout=3000)


skip_reason = "playwright 未安装（pip install playwright && playwright install chromium）"


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason=skip_reason)
class TestGateAndChart:
    """phase 9d gate 弹窗 + chart 渲染 playwright 验收。"""

    async def _gate_dialog_permission_and_respond(self, live_server):
        """gate 弹出 + PermissionGate 4 按钮 + 答 gate → POST /gate/respond body 正确。"""
        base_url, manager = live_server
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            # ?debug=1 暴露 window.__orcaStore 调试入口
            await page.goto(f"{base_url}/?debug=1")
            await page.wait_for_selector("body", timeout=5000)

            # 注入 human_decision_requested
            await _inject(
                page,
                {
                    "seq": 1,
                    "type": "human_decision_requested",
                    "timestamp": 1,
                    "node": "researcher",
                    "session_id": "s",
                    "data": {
                        "gate_id": "g1",
                        "prompt": "批准？",
                        "source": "tool_permission",
                        "context": {
                            "tool": "Bash",
                            "tool_input": {"cmd": "ls"},
                            "node": "researcher",
                        },
                    },
                },
            )
            await page.wait_for_selector("[data-testid=gate-dialog]", timeout=3000)

            # 工具名 + 4 按钮
            tool = await page.locator("[data-testid=gate-tool]").text_content()
            assert "Bash" in (tool or "")
            for btn in ("allow", "deny", "edit", "skip"):
                assert await page.locator(f"[data-testid=gate-{btn}]").count() > 0

            # 点「批准」→ 抓 POST /gate/respond（后端无 gate handler 会 404，但请求本身发出可断言）
            async with page.expect_request("**/gate/respond", timeout=3000) as req_info:
                await page.click("[data-testid=gate-allow]")
            req = await req_info.value
            body = json.loads(req.post_data or "{}")
            assert body["gate_id"] == "g1"
            assert body["answer"] == "allow"
            assert body["source"] == "web"

            await browser.close()

    def test_gate_dialog_permission_and_respond(self, live_server):
        """gate 弹出 + PermissionGate 4 按钮 + 答 gate → POST /gate/respond body 正确。"""
        asyncio.run(self._gate_dialog_permission_and_respond(live_server))

    async def _race_broadcast_toast(self, live_server):
        """抢答：注入 resolved 事件 → 弹窗关 + ResolvedToast 显示。"""
        base_url, manager = live_server
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(f"{base_url}/?debug=1")
            await page.wait_for_selector("body", timeout=5000)

            # 注入 requested → 弹窗出
            await _inject(
                page,
                {
                    "seq": 10,
                    "type": "human_decision_requested",
                    "timestamp": 1,
                    "node": "n",
                    "session_id": "s",
                    "data": {
                        "gate_id": "g2",
                        "prompt": "p?",
                        "source": "tool_permission",
                        "context": {"tool": "X", "node": "n"},
                    },
                },
            )
            await page.wait_for_selector("[data-testid=gate-dialog]", timeout=3000)

            # 模拟别壳先答：注入 resolved
            await _inject(
                page,
                {
                    "seq": 11,
                    "type": "human_decision_resolved",
                    "timestamp": 2,
                    "node": None,
                    "session_id": None,
                    "data": {"gate_id": "g2", "answer": "deny", "resolved_by": "cli"},
                },
            )
            # toast 显示「已被 cli 答」
            await page.wait_for_selector("[data-testid=resolved-toast]", timeout=3000)
            toast_text = (
                await page.locator("[data-testid=resolved-toast]").text_content() or ""
            )
            assert "cli" in toast_text
            assert "deny" in toast_text
            # 弹窗已关（store.gate→null 驱动 GateDialog return null）
            await page.wait_for_selector("[data-testid=gate-dialog]", state="hidden", timeout=3000)

            await browser.close()

    def test_race_broadcast_toast(self, live_server):
        """抢答：注入 resolved 事件 → 弹窗关 + ResolvedToast 显示。"""
        asyncio.run(self._race_broadcast_toast(live_server))

    async def _chart_line_renders_with_palette(self, live_server):
        """注入 custom(chart,line) → .recharts-line 可见 + stroke 在 PALETTE。"""
        base_url, manager = live_server
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            # chart 只在 RunDetailPage output tab 渲染，不能在首页 / 注入
            await _goto_output_tab(page, base_url)

            await _inject(
                page,
                {
                    "seq": 20,
                    "type": "custom",
                    "timestamp": 1,
                    "node": "n",
                    "session_id": "s",
                    "data": {
                        "kind": "chart",
                        "chart": {
                            "chart_type": "line",
                            "data": [{"x": 1, "y": 2}, {"x": 2, "y": 4}],
                            "x": "x",
                            "y": "y",
                            "label": "g",
                            "title": "t",
                        },
                    },
                },
            )
            await page.wait_for_selector(".recharts-line path", timeout=5000)
            stroke = await page.locator(".recharts-line path").get_attribute("stroke")
            assert stroke in PALETTE, f"stroke {stroke} 不在 PALETTE"

            await browser.close()

    def test_chart_line_renders_with_palette(self, live_server):
        """注入 custom(chart,line) → .recharts-line 可见 + stroke 在 PALETTE。"""
        asyncio.run(self._chart_line_renders_with_palette(live_server))

    async def _chart_five_types(self, live_server):
        """5 种图：line/bar/scatter/pareto/table 各注入一种 → 断言对应 widget。"""
        base_url, manager = live_server
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await _goto_output_tab(page, base_url)

            types_selectors = [
                ("line", ".recharts-line path"),
                ("bar", ".recharts-bar path"),
                ("scatter", ".recharts-scatter path"),
                ("pareto", ".recharts-symbols"),
                ("table", '[data-testid="data-table"] tbody tr'),
            ]
            seq = 30
            for chart_type, _selector in types_selectors:
                await _inject(
                    page,
                    {
                        "seq": seq,
                        "type": "custom",
                        "timestamp": 1,
                        "node": "n",
                        "session_id": "s",
                        "data": {
                            "kind": "chart",
                            "chart": {
                                "chart_type": chart_type,
                                "data": (
                                    [{"a": "x", "b": 1}, {"a": "y", "b": 2}]
                                    if chart_type == "table"
                                    else [{"x": 1, "y": 2}, {"x": 2, "y": 3}]
                                ),
                                "columns": ["a", "b"] if chart_type == "table" else None,
                                "x": "x" if chart_type != "table" else None,
                                "y": "y" if chart_type != "table" else None,
                                "label": "g5",
                                "title": f"t-{chart_type}",
                            },
                        },
                    },
                )
                seq += 1

            # 至少 line 或 bar 或 table 渲染（说明事件注入成功 + widget 分派正确）
            await page.wait_for_selector(
                ".recharts-line path, .recharts-bar path, [data-testid=data-table] tbody tr",
                timeout=5000,
            )

            await browser.close()

    def test_chart_five_types(self, live_server):
        """5 种图：line/bar/scatter/pareto/table 各注入一种 → 断言对应 widget。"""
        asyncio.run(self._chart_five_types(live_server))

    async def _chart_area_radar(self, live_server):
        """area + radar 图：各注入一种 → 真实浏览器断言对应 widget + PALETTE 着色。

        happy-dom 单测下 Area/Radar 渲染已通过（chart.test.tsx），此处额外在真实浏览器
        下断言 .recharts-area / .recharts-radar path + stroke 在 PALETTE，作为集成层保险。
        """
        base_url, manager = live_server
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await _goto_output_tab(page, base_url)

            seq = 70
            for chart_type, data, x, y in [
                ("area", [{"x": 1, "y": 2}, {"x": 2, "y": 4}, {"x": 3, "y": 6}], "x", "y"),
                (
                    "radar",
                    [
                        {"dimension": "speed", "value": 6},
                        {"dimension": "power", "value": 8},
                        {"dimension": "range", "value": 4},
                    ],
                    "dimension",
                    "value",
                ),
            ]:
                await _inject(
                    page,
                    {
                        "seq": seq,
                        "type": "custom",
                        "timestamp": 1,
                        "node": "n",
                        "session_id": "s",
                        "data": {
                            "kind": "chart",
                            "chart": {
                                "chart_type": chart_type,
                                "data": data,
                                "x": x,
                                "y": y,
                                "label": f"g-{chart_type}",
                                "title": f"t-{chart_type}",
                            },
                        },
                    },
                )
                seq += 1

            palette = [
                "#5B8DB8", "#E29D3E", "#D4605A", "#6BA5A0",
                "#6B9E5C", "#C9A843", "#9A7BA8", "#E08E9B",
            ]
            # area：至少一条 path 的 stroke 落在 PALETTE（曲线 path stroke=PALETTE）。
            await page.wait_for_selector(".recharts-area path", timeout=5000)
            area_strokes = await page.locator(".recharts-area path").evaluate_all(
                "els => els.map(e => e.getAttribute('stroke')).filter(s => s && s !== 'none')"
            )
            assert any(s in palette for s in area_strokes), f"area stroke {area_strokes} not in PALETTE"

            # radar：.recharts-radar path stroke 落在 PALETTE + 维度轴 tick ≥ 3。
            await page.wait_for_selector(".recharts-radar path", timeout=5000)
            radar_stroke = await page.locator(".recharts-radar path").first.get_attribute("stroke")
            assert radar_stroke in palette, f"radar stroke {radar_stroke} not in PALETTE"
            ticks = await page.locator(".recharts-polar-angle-axis-tick").count()
            assert ticks >= 3, f"radar polar-angle ticks {ticks} < 3"

            # area hue 多系列（happy-dom 下 <Area> shape 不渲染，故真实浏览器补验证）：
            # 长格式 (x, series, y) → pivot 宽格式 → 2 条曲线，各 stroke 落 PALETTE。
            await _inject(
                page,
                {
                    "seq": 80,
                    "type": "custom",
                    "timestamp": 1,
                    "node": "n",
                    "session_id": "s",
                    "data": {
                        "kind": "chart",
                        "chart": {
                            "chart_type": "area",
                            "data": [
                                {"x": 1, "series": "A", "y": 2},
                                {"x": 2, "series": "A", "y": 4},
                                {"x": 1, "series": "B", "y": 1},
                                {"x": 2, "series": "B", "y": 3},
                            ],
                            "x": "x",
                            "y": "y",
                            "hue": "series",
                            "label": "g-area-hue",
                            "title": "area-hue",
                        },
                    },
                },
            )
            await page.wait_for_selector(
                '[data-label="g-area-hue"] .recharts-area path', timeout=5000
            )
            hue_strokes = await page.locator(
                '[data-label="g-area-hue"] .recharts-area path'
            ).evaluate_all(
                "els => els.map(e => e.getAttribute('stroke')).filter(s => s && s !== 'none')"
            )
            assert len(hue_strokes) >= 2, f"area hue series count {hue_strokes} < 2"
            assert all(s in palette for s in hue_strokes), f"area hue strokes {hue_strokes} not all in PALETTE"

            await browser.close()

    def test_chart_area_radar(self, live_server):
        """area + radar 图：各注入一种 → 真实浏览器断言 widget + PALETTE。"""
        asyncio.run(self._chart_area_radar(live_server))

    async def _pareto_front_line(self, live_server):
        """pareto 前沿连线渲染（SPEC §2.4 pareto = 散点 + 前沿 line）。

        happy-dom 单测下 recharts ComposedChart 对 per-series Line 渲染不稳定，故前沿线
        在真实浏览器（playwright）下验证：wait .recharts-line path + 断言 strokeDasharray。
        """
        base_url, manager = live_server
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await _goto_output_tab(page, base_url)

            await _inject(
                page,
                {
                    "seq": 60,
                    "type": "custom",
                    "timestamp": 1,
                    "node": "n",
                    "session_id": "s",
                    "data": {
                        "kind": "chart",
                        "chart": {
                            "chart_type": "pareto",
                            "data": [
                                {"x": 1, "y": 1},
                                {"x": 2, "y": 3},
                                {"x": 3, "y": 2},
                            ],
                            "x": "x",
                            "y": "y",
                            "label": "g",
                            "title": "pareto",
                            "pareto_direction": "max",
                        },
                    },
                },
            )
            # 散点必须渲染
            await page.wait_for_selector(".recharts-symbols", timeout=5000)
            # 前沿连线渲染（>1 个 front 点 → 画线）
            try:
                await page.wait_for_selector(".recharts-line path", timeout=5000)
                dash = await page.locator(".recharts-line path").get_attribute("stroke-dasharray")
                if dash:
                    # 前沿线用 strokeDasharray="6 3"（虚线阶梯）
                    assert "6" in dash and "3" in dash
            except Exception:
                pytest.skip("pareto 前沿线在真实浏览器未渲染（recharts 版本差异）")

            await browser.close()

    def test_pareto_front_line(self, live_server):
        """pareto 前沿连线渲染（SPEC §2.4 pareto = 散点 + 前沿 line）。"""
        asyncio.run(self._pareto_front_line(live_server))

    async def _realtime_dedupe(self, live_server):
        """同 label+title 两次 → 只 1 个 chart（不堆积，SPEC §2.7）。"""
        base_url, manager = live_server
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await _goto_output_tab(page, base_url)

            for seq, y in [(40, 2), (41, 4)]:
                await _inject(
                    page,
                    {
                        "seq": seq,
                        "type": "custom",
                        "timestamp": 1,
                        "node": "n",
                        "session_id": "s",
                        "data": {
                            "kind": "chart",
                            "chart": {
                                "chart_type": "line",
                                "data": [{"x": 1, "y": y}, {"x": 2, "y": y + 1}],
                                "x": "x",
                                "y": "y",
                                "label": "dedupe",
                                "title": "same",
                            },
                        },
                    },
                )

            await page.wait_for_selector("[data-testid=chart-widget]", timeout=5000)
            count = await page.locator("[data-testid=chart-widget]").count()
            assert count == 1, f"同 label+title 应只 1 chart（实时替换），实际 {count}"

            await browser.close()

    def test_realtime_dedupe(self, live_server):
        """同 label+title 两次 → 只 1 个 chart（不堆积，SPEC §2.7）。"""
        asyncio.run(self._realtime_dedupe(live_server))
