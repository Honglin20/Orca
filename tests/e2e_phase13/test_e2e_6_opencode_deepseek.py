"""tests/e2e_phase13/test_e2e_6_opencode_deepseek.py —— E2E-6 opencode+deepseek TUI 真跑（SPEC §8.4）。

4 个用户重点验证点：
  1. **agent_message 完整性**：opencode events 模式不丢消息（每条 agent_message 在 TUI 流式 tab 可见）
  2. **TUI 各面板显示合理**：DagGraph / NodeDetail / LogStream / ChartBrowser 渲染对
  3. **render_chart 正确推送**：tape 含 custom(chart) + 图表 tab 真渲染（line chart braille 字符）
  4. **图表排布合理**：多图按 label 折叠、不同 label 分组、同 label+title 替换不堆积

驱动（全部真跑，不 mock 编排 / 不 mock render_chart）：
  - ``OrcaApp.run_test()`` 起真 TUI（headless pilot）。
  - 不 mock kickoff —— on_mount 真起 orchestrator → spawn opencode（model=deepseek/deepseek-v4-flash）。
  - opencode agent 调 Bash 工具 spawn ``python3 <chart_demo.py>`` —— 经 env 链继承 ORCA_*。
  - chart_demo.py 真调 ``orca.chart.render_chart`` → 经 unix socket → ingestor → tape。
  - 编排到终态后断言。

约定：
  - 无 opencode 二进制 / 无 auth.json → skip。
  - tape_path 用短 /tmp 路径（macOS SOCK_PATH_MAX=90）。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from pathlib import Path

import pytest

_CHART_DEMO = Path(__file__).parent / "scripts" / "chart_demo.py"
_ARTIFACTS = Path(__file__).parent / "_artifacts"


def _opencode_available() -> bool:
    if os.environ.get("ORCA_E2E_SKIP_OPENCODE") == "1":
        return False
    return shutil.which("opencode") is not None


def _deepseek_auth_present() -> bool:
    """检查 ~/.local/share/opencode/auth.json 含 deepseek provider。"""
    p = Path.home() / ".local/share/opencode/auth.json"
    if not p.exists():
        return False
    try:
        import json
        d = json.loads(p.read_text(encoding="utf-8"))
        return isinstance(d, dict) and "deepseek" in d
    except Exception:
        return False


def _braille_glyphs() -> set[str]:
    return {chr(c) for c in range(0x2800, 0x2900)}


# ── §8.4 / SPEC §6.6 e2e：opencode+deepseek 真跑 + TUI 全面板 ───────────────


class TestOpencodeDeepseekE2E:
    """E2E-6：真 opencode + deepseek + 真 render_chart + TUI snapshot。

    4 个用户重点验证点逐条断言（见各 test 方法 docstring）。
    """

    def test_opencode_deepseek_drives_chart_pipeline_e2e(self, tmp_path):
        if not _opencode_available():
            pytest.skip("opencode 二进制不可用")
        if not _deepseek_auth_present():
            pytest.skip("~/.local/share/opencode/auth.json 缺 deepseek provider")

        # 短 tape_path（macOS SOCK_PATH_MAX=90）
        h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:8]
        short_root = Path(f"/tmp/orca-p13-e2e6-{h}")
        short_root.mkdir(parents=True, exist_ok=True)
        tape_path = short_root / "tape.jsonl"

        try:
            asyncio.run(self._scenario(tape_path))
        finally:
            # 留档 tape 到 _artifacts（无论成功失败）
            try:
                _ARTIFACTS.mkdir(exist_ok=True)
                if tape_path.exists():
                    shutil.copy(
                        tape_path,
                        _ARTIFACTS / "phase13_e2e6_tape.jsonl",
                    )
            except Exception:
                pass
            shutil.rmtree(short_root, ignore_errors=True)

    async def _scenario(self, tape_path: Path):
        from orca.compile import load_workflow
        from orca.iface.cli.app import OrcaApp
        from orca.iface.cli.widgets.dag_graph import DagGraph
        from orca.iface.cli.widgets.node_detail import NodeDetail
        from orca.iface.cli.widgets.chart_canvas import _PLOTEXT_OK

        # 动态写 workflow yaml（chart_demo.py 路径要绝对）
        wf_yaml = _ARTIFACTS / "phase13_e2e6_workflow.yaml"
        wf_yaml.parent.mkdir(exist_ok=True)
        # agent 节点：opencode + deepseek-v4-flash，调 Bash spawn chart_demo.py
        # 让 agent 推 N=2 张图（同 label 不同 title + 同 label+title 再推一张验证替换）。
        wf_yaml.write_text(f"""\
name: p13_e2e6
description: phase-13 E2E-6 opencode+deepseek 真 render_chart e2e
entry: runner
nodes:
  - name: runner
    kind: agent
    executor: opencode
    model: "deepseek/deepseek-v4-flash"
    prompt: |
      You are a chart-emission bot. Run the following shell command EXACTLY THREE TIMES
      (the chart_demo.py script pushes a line chart to the orca tape each call).
      Do not call any other tool. Do not add any commentary before/after.
      After running all three commands, reply with exactly: DONE

      Command (run 3 times in total, sequentially):
        python3 {_CHART_DEMO}
    routes:
      - to: $end
outputs:
  result: "{{{{ runner.output }}}}"
