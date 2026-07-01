"""test_playwright.py —— playwright 端到端 API/WS 断言（SPEC §6.8 / 计划 A4.4）。

``@pytest.mark.integration``：默认 CI 不跑。需安装 playwright + 浏览器。

phase 9a 无 UI，但用 playwright 的 ``page.request``（fetch）+ ``page.evaluate``（WS 客户端）
测后端 API/WS。phase 9b 前端就位后，playwright 主要验 UI。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_PLAYWRIGHT_AVAILABLE = True
try:
    from playwright.async_api import async_playwright  # noqa: F401
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False


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


def _slow_yaml(tmp_path: Path) -> Path:
    """慢 workflow（WS 实测用）：sleep 5 的 script node，保证订阅时 run 仍在跑。

    echo hi 在 ms 级完成，run 终态后 bus.close（teardown）—— 此时再 subscribe 拿不到
    任何 live 事件（race lost）。改用 sleep 5 让 run 在订阅窗口内仍 running，pump task
    能真正把 ``node_started``/``node_completed`` 推给 WS（SPEC §4 铁律：pump 转发 bus
    订阅事件，这是唯一验证 WS 真推送的测试）。

    时序：run-t≈0 emit workflow_started/node_started（subscribe 完成在 ~0.5s，可能错过），
    run-t≈5 emit node_completed/workflow_completed —— 必须落入收集窗口。窗口设 6s 给
    ≥1s 余量（CI 慢机/浏览器冷启 subscribe 慢也能稳定接到 node_completed）。
    """
    p = tmp_path / "slow.yaml"
    p.write_text(
        """
name: slow
entry: a
nodes:
  - name: a
    kind: script
    command: "sleep 5"
    routes:
      - to: $end
""",
        encoding="utf-8",
    )
    return p


@pytest.mark.skipif(not _PLAYWRIGHT_AVAILABLE, reason="playwright 未安装")
def test_playwright_runs_api(live_server, tmp_path):
    """playwright fetch('/api/runs') 返回非空元数据列表（懒加载断言）。SPEC §6.8。"""
    base_url, manager = live_server
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
    base_url, manager = live_server
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
    """playwright evaluate WS 客户端 → subscribe + 收到 live 事件（带 run_id）。SPEC §6.8。

    这是**唯一**验证 WS 真推送 live 事件的测试，必须确定性通过（不能依赖 race）：
    用 sleep 3 的慢 workflow，订阅窗口内 run 仍 running → pump task 把
    ``node_started``/``node_completed`` 真正推给 WS。断言：
      1. 5s 内收到至少一条事件（WS 真推送，不是只 ack subscribe）
      2. 事件带 ``run_id == rid``（pump 标签正确，前端能按 run 区分）
      3. 事件 type 是编排真发的事件（node_started/node_completed/workflow_started 等），
         证明是 bus fan-out 转发，而非 WS 层自造
    """
    base_url, manager = live_server
    yaml_path = _slow_yaml(tmp_path)

    async def go():
        rid = await manager.start_run(str(yaml_path), {}, None, None)
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            ws_url = base_url.replace("http://", "ws://") + "/ws"
            # 注入 WS 客户端 → subscribe → 收集 6s 内的所有事件（run 此期间 running）。
            # 6s 窗口：subscribe 在 ~0.5s 完成，node_completed 在 run-t≈5s emit，
            # 给 ≥0.5s 余量（CI 慢机也能稳定接到 node_completed）。
            received = await page.evaluate(
                """
                async ({ws_url, run_id}) => {
                    const ws = new WebSocket(ws_url);
                    await new Promise(r => ws.onopen = r);
                    ws.send(JSON.stringify({type: "subscribe", run_id: run_id}));
                    const msgs = [];
                    const done = new Promise(resolve => {
                        ws.onmessage = (e) => {
                            try { msgs.push(JSON.parse(e.data)); } catch {}
                            if (msgs.length >= 3) resolve();
                        };
                        setTimeout(() => resolve(), 6000);
                    });
                    await done;
                    ws.close();
                    return msgs;
                }
                """,
                {"ws_url": ws_url, "run_id": rid},
            )
            assert isinstance(received, list) and len(received) > 0, (
                "2.5s 内未收到任何 WS live 事件（pump 未真推送）"
            )
            # 每条都应带 run_id 标签（pump 注入，前端按 run 分发的前提）
            for msg in received:
                assert msg.get("run_id") == rid, (
                    f"WS 事件缺/错 run_id 标签：期望 {rid}，实际 {msg.get('run_id')}"
                )
            # 至少一条是编排真发的事件 type（证明转发自 bus，非 WS 自造）
            real_types = {
                "workflow_started",
                "node_started",
                "node_completed",
                "workflow_completed",
                "workflow_failed",
            }
            got_types = {m.get("type") for m in received}
            assert got_types & real_types, (
                f"未收到任何编排事件 type，仅 {got_types}（应为 node_started/node_completed 等）"
            )
            await browser.close()

    asyncio.run(go())
