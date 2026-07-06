"""tests/e2e_phase12/test_opencode_e2e.py — phase-12 S10 真 e2e（opencode 后端）。

驱动（全部真跑，不 mock 编排）：
  1. ``OrcaApp.run_test()`` 起真 TUI（headless pilot）。
  2. 不 mock kickoff —— on_mount 真起编排 worker，spawn 真 opencode 子进程。
  3. 编排跑 2-agent workflow（analyst/reporter，executor=opencode，model=glm-4.6v）。
  4. **运行中途**（analyst started、bus 未关时）往 app.bus emit chart 事件
     （SPEC §0.3/§6.1 解耦验收：render_chart 生产者不存在，TUI 渲染路径经 bus 验证）。
  5. 编排到终态后断言每面板按 SPEC §6 推送 + 图表渲染 + 多图规整 + ChartBrowser。

约定：跟 ``tests/iface/cli/test_app.py`` 一致用 ``asyncio.run(coro)`` 包装（项目未开
pytest-asyncio auto mode）。无 opencode auth 时 skip。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

_WORKFLOW = Path(__file__).parent / "opencode_tui_workflow.yaml"
_ARTIFACTS = Path(__file__).parent / "_artifacts"


def _opencode_available() -> bool:
    if os.environ.get("ORCA_E2E_SKIP_OPENCODE") == "1":
        return False
    return shutil.which("opencode") is not None


def run_async(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def wf():
    from orca.compile import load_workflow

    return load_workflow(_WORKFLOW)


def _braille_glyphs() -> set[str]:
    return {chr(c) for c in range(0x2800, 0x2900)}


def _line_payload(label: str, title: str, ctype: str = "line") -> dict:
    return {
        "chart_type": ctype,
        "label": label,
        "title": title,
        "data": [{"x": i, "y": v} for i, v in enumerate([5, 3, 4, 2, 1], start=1)],
        "x": "x",
        "y": "y",
    }


# ── §6.6 e2e：opencode 后端真跑 + TUI 全面板 + 图表渲染 ──────────────────────


class TestOpencodeE2E:
    def test_opencode_drives_tui_end_to_end(self, wf, tmp_path):
        if not _opencode_available():
            pytest.skip("opencode 二进制不可用")
        run_async(self._scenario(wf, tmp_path))

    async def _scenario(self, wf, tmp_path):
        from orca.iface.cli.app import OrcaApp
        from orca.iface.cli.widgets import AgentsList, AgentHistory
        from orca.iface.cli.widgets.node_detail import NodeDetail
        from orca.iface.cli.widgets.chart_canvas import _PLOTEXT_OK

        _ARTIFACTS.mkdir(exist_ok=True)
        app = OrcaApp(wf=wf, tape_path=tmp_path / "tape.jsonl")

        charts_injected = {"done": False}

        async def inject_charts_midrun():
            """等 analyst running（bus 还开着）→ 往 bus emit chart 事件（SPEC §6.1）。"""
            for _ in range(300):  # 最长 ~30s 等 analyst running
                proj = app.query_one(AgentsList).projection_of("analyst")
                if proj is not None and proj.status == "running":
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(0.4)  # dispatch 把 analyst 行推到 AgentHistory
            # 选中 reporter（即便它还没跑）让图表 tab 按 reporter 过滤。
            try:
                app.query_one(AgentsList).select("reporter")
            except Exception:
                pass
            # render_chart 生产者不存在（phase-10 deferred）—— SPEC §0.3/§6.1 明文允许
            # 直接 emit 满足 types.ts 的 chart 事件验证渲染路径。
            payloads = [
                (_line_payload("training", "loss"), "reporter"),
                (_line_payload("training", "acc"), "reporter"),
                (_line_payload("eval", "f1", ctype="bar"), "reporter"),
                (_line_payload("eval", "precision", ctype="bar"), "reporter"),
                (_line_payload("wf_summary", "elapsed"), None),  # workflow 级
                # 同 label+title 再 emit 一次（必替换不堆积）：
                (_line_payload("training", "loss"), "reporter"),
            ]
            for payload, node in payloads:
                await app.bus.emit(
                    "custom",
                    {"kind": "chart", "chart": payload},
                    node=node,
                )
            charts_injected["done"] = True

        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause(0.3)
            injector = asyncio.ensure_future(inject_charts_midrun())

            # poll 终态（最长 ~150s；两个 agent 实测 ~10s）。
            for _ in range(750):
                if app.terminal_state is not None:
                    break
                await pilot.pause(0.2)
            else:
                injector.cancel()
                pytest.fail("opencode 编排 150s 未到终态")
            try:
                await asyncio.wait_for(injector, timeout=5)
            except asyncio.TimeoutError:
                pass
            await pilot.pause(0.5)

            # ── §6.0.4 / §1.4：临时 UI 态不写 tape ──────────────────────────
            tape_text = (tmp_path / "tape.jsonl").read_text(encoding="utf-8")
            assert "_selected_node" not in tape_text
            assert "_auto_follow" not in tape_text

            # ── §6.2 v2 AgentsList 状态投影 ────────────────────────────────
            # analyst 应为 done（dispatcher 跑完后状态由 node_completed 推进）。
            lst = app.query_one(AgentsList)
            analyst_proj = lst.projection_of("analyst")
            assert analyst_proj is not None, "AgentsList 应含 analyst 节点"
            assert analyst_proj.status == "done", (
                f"analyst 应 done；got {analyst_proj.status!r}"
            )
            # reporter 也应 done（terminal_state == completed 时所有 agent 都终态）
            reporter_proj = lst.projection_of("reporter")
            assert reporter_proj is not None, "AgentsList 应含 reporter 节点"
            assert reporter_proj.status == "done", (
                f"reporter 应 done；got {reporter_proj.status!r}"
            )

            # ── §6.3 AgentHistory：opencode agent_message 整块可见 ───────────
            # spec v2 §6.3：NodeDetail 仅 chart 路径活跃（display:none）；
            # 流式/输出改由 AgentHistory 承担（spec §2.3）。
            nd = app.query_one(NodeDetail)
            history = app.query_one(AgentHistory)
            # pin analyst（select 触发 _on_node_selected → _auto_follow=False）。
            lst.select("analyst")
            await pilot.pause(0.1)
            assert app._auto_follow is False, "AgentsList.select 必须 pin（v2 同语义）"
            assert app._selected_node == "analyst"
            assert history.node_name == "analyst", (
                f"AgentHistory.node_name 应为 analyst；got {history.node_name!r}"
            )
            # analyst 输出：opencode 在 node_completed 前至少 1 条 agent_message（events 模式不丢）。
            analyst_events = app._node_events.get("analyst", [])
            analyst_msgs = [
                e for e in analyst_events
                if e.type == "agent_message"
            ]
            assert analyst_msgs, (
                f"analyst 应至少 1 条 agent_message（opencode events 模式不丢）；"
                f"event_types={[e.type for e in analyst_events]}"
            )
            # AgentHistory 应已显示这些 events（set_node 全量重渲）
            assert len(history.entries) == len(analyst_events), (
                f"AgentHistory entries 数应 == _node_events[analyst]；"
                f"got history={len(history.entries)}, node_events={len(analyst_events)}"
            )

            # 切到 reporter，验证 AgentHistory 含 opencode agent_message（整块、无 thinking）。
            lst.select("reporter")
            await pilot.pause(0.1)
            assert app._selected_node == "reporter"
            assert history.node_name == "reporter"
            reporter_events = app._node_events.get("reporter", [])
            reporter_types = {e.type for e in reporter_events}
            assert "agent_message" in reporter_types, (
                f"reporter 应有 agent_message 事件；got types={reporter_types}"
            )
            # executor-agnostic 关键证明：opencode **不发** agent_thinking。
            assert "agent_thinking" not in reporter_types, (
                f"opencode 不应发 agent_thinking；got types={reporter_types}"
            )

            # ── §6.4 ChartPanel：按 label 分组 + 同 label+title 替换 ─────────
            assert charts_injected["done"], "chart 注入协程未完成"
            cp = nd._chart_panel
            lst.select("reporter")
            await pilot.pause(0.2)

            charts_for_reporter = cp.charts_for("reporter")
            assert set(charts_for_reporter.keys()) == {"training", "eval"}, (
                f"reporter 图表未按 label 分 2 组；got {set(charts_for_reporter.keys())}"
            )
            assert len(charts_for_reporter["training"]) == 2, (
                f"training 组应有 2 title；got {charts_for_reporter['training']}"
            )
            assert len(charts_for_reporter["eval"]) == 2, (
                f"eval 组应有 2 title；got {charts_for_reporter['eval']}"
            )
            training_titles = [c["title"] for c in charts_for_reporter["training"]]
            assert training_titles.count("loss") == 1, (
                f"同 label+title 替换失败：titles={training_titles}"
            )

            # ── §6.1 line chart 必须 braille 渲染（完整 install） ───────────
            assert _PLOTEXT_OK, "完整 install 下 plotext 必须可用（SPEC §6.1）"
            cp._focus = ("reporter", "training", "loss")
            cp._rerender()
            await pilot.pause(0.2)
            rendered = cp.canvas.last_rendered
            braille = _braille_glyphs()
            assert any(ch in braille for ch in rendered), (
                "line chart 未渲染为 braille（SPEC §6.1 完整 install 必 braille）；"
                f"last_rendered 片段：{rendered[:150]!r}"
            )

            # ── §6.5 ChartBrowser：__workflow__ 桶顶层 ──────────────────────
            all_charts = dict(nd.all_charts())
            assert "__workflow__" in all_charts, (
                "workflow 级 chart（node=None）未归 __workflow__ 桶"
            )
            assert "wf_summary" in all_charts["__workflow__"], (
                f"__workflow__ 桶缺 wf_summary；got {all_charts['__workflow__']}"
            )

            # ── §5 键位 c：聚焦 NodeDetail + 切图表 tab ─────────────────────
            app.action_focus_charts()
            await pilot.pause(0.1)
            assert nd.active_tab == "charts", (
                f"按 c 后 NodeDetail 未切到图表 tab；active_tab={nd.active_tab!r}"
            )

            # ── 截图存档（视觉 sanity，不作 pass/fail） ──────────────────────
            try:
                svg = _ARTIFACTS / "phase12_opencode_e2e.svg"
                app.save_screenshot(svg, title="phase12 opencode e2e")
            except Exception:
                pass

            # ── 终态 ────────────────────────────────────────────────────────
            assert app.terminal_state.status == "completed", (
                f"opencode 编排未 completed（got {app.terminal_state.status}）"
            )

    def test_opencode_profile_is_genuinely_opencode(self, wf):
        """解耦铁证：被 spawn 的 profile 是 opencode（events 模式），不是 claude。"""
        from orca.profiles import get_profile

        agent_nodes = [n for n in wf.nodes if n.kind == "agent"]
        assert agent_nodes, "workflow 应有 agent 节点"
        for node in agent_nodes:
            assert node.executor == "opencode", (
                f"{node.name!r}.executor != opencode（got {node.executor!r}）"
            )
            p = get_profile(node.executor)
            assert p.name == "opencode"
            # opencode = events 模式（无 result 终止行），与 claude 的 result_line 异协议。
            assert p.terminal.mode == "events", (
                f"opencode 必须 events 模式；got {p.terminal.mode!r}"
            )
            assert p.prompt_channel == "argv", "opencode prompt 走 argv（位置参数）"
            assert p.translator.__name__ == "opencode_translator"