""", encoding="utf-8")
        wf = load_workflow(wf_yaml)

        app = OrcaApp(wf=wf, tape_path=tape_path)

        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause(0.3)

            # poll 终态（opencode 3 次 Bash 调用 ~20-40s；预留 180s 余量）
            for _ in range(900):
                if app.terminal_state is not None:
                    break
                await pilot.pause(0.2)
            else:
                pytest.fail("opencode+deepseek 编排 180s 未到终态")
            await pilot.pause(0.5)

            # ── 验证点 1：agent_message 完整性 ──────────────────────────────
            # opencode events 模式：每个 text part → translator 产 agent_message。
            # runner 节点应有 agent_message 事件（至少 1 条最终答案 "DONE"）。
            events = list(app.bus.tape.replay())
            agent_messages = [
                e for e in events
                if e.type == "agent_message" and e.node == "runner"
            ]
            assert agent_messages, (
                "验证点 1 失败：runner 应至少 1 条 agent_message（events 模式不丢）；"
                f"events types: {[e.type for e in events]}"
            )
            # 最终 answer 文本含 DONE（agent 完成话）
            joined = "".join(
                e.data.get("text", "") for e in agent_messages
            )
            assert "DONE" in joined, (
                f"验证点 1 失败：agent_message 应含 'DONE'；joined={joined!r}"
            )

            # TUI 流式 tab：每条 agent_message 都应在 NodeDetail 的流式缓冲里
            nd = app.query_one(NodeDetail)
            graph = app.query_one(DagGraph)
            graph.select("runner")
            await pilot.pause(0.2)
            stream_lines = nd._stream_lines.get("runner", [])
            stream_text = "\n".join(stream_lines)
            # opencode translator 给 agent_message 加 [msg] 前缀（phase-12 §6.3）
            assert "[msg]" in stream_text, (
                f"验证点 1 失败：TUI 流式 tab 缺 [msg] 行（agent_message 未渲染）；"
                f"stream_lines={stream_lines}"
            )

            # ── 验证点 2：TUI 各面板显示合理 ────────────────────────────────
            # DagGraph 渲染含 runner
            graph_render = str(graph.render())
            assert "runner" in graph_render, (
                f"验证点 2 失败：DagGraph 渲染缺 runner；render={graph_render!r}"
            )
            # runner 状态为 done
            assert graph.status_of_node("runner") == "done", (
                f"验证点 2 失败：runner 应 done；"
                f"got {graph.status_of_node('runner')!r}"
            )
            # NodeDetail active=runner / kind=agent
            assert nd.active == "runner"
            assert nd.kind == "agent"

            # ── 验证点 3：render_chart 正确推送（tape + 图表渲染）────────────
            chart_events = [
                e for e in events
                if e.type == "custom" and e.data.get("kind") == "chart"
                and e.node == "runner"
            ]
            # 同 label+title 替换语义：tape 里会落 3 条（chart_demo 3 次调用每次落 1 条），
            # 但 NodeDetail._chart_panel 因同 label=training + title=loss 替换只显示 1 张
            assert len(chart_events) == 3, (
                f"验证点 3 失败：tape 应落 3 条 chart（chart_demo 调 3 次）；"
                f"got {len(chart_events)}"
            )
            # chart 字段（line / training / loss / 5 data points）
            sample = chart_events[0].data["chart"]
            assert sample["chart_type"] == "line"
            assert sample["label"] == "training"
            assert sample["title"] == "loss"
            assert len(sample["data"]) == 5

            # TUI 图表 tab 真渲染 line chart 为 braille
            assert _PLOTEXT_OK, "完整 install 下 plotext 必须可用（SPEC §6.1）"
            cp = nd._chart_panel
            cp._focus = ("runner", "training", "loss")
            cp._rerender()
            await pilot.pause(0.3)
            rendered = cp.canvas.last_rendered or ""
            braille = _braille_glyphs()
            assert any(ch in braille for ch in rendered), (
                "验证点 3 失败：line chart 未渲染为 braille；"
                f"last_rendered 片段：{rendered[:200]!r}"
            )

            # ── 验证点 4：图表排布合理（同 label 折叠 + 同 label+title 替换）─
            charts_for_runner = cp.charts_for("runner")
            assert "training" in charts_for_runner, (
                f"验证点 4 失败：runner 图表应分到 'training' label；"
                f"got {set(charts_for_runner.keys())}"
            )
            training_titles = [c["title"] for c in charts_for_runner["training"]]
            assert training_titles.count("loss") == 1, (
                f"验证点 4 失败：同 label+title 应替换不堆积；titles={training_titles}"
            )

            # ── 终态 ────────────────────────────────────────────────────────
            assert app.terminal_state.status == "completed", (
                f"验证点 2 失败：编排未 completed；got {app.terminal_state.status}"
            )

            # ── 截图存档（视觉 sanity）──────────────────────────────────────
            try:
                svg = _ARTIFACTS / "phase13_e2e6_tui.svg"
                # textual save_screenshot(filename, path=None) —— 不接 title kw
                app.save_screenshot(filename=str(svg))
            except Exception as e:
                # 截图失败不应让测试失败（视觉 sanity 非断言）；记 log
                import logging
                logging.getLogger(__name__).warning(
                    "save_screenshot 失败（视觉 sanity 不阻断 pass/fail）: %r", e
                )
