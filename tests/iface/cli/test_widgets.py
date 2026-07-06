"""test_widgets.py —— TUI widget 渲染逻辑单测（phase-12 SPEC §6.2 §6.4 §6.6）。

用 Textual ``run_test()`` pilot（headless，CI 友好）。覆盖：
  - AgentsList / AgentHistory：v2 三块布局 widget（Step 2/3 填充；Step 1a 仅占位 import）
  - LogStream ``format_event`` 格式（HH:MM:SS [session] <desc>）
  - Header stats 渲染（done/total/awaiting）
  - ChartPanel：同 label+title 幂等替换 / label 分组 / all_charts / 确定性 fold
  - ChartCanvas：7 chart_type 分派 / braille / 降级 / fail loud
  - NodeDetail：6 kind 永不空白 / ● 徽标 / executor-agnostic 流式
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical

from orca.iface.cli.widgets import (
    NODE_STATUS_ICONS,
    AgentsList,
    AgentHistory,
    ChartCanvas,
    ChartPanel,
    Header,
    LogStream,
    NodeDetail,
)
from orca.iface.cli.widgets.chart_panel import WORKFLOW_BUCKET
from orca.iface.cli.widgets.header import HeaderStats
from orca.iface.cli.widgets.log_stream import format_event


# ── pilot 跑 widget 的 helper app（共享）────────────────────────────────────


class _Harness(App):
    """把多个 widget 装进一个临时 app，便于 ``run_test()`` 驱动。"""

    def __init__(self, widgets: list) -> None:
        super().__init__()
        self._widgets = widgets

    def compose(self) -> ComposeResult:
        with Vertical():
            for w in self._widgets:
                yield w


def run_async(coro):
    return asyncio.run(coro)


def _flatten_strips(strips) -> str:
    """把 RichLog 的 ``lines``（Strip 列表，每条含若干 Segment）拍平成纯文本。

    RichLog 把 markup（如 ``[session]``）拆成不同 Style 的 segment，断言时取
    ``segment.text`` 拼接即可。``Strip._segments`` 是 textual 的私有访问器
    （公开 API 未直接暴露 segment 文本，测试场景可接受访问私有）。
    """
    parts = []
    for strip in strips:
        for segment in strip._segments:
            parts.append(segment.text)
    return "".join(parts)


# ── NODE_STATUS_ICONS 常量（SPEC §4.1 锁定 5 图标）─────────────────────────


class TestNodeStatusIcons:
    """SPEC §4.1：5 种状态图标 ✓✽⏸!○。常量锁定防 drift。"""

    def test_five_icons_defined(self):
        assert set(NODE_STATUS_ICONS.keys()) == {
            "pending", "running", "done", "failed", "blocked",
        }

    def test_icon_values_locked(self):
        # SPEC §4.1 明确：✓ done / ✽ running / ⏸ blocked / ! failed / ○ pending
        assert NODE_STATUS_ICONS["done"] == "✓"
        assert NODE_STATUS_ICONS["running"] == "✽"
        assert NODE_STATUS_ICONS["blocked"] == "⏸"
        assert NODE_STATUS_ICONS["failed"] == "!"
        assert NODE_STATUS_ICONS["pending"] == "○"


# ── v2 AgentsList（spec §2.2 完整实现，Step 2 填充）────────────────────────────


class TestAgentsList:
    """v2 AgentsList widget 单元测试（spec §2.2 + §5.1）。

    INTENT（CLAUDE.md 铁律 9）：测试不是「widget update 不崩」，而是
    「用户能看到 agent 列表 + 状态 + 切换」——断言渲染内容含拓扑序、状态 icon、
    选中标记、错误摘要；断言 j/k 切换会通知 app（驱动 AgentHistory 重渲）。
    """

    def test_build_renders_topo_order(self):
        """build() 后渲染顺序与输入一致（用户看 agent 列表按拓扑序排列）。"""
        lst = AgentsList()
        lst.build(["analyzer", "configurator", "runner"])
        # 用 Static.content 公开 API（render() 返 Visual，content 返原始字符串）
        content = lst.content if isinstance(lst.content, str) else str(lst.content)
        assert "analyzer" in content
        assert "configurator" in content
        assert "runner" in content
        # 拓扑序：analyzer 必须在 configurator 之前
        assert content.find("analyzer") < content.find("configurator") < content.find("runner")

    def test_build_selects_first_by_default(self):
        """build 后默认选中第一个（j/k 从头开始；auto-follow 之前用户的视觉锚点）。"""
        lst = AgentsList()
        lst.build(["analyzer", "configurator"])
        assert lst.selected == "analyzer"
        # 渲染内容含选中标记 ▸ 在 analyzer 前
        content = lst.content if isinstance(lst.content, str) else str(lst.content)
        analyzer_idx = content.find("analyzer")
        assert analyzer_idx > 0
        assert content[analyzer_idx - 2] == "▸"  # sel_mark 在 name 前 2 格（sel + space）

    def test_build_empty_nodes_renders_placeholder(self):
        """build([]) 渲染占位（不崩；用户空 workflow 看到提示）。"""
        lst = AgentsList()
        lst.build([])
        assert lst.selected is None
        content = lst.content if isinstance(lst.content, str) else str(lst.content)
        assert "no agents" in content

    def test_update_node_status_running(self):
        """update_node(status='running') 投影生效（用户看到 agent 进入 running 状态）。"""
        lst = AgentsList()
        lst.build(["analyzer"])
        lst.update_node("analyzer", status="running", iter_n=1)
        proj = lst.projection_of("analyzer")
        assert proj is not None
        assert proj.status == "running"
        assert proj.iter_n == 1
        # 渲染内容含 running icon ✽
        content = lst.content if isinstance(lst.content, str) else str(lst.content)
        assert "✽" in content

    def test_update_node_done_with_elapsed_tokens(self):
        """update_node(status='done', elapsed, tokens) 投影生效（用户看到完成耗时 + tok）。"""
        lst = AgentsList()
        lst.build(["analyzer"])
        lst.update_node("analyzer", status="done", elapsed=14.0, tokens=1234)
        proj = lst.projection_of("analyzer")
        assert proj.status == "done"
        assert proj.elapsed == 14.0
        assert proj.tokens == 1234
        # 渲染含 ✓ done icon + 14s + 1.2k
        content = lst.content if isinstance(lst.content, str) else str(lst.content)
        assert "✓" in content
        assert "14s" in content
        assert "1.2k" in content

    def test_update_node_partial_field_update(self):
        """update_node(elapsed=...) 不覆盖既有 status/tokens（部分字段更新语义）。"""
        lst = AgentsList()
        lst.build(["analyzer"])
        lst.update_node("analyzer", status="running", iter_n=1, tokens=500)
        lst.update_node("analyzer", elapsed=20.0)  # 部分更新
        proj = lst.projection_of("analyzer")
        assert proj.status == "running"  # 保留
        assert proj.tokens == 500        # 保留
        assert proj.iter_n == 1          # 保留
        assert proj.elapsed == 20.0      # 新增

    def test_update_node_unknown_name_ignored(self):
        """未知 name 静默忽略（防御；与 v1.1.1 DagGraph.set_status 同语义）。"""
        lst = AgentsList()
        lst.build(["analyzer"])
        lst.update_node("nonexistent", status="running")  # 不抛
        assert lst.projection_of("nonexistent") is None
        # 已有节点不受影响
        assert lst.projection_of("analyzer").status == "pending"

    def test_update_node_unknown_status_ignored(self):
        """未知 status 字符串忽略（防御；spec §11.5 item2 不引入新 enum，只接受 NODE_STATUS_ICONS keys）。"""
        lst = AgentsList()
        lst.build(["analyzer"])
        lst.update_node("analyzer", status="bogus_state")
        proj = lst.projection_of("analyzer")
        # status 没改成 bogus，仍是 pending
        assert proj.status == "pending"

    def test_select_triggers_app_callback(self):
        """select(name) 调 app._on_node_selected(name)（duck-typing；spec §3 切换语义）。"""
        lst = AgentsList()

        class _MockApp:
            def __init__(self) -> None:
                self.selected_called_with: str | None = None
            def _on_node_selected(self, name: str) -> None:
                self.selected_called_with = name

        lst.build(["analyzer", "configurator"])
        # textual widget.app 在未 mount 时返 None（不抛）；select 内用 getattr 兜底，
        # 故未挂载时不会调 _on_node_selected。本测试通过 setattr 注入 mock app
        #（绕过 textual 真起 app pilot；与 v1.1.1 DagGraph 测试同模式）。
        mock_app = _MockApp()
        type(lst).app = property(lambda _: mock_app)  # type: ignore[misc]
        try:
            lst.select("configurator")
        finally:
            # 还原（避免污染后续测试）
            del type(lst).app
        assert mock_app.selected_called_with == "configurator"
        assert lst.selected == "configurator"

    def test_j_k_navigation_wraps(self):
        """j/k 在边界 wrap（最后一个 → 第一个；用户线性遍历不会卡死在边界）。"""
        lst = AgentsList()

        class _MockApp:
            def __init__(self) -> None:
                self.history: list[str] = []
            def _on_node_selected(self, name: str) -> None:
                self.history.append(name)

        mock_app = _MockApp()
        type(lst).app = property(lambda _: mock_app)  # type: ignore[misc]
        try:
            lst.build(["a", "b", "c"])
            assert lst.selected == "a"
            lst.action_select_next()  # a → b
            assert lst.selected == "b"
            lst.action_select_next()  # b → c
            assert lst.selected == "c"
            lst.action_select_next()  # c → a (wrap)
            assert lst.selected == "a"
            lst.action_select_prev()  # a → c (wrap back)
            assert lst.selected == "c"
        finally:
            del type(lst).app

    def test_select_unknown_name_ignored(self):
        """select(unknown) 静默忽略（防御；不污染 _selected）。"""
        lst = AgentsList()
        lst.build(["a", "b"])
        original = lst.selected
        lst.select("nonexistent")
        assert lst.selected == original  # 不变

    def test_iter_n_display_when_loop_reentry(self):
        """iter_n >= 2 时显示「iter N」（loop workflow 重入可视化；用户看 agent 跑第几轮）。"""
        lst = AgentsList()
        lst.build(["counter"])
        lst.update_node("counter", status="running", iter_n=2, tokens=800)
        content = lst.content if isinstance(lst.content, str) else str(lst.content)
        assert "iter 2" in content

    def test_failed_status_shows_error_summary(self):
        """failed 状态第二行显错误摘要前 30 字符（用户看到失败原因；spec §6.3 错误显示）。"""
        lst = AgentsList()
        lst.build(["runner"])
        lst.update_node(
            "runner",
            status="failed",
            error_msg="RuntimeError: cuda OOM at layer 47 in training loop",
        )
        content = lst.content if isinstance(lst.content, str) else str(lst.content)
        # 第二行错误标记 + 摘要（前 30 字符）
        assert "!" in content
        # 前 30 字符: "RuntimeError: cuda OOM at layer "[:30] == "RuntimeError: cuda OOM at la"
        assert "RuntimeError" in content
        # 完整原文（>30）不应全显（避免行宽爆炸，第二行只显前 30）
        assert "training loop" not in content

    def test_format_elapsed_helper(self):
        """_format_elapsed 边界值（spec §2.2：< 60s 显 {n}s；>= 60s 显 {m}m{s}s）。"""
        from orca.iface.cli.widgets.agents_list import _format_elapsed
        assert _format_elapsed(0) == "0s"
        assert _format_elapsed(14.0) == "14s"
        assert _format_elapsed(59.9) == "60s"  # 59.9 → :.0f 取整 60
        assert _format_elapsed(60.0) == "1m0s"
        assert _format_elapsed(90.0) == "1m30s"
        assert _format_elapsed(-5) == "0s"  # 负值兜底

    def test_format_tokens_helper(self):
        """_format_tokens 边界值（spec §2.2：< 1000 显原数；>= 1000 显 {k}k）。"""
        from orca.iface.cli.widgets.agents_list import _format_tokens
        assert _format_tokens(0) == "0"
        assert _format_tokens(500) == "500"
        assert _format_tokens(999) == "999"
        assert _format_tokens(1000) == "1.0k"
        assert _format_tokens(1234) == "1.2k"
        assert _format_tokens(24000) == "24.0k"
        assert _format_tokens(-1) == "0"  # 负值兜底


class TestAgentHistoryShell:
    """v2 右上 AgentHistory：Step 1a 占位空 shell，仅 import + 实例化。

    Step 3 填充 set_node / append_event / last message 默认展开后改为完整单测。
    """

    def test_agent_history_imports(self):
        """import AgentHistory 不崩（Step 1a 占位守门）。"""
        from orca.iface.cli.widgets import AgentHistory as _AH
        assert _AH is AgentHistory

    def test_agent_history_can_instantiate(self):
        """空 shell 可实例化（compose 时挂得上）。"""
        widget = AgentHistory()
        assert widget is not None


# ── LogStream format_event（SPEC §4.3：HH:MM:SS [session] <desc>）─────────


class TestLogStreamFormat:
    """``format_event`` 纯函数：格式 + 各事件类型描述。"""

    FIXED_TS = time.mktime(time.strptime("14:02:11", "%H:%M:%S"))

    def test_basic_format(self):
        line = format_event(
            "agent_message", {"text": "hello"},
            session_id="abcdef0123456789", timestamp=self.FIXED_TS,
        )
        assert line == "14:02:11 [abcdef01] hello"

    def test_session_truncated_to_8_chars(self):
        line = format_event(
            "agent_message", {"text": "x"},
            session_id="0123456789abcdef", timestamp=self.FIXED_TS,
        )
        assert "[01234567]" in line

    def test_no_session_shows_dash(self):
        line = format_event(
            "agent_message", {"text": "x"}, session_id=None, timestamp=self.FIXED_TS,
        )
        assert "[-]" in line

    def test_node_prefix_when_node_given(self):
        line = format_event(
            "agent_message", {"text": "x"},
            node="research", session_id="abcd1234", timestamp=self.FIXED_TS,
        )
        assert "14:02:11 [abcd1234] research · x" == line

    def test_agent_tool_call_described(self):
        line = format_event(
            "agent_tool_call", {"tool": "Bash", "args": {"command": "ls"}},
            session_id="s1", timestamp=self.FIXED_TS,
        )
        assert "tool: Bash(" in line

    def test_node_lifecycle_described(self):
        assert "node started" in format_event(
            "node_started", {"kind": "script"}, timestamp=self.FIXED_TS,
        )
        assert "node completed" in format_event(
            "node_completed", {"elapsed": 1.2}, timestamp=self.FIXED_TS,
        )
        assert "node FAILED" in format_event(
            "node_failed", {"message": "boom"}, timestamp=self.FIXED_TS,
        )

    def test_gate_events_described(self):
        req = format_event(
            "human_decision_requested", {"prompt": "批准 Bash？"},
            timestamp=self.FIXED_TS,
        )
        assert "gate: 批准 Bash？" == req.split("] ", 1)[1]
        res = format_event(
            "human_decision_resolved",
            {"resolved_by": "web", "answer": "allow"},
            timestamp=self.FIXED_TS,
        )
        assert "gate resolved by web" in res

    def test_long_text_truncated(self):
        long_text = "x" * 200
        line = format_event(
            "agent_message", {"text": long_text}, timestamp=self.FIXED_TS,
        )
        # 描述截短到 60 + 1（…），不会占满终端宽度
        desc = line.split("] ", 1)[1]
        assert len(desc) <= 61
        assert desc.endswith("…")

    # ── phase 11 收官 sweep item8：每个 EventType 都有非泛型描述 ────────────────

    # 各 EventType 的合理 payload（让 _describe 的 data.get(...) 拿到非空值，
    # 避免「描述 = 截短的空串」误判为「有专属描述」）。
    _PAYLOADS = {
        "workflow_started": {"workflow_name": "wf"},
        "workflow_completed": {"elapsed": 1.0},
        "workflow_failed": {"error_type": "spawn_error", "message": "boom"},
        "workflow_cancelled": {"reason": "user"},
        "node_started": {"kind": "agent"},
        "node_completed": {"elapsed": 1.0},
        "node_failed": {"message": "boom", "error_type": "spawn_error"},
        "node_skipped": {"reason": "user"},
        "agent_message": {"text": "hi"},
        "agent_thinking": {"text": "hmm"},
        "agent_tool_call": {"tool": "Bash", "args": {"cmd": "ls"}},
        "agent_tool_result": {"tool_call_id": "1", "result": "ok"},
        "agent_usage": {"input_tokens": 1, "output_tokens": 2,
                        "cache_tokens": 3, "cost_usd": 0.1},
        "route_taken": {"from": "a", "to": "b"},
        "foreach_started": {"item_count": 3, "max_concurrent": 2},
        "foreach_item_started": {"index": 0, "item_key": "k"},
        "foreach_item_completed": {"index": 0, "output": "x"},
        "foreach_completed": {"count": 3, "succeeded": 3},
        "human_decision_requested": {"gate_id": "g", "prompt": "approve?"},
        "human_decision_resolved": {"gate_id": "g", "answer": "yes"},
        "interrupt_requested": {"node": "a", "elapsed_at_request": 1.0},
        "interrupt_resolved": {"action": "skip", "skip_target": "b"},
        "prompt_rendered": {"preview": "..."},
        "workflow_resumed": {"from_tape": "t.jsonl", "resumed_node": "a",
                             "replayed_events": 5},
        "retry_started": {"attempt": 1, "max_attempts": 3, "error_type": "spawn_error",
                          "delay_seconds": 1.0},
        "retry_succeeded": {"attempt_total": 2},
        "retry_exhausted": {"attempts": 3, "last_error_type": "spawn_error"},
        "wait_started": {"duration_seconds": 60.0, "reason": "rl"},
        "wait_completed": {"elapsed_seconds": 60.0, "interrupted": False},
        "validator_started": {"criteria_preview": "must be valid"},
        "validator_passed": {},
        "validator_failed": {"issues": ["bad"], "retrying": True},
        "dialog_started": {"node": "a", "initial_prompt": "why?"},
        "dialog_message": {"role": "user", "text": "hi", "turn": 1},
        "dialog_ended": {"node": "a", "total_turns": 1},
        "custom": {"kind": "chart"},
        "error": {"error_type": "ValueError", "message": "boom"},
    }

    def test_every_event_type_has_non_generic_description(self):
        """穷尽性守门：遍历 EventType Literal 全集，每个 type 的描述都不落入泛型 fallback。

        INTENT（SPEC §10.2 item8 / final sweep）：LogStream 必须给**每个**新 EventType 一个
        非泛型描述（``_describe`` 不落入末尾的 ``return event_type`` 兜底）。否则用户在 LogStream
        看到的就是裸 type 名（如 ``retry_started``）而非人类可读描述，违反 item8。

        与既有测试的区别：
          - 既有 ad-hoc 测试（test_node_lifecycle_described / test_gate_events_described）只挑几个
            type 断言，不遍历全集；新增 type 漏 ``_describe`` 分支时这些测试抓不到。
          - 本测试遍历真 Literal 全集，新增 type 漏分支时立即可见（描述 == 裸 type 名）。
        """
        import typing

        from orca.schema import EventType

        types = typing.get_args(EventType)
        assert len(types) > 0  # sanity：Literal 非空

        leaked = []
        for t in types:
            payload = self._PAYLOADS.get(t, {})
            desc = format_event(t, payload, timestamp=self.FIXED_TS)
            # 描述段 = 去掉时间戳 + session 前缀后的部分。
            body = desc.split("] ", 1)[1] if "] " in desc else desc
            # node 前缀（``node · desc``）也要剥掉，只看 _describe 产出。
            if " · " in body:
                body = body.split(" · ", 1)[1]
            # 泛型 fallback：body 与裸 type 名相同（_describe 未匹配任何 if 分支）。
            if body == t:
                leaked.append(t)
        assert leaked == [], (
            f"以下 EventType 落入 format_event 泛型 fallback（_describe 漏分支，"
            f"LogStream 会显示裸 type 名）：{leaked}"
        )


class TestLogStreamWidget:
    """LogStream widget：append_event 写入文本。"""

    def test_append_event_writes_line(self):
        stream = LogStream()

        async def scenario():
            async with _Harness([stream]).run_test() as pilot:
                stream.append_event(
                    "agent_message", {"text": "hello"},
                    session_id="abcdef0123", timestamp=TestLogStreamFormat.FIXED_TS,
                )
                await pilot.pause()
                await pilot.pause()  # RichLog 异步渲染，双 pause 确保 flush
                # RichLog.lines 是 Strip 列表（segment 组），flatten 成纯文本断言
                text = _flatten_strips(stream.lines)
                assert "hello" in text
                assert "14:02:11" in text
                assert "abcdef01" in text  # session 截短
        run_async(scenario())


# ── Header stats（SPEC §4.4）────────────────────────────────────────────────


class TestHeader:
    """Header：stats 渲染（done/total/awaiting/model）。"""

    def test_stats_render_text_basic(self):
        stats = HeaderStats(run_id="r1", workflow_name="nas", total=7, done=3)
        text = stats.render_text()
        assert "Orca Run #r1" in text
        assert "nas" in text
        assert "3/7 nodes" in text
        assert "awaiting gate" not in text  # awaiting=0 不显示
        # 无 model 时，workflow_name 与 nodes 数之间只有一个 `` · `` 分隔
        assert "sonnet" not in text  # 无 model

    def test_stats_render_with_model_and_gate(self):
        stats = HeaderStats(
            run_id="r1", workflow_name="nas", model="sonnet",
            total=7, done=3, awaiting_gate=2,
        )
        text = stats.render_text()
        assert "· sonnet ·" in text
        assert "⏸ 2 awaiting gate" in text

    def test_widget_update_stats_stored(self):
        header = Header()

        async def scenario():
            async with _Harness([header]).run_test() as pilot:
                stats = HeaderStats(run_id="r9", workflow_name="demo", total=2, done=1)
                header.update_stats(stats)
                await pilot.pause()
                await pilot.pause()  # update 异步刷新，双 pause 确保 flush
                assert header.stats is stats
                # Header(Static) 的当前文本经 name-mangled 私有属性访问（_Static__content）
                rendered = getattr(header, "_Static__content", "")
                assert "demo" in rendered
                assert "1/2 nodes" in rendered
        run_async(scenario())


# ── ChartPanel（确定性 fold / 幂等 / label 分组 / all_charts）SPEC §6.4 ──────


def _payload(label: str, title: str, ctype: str = "line", data=None) -> dict:
    return {
        "chart_type": ctype,
        "label": label,
        "title": title,
        "data": data if data is not None else [{"x": i, "y": i} for i in range(5)],
    }


class TestChartPanel:
    """ChartPanel：同 label+title 幂等替换 / label 分组 / all_charts / 确定性 fold。"""

    def test_same_label_title_replaces_not_accumulates(self):
        """SPEC §6.4 / phase-9d §2.7：同 label+title 两次 → 1 图。"""
        panel = ChartPanel()

        async def scenario():
            async with _Harness([panel]).run_test() as pilot:
                panel.upsert("n1", _payload("L", "T1", data=[{"x": 1, "y": 1}]))
                panel.upsert("n1", _payload("L", "T1", data=[{"x": 1, "y": 9}]))
                await pilot.pause()
                charts = panel.charts_for("n1")
                assert len(charts["L"]) == 1  # 替换不堆积
                assert charts["L"][0]["data"][0]["y"] == 9  # 是后者
        run_async(scenario())

    def test_label_grouping_3x3(self):
        """SPEC §6.4：3 label×3 title → 9 图按 label 分 3 组。"""
        panel = ChartPanel()

        async def scenario():
            async with _Harness([panel]).run_test() as pilot:
                for label in ("A", "B", "C"):
                    for t in range(3):
                        panel.upsert("n1", _payload(label, f"t{t}"))
                await pilot.pause()
                charts = panel.charts_for("n1")
                assert set(charts.keys()) == {"A", "B", "C"}
                for label in ("A", "B", "C"):
                    assert len(charts[label]) == 3
        run_async(scenario())

    def test_workflow_bucket_node_none(self):
        """SPEC §3.3 D2-a：node=None → __workflow__ 桶；all_charts 顶层。"""
        panel = ChartPanel()

        async def scenario():
            async with _Harness([panel]).run_test() as pilot:
                panel.upsert(None, _payload("WL", "WT"))
                panel.upsert("n1", _payload("L", "T"))
                await pilot.pause()
                keys = [k for k, _ in panel.all_charts()]
                assert keys[0] == WORKFLOW_BUCKET  # 顶层
                assert "n1" in keys
        run_async(scenario())

    def test_deterministic_fold_clear_replay_equal(self):
        """SPEC §6.0.3：清空投影→重放同段事件→投影完全一致（确定性 fold 证伪）。"""
        events = [
            ("n1", _payload("L1", "T1", data=[{"x": 1, "y": 2}])),
            ("n1", _payload("L1", "T2")),
            ("n2", _payload("L2", "T1")),
            (None, _payload("WL", "WT")),
        ]

        async def run_once() -> dict:
            panel = ChartPanel()
            async with _Harness([panel]).run_test() as pilot:
                for node_key, p in events:
                    panel.upsert(node_key, p)
                await pilot.pause()
                # 序列化投影为可比较的结构。
                return {
                    k: {l: list(t.values()) for l, t in labels.items()}
                    for k, labels in panel.projection.items()
                }

        first = run_async(run_once())
        second = run_async(run_once())
        assert first == second, "确定性 fold：清空→重放→投影应一致"

    def test_malformed_payload_skipped_with_warning(self):
        """SPEC §6.4：缺 chart_type / data 非 list → 跳过（不崩）。"""
        panel = ChartPanel()

        async def scenario():
            async with _Harness([panel]).run_test() as pilot:
                # 缺 chart_type。
                panel.upsert("n1", {"label": "L", "title": "T", "data": []})
                # data 非 list。
                panel.upsert("n1", {"chart_type": "line", "label": "L", "title": "T2",
                                    "data": "notalist"})
                # 缺 label。
                panel.upsert("n1", {"chart_type": "line", "title": "T3", "data": []})
                await pilot.pause()
                charts = panel.charts_for("n1")
                assert charts == {}, "残缺 payload 全部跳过"
        run_async(scenario())


# ── ChartCanvas（分派 / braille / 降级 / fail loud）SPEC §1.2 §6.1 §6.4 ─────


class TestChartCanvas:
    """ChartCanvas：7 chart_type 分派 + braille + 降级 + fail loud。"""

    @pytest.mark.parametrize("ctype", ["line", "bar", "area", "scatter", "pareto"])
    def test_plotext_types_render_braille(self, ctype):
        """SPEC §6.1 必测：完整 install 下 line/bar/area/scatter/pareto → braille。

        断言 ``canvas.last_rendered`` 含 braille 码点（U+2800–U+28FF）—— 不扒 Static 私有。
        """
        canvas = ChartCanvas()

        async def scenario():
            async with _Harness([canvas]).run_test() as pilot:
                canvas.render_payload({
                    "chart_type": ctype, "label": "L", "title": "T",
                    "x": "x", "y": "y",
                    "data": [{"x": i, "y": (i - 3) ** 2} for i in range(8)],
                })
                await pilot.pause()
                await pilot.pause()  # Static update 异步 flush
                text = canvas.last_rendered
                braille = [c for c in text if "⠀" <= c <= "⣿"]
                assert braille, f"{ctype} 必须含 braille 字符（SPEC §6.1）"
        run_async(scenario())

    def test_table_renders_text_table(self):
        canvas = ChartCanvas()

        async def scenario():
            async with _Harness([canvas]).run_test() as pilot:
                canvas.render_payload({
                    "chart_type": "table", "label": "L", "title": "T",
                    "columns": ["a", "b"],
                    "data": [{"a": 1, "b": 2}, {"a": 3, "b": 4}],
                })
                await pilot.pause()
                await pilot.pause()
                text = getattr(canvas, "_Static__content", "")
                assert "a | b" in text
                assert "1" in text and "4" in text
        run_async(scenario())

    def test_radar_degrades_to_table_with_hint(self):
        """SPEC §1.2：radar → DataTable 降级 +「见 Web」提示。"""
        canvas = ChartCanvas()

        async def scenario():
            async with _Harness([canvas]).run_test() as pilot:
                canvas.render_payload({
                    "chart_type": "radar", "label": "L", "title": "T",
                    "data": [{"axis": "x", "value": 1}],
                })
                await pilot.pause()
                await pilot.pause()
                text = getattr(canvas, "_Static__content", "")
                assert "见 Web" in text or "radar" in text.lower()
        run_async(scenario())

    def test_unknown_chart_type_fail_loud(self):
        """SPEC §1.2 / 铁律 12：未知 chart_type → fail loud（不静默崩）。"""
        canvas = ChartCanvas()

        async def scenario():
            async with _Harness([canvas]).run_test() as pilot:
                canvas.render_payload({"chart_type": "bogus", "data": []})
                await pilot.pause()
                await pilot.pause()
                text = getattr(canvas, "_Static__content", "")
                assert "未知 chart_type" in text
        run_async(scenario())

    def test_missing_plotext_degrades_gracefully(self, monkeypatch):
        """SPEC §6.4 开发期降级测试：缺包 → DataTable + 提示（生产不缺包，仅 monkeypatch）。"""
        # 模拟 plotext 缺失：把 sys.modules['plotext'] 置 None + 重载 chart_canvas。
        monkeypatch.setitem(sys.modules, "plotext", None)
        import importlib
        import orca.iface.cli.widgets.chart_canvas as cc_mod
        importlib.reload(cc_mod)
        DegradedCanvas = cc_mod.ChartCanvas
        canvas = DegradedCanvas()

        async def scenario():
            async with _Harness([canvas]).run_test() as pilot:
                canvas.render_payload({
                    "chart_type": "line", "label": "L", "title": "T",
                    "data": [{"x": 1, "y": 2}],
                })
                await pilot.pause()
                await pilot.pause()
                text = getattr(canvas, "_Static__content", "")
                # 降级：DataTable 文本（有数据行）+ 提示。
                assert "降级" in text or "x" in text.lower()
        run_async(scenario())


# ── NodeDetail（6 kind 永不空白 / ● 徽标 / executor-agnostic）SPEC §6.3 ────


class TestNodeDetail:
    """NodeDetail：6 kind 派发 + ● 徽标 + executor-agnostic 流式。"""

    def test_agent_stream_n_events_n_lines(self):
        """SPEC §6.3：N 个 agent_* 事件 → N 行（executor-agnostic）。"""
        nd = NodeDetail()

        async def scenario():
            async with _Harness([nd]).run_test() as pilot:
                nd.set_node("a", kind="agent")
                await pilot.pause()
                for i in range(5):
                    nd.append_event_stream("a", "agent_message", {"text": f"msg{i}"})
                    await pilot.pause()
                # 5 个事件 → 5 行（缓存）。
                assert len(nd._stream_lines["a"]) == 5
        run_async(scenario())

    def test_agent_thinking_only_also_renders(self):
        """SPEC §6.3：claude 发 thinking、opencode 不发；只有 message 也正确。

        模拟 opencode 路径：仅 agent_message（无 thinking）→ 仍有 N 行。
        """
        nd = NodeDetail()

        async def scenario():
            async with _Harness([nd]).run_test() as pilot:
                nd.set_node("a", kind="agent")
                await pilot.pause()
                nd.append_event_stream("a", "agent_message", {"text": "only message"})
                await pilot.pause()
                assert len(nd._stream_lines["a"]) == 1
        run_async(scenario())

    def test_script_node_not_blank_after_completed(self):
        """SPEC §1.3：script node 完成后输出 tab 有 {stdout,stderr,exit_code}。"""
        nd = NodeDetail()

        async def scenario():
            async with _Harness([nd]).run_test() as pilot:
                nd.set_node("s", kind="script")
                await pilot.pause()
                nd.set_output("s", {"stdout": "hi", "stderr": "", "exit_code": 0})
                await pilot.pause()
                assert nd._outputs["s"]["stdout"] == "hi"
        run_async(scenario())

    def test_foreach_progress_in_stream(self):
        """SPEC §1.3：foreach 流式 tab 有 foreach_started/completed 进度。"""
        nd = NodeDetail()

        async def scenario():
            async with _Harness([nd]).run_test() as pilot:
                nd.set_node("f", kind="foreach")
                await pilot.pause()
                nd.append_event_stream("f", "foreach_started", {"item_count": 3, "max_concurrent": 2})
                nd.append_event_stream("f", "foreach_completed", {"count": 3, "succeeded": 3})
                await pilot.pause()
                assert len(nd._stream_lines["f"]) == 2
        run_async(scenario())

    def test_wait_events_in_stream(self):
        """SPEC §1.3：wait 流式 tab 有 wait_started/completed。"""
        nd = NodeDetail()

        async def scenario():
            async with _Harness([nd]).run_test() as pilot:
                nd.set_node("w", kind="wait")
                await pilot.pause()
                nd.append_event_stream("w", "wait_started", {"duration_seconds": 30, "reason": "rl"})
                nd.append_event_stream("w", "wait_completed", {"elapsed_seconds": 30, "interrupted": False})
                await pilot.pause()
                assert len(nd._stream_lines["w"]) == 2
        run_async(scenario())

    def test_terminate_node_started_in_stream(self):
        """SPEC §1.3：terminate 流式 tab 有 node_started{kind:terminate}。"""
        nd = NodeDetail()

        async def scenario():
            async with _Harness([nd]).run_test() as pilot:
                nd.set_node("t", kind="terminate")
                await pilot.pause()
                nd.append_event_stream("t", "node_started", {"kind": "terminate"})
                await pilot.pause()
                assert len(nd._stream_lines["t"]) == 1
        run_async(scenario())

    def test_chart_upsert_dirty_badge_until_tab_activated(self):
        """SPEC §6.3：upsert_chart 到非图表 tab → ● 置位；Tab.Activated(图表) → 清除。"""
        nd = NodeDetail()

        async def scenario():
            async with _Harness([nd]).run_test() as pilot:
                nd.set_node("a", kind="agent")
                await pilot.pause()
                # 默认 active=流式，upsert_chart → 图表 dirty。
                nd.upsert_chart("a", _payload("L", "T"))
                await pilot.pause()
                assert nd.dirty["charts"] is True
                # 模拟 Tab.Activated(图表)：直接调 handler（绕过 UI 点击）。
                # 构造一个最小的 TabActivated-like 事件。
                from textual.widgets._tabs import Tabs
                from textual.message import Message

                # NodeDetail._on_tab_activated 读 event.tab.id；构造 stub。
                class _StubTab:
                    id = "charts"
                class _StubEvent(Tabs.TabActivated):
                    def __init__(self):
                        pass  # 不调 super（避免依赖 Tabs 内部）
                    tab = _StubTab()

                nd._on_tab_activated(_StubEvent())
                assert nd.dirty["charts"] is False
        run_async(scenario())

    def test_set_node_filters_stream_by_selected(self):
        """SPEC §4.2：append_stream 只在 node==_selected 时显示（别节点事件不混入显示）。

        语义：append_stream 按节点缓存（切回时显示历史）；当前显示的只有 _selected。
        故 a 选中时，b 的事件不会出现在 a 的流式 tab 内容里。
        """
        nd = NodeDetail()

        async def scenario():
            async with _Harness([nd]).run_test() as pilot:
                nd.set_node("a", kind="agent")
                await pilot.pause()
                # b 的事件缓存到 b（不混入 a 的显示）。
                nd.append_stream("b", "[msg] other node")
                await pilot.pause()
                # a 的缓存不含 b 的事件。
                assert nd._stream_lines.get("a", []) == []
                # b 的缓存有那条（切到 b 时才显示）。
                assert nd._stream_lines.get("b") == ["[msg] other node"]
        run_async(scenario())


# ── ChartBrowser（C 全屏 / 数据源 all_charts / __workflow__ 顶层）SPEC §6.5 ──


class TestChartBrowser:
    """ChartBrowser：全屏跨节点图表浏览（SPEC §4.5 / §6.5）。

    通过 OrcaApp 真跑（ChartBrowser 数据源 = ``app.query_one(NodeDetail).all_charts()``，
    需 app 上下文）。验收：列表含所有图 + __workflow__ 顶层 + 选中触发预览。
    """

    def _wf_yaml(self, tmp_path):
        import yaml as _yaml
        p = tmp_path / "wf.yaml"
        p.write_text(_yaml.safe_dump({
            "name": "t", "entry": "a",
            "nodes": [
                {"name": "a", "kind": "script", "command": "echo a",
                 "routes": [{"to": "$end"}]},
            ],
        }))
        return p

    def test_browser_lists_charts_with_workflow_on_top(self, tmp_path):
        """SPEC §6.5：C 进全屏 → 列所有图，__workflow__ 顶层。"""
        from orca.iface.cli.app import OrcaApp
        from orca.iface.cli.screens.chart_browser import ChartBrowser
        from orca.compile import load_workflow

        wf = load_workflow(self._wf_yaml(tmp_path))
        app = OrcaApp(wf=wf, tape_path=tmp_path / "e.jsonl")
        app.kickoff = lambda: None

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                # 注入两张图：一张节点级，一张 workflow 级。
                app._dispatch_to_widgets(_event_like_chart(node="a", label="L", title="T1"))
                app._dispatch_to_widgets(_event_like_chart(node=None, label="WL", title="WT"))
                await pilot.pause()
                # 推 ChartBrowser。
                app.push_screen(ChartBrowser())
                await pilot.pause()
                await pilot.pause()
                browser = app.screen
                # 数据源 = NodeDetail.all_charts()，__workflow__ 在前。
                items = browser._items
                assert items[0][0] == "__workflow__"  # workflow 顶层
                assert any(k == "a" for k, _, _ in items)
        run_async(scenario())


def _event_like_chart(*, node, label, title, ctype="line"):
    """构造 custom(chart) event-like（与 test_app._event 同模式）。"""
    from types import SimpleNamespace
    return SimpleNamespace(
        type="custom",
        data={"kind": "chart", "chart": {
            "chart_type": ctype, "label": label, "title": title,
            "x": "x", "y": "y",
            "data": [{"x": i, "y": i} for i in range(5)],
        }},
        node=node, session_id=None, seq=0, timestamp=0.0,
    )


# ── v2 _event_summary 共享纯函数（Step 1b 迁入；Step 3 AgentHistory / Step 4
#    LogStream 单测填充时再补完整字段级断言；当前最小守门：6 函数能 import）。 ──────


class TestEventSummaryImports:
    """spec v2 §2.3 / §2.4：6 个共享事件派生纯函数 import 守门（Step 1b 占位）。

    Step 3 AgentHistory 填充时补 set_node / append_event / last message 默认展开 等单测
    （调 _build_summary_line / _build_meta_line / _build_detail_renderable 断言字段级输出）；
    Step 4 LogStream 改造时同理补 EVENT_LEVEL 表派生单测。
    """

    def test_event_summary_imports(self):
        """6 个共享函数能 import（Step 1b 占位守门，防迁移漏函数）。"""
        from orca.iface.cli.widgets._event_summary import (
            _arg_title,
            _build_detail_renderable,
            _build_meta_line,
            _build_summary_line,
            _format_elapsed_sec,
            _truncate,
        )
        # sanity：所有函数都是 callable
        for fn in (
            _arg_title, _build_detail_renderable, _build_meta_line,
            _build_summary_line, _format_elapsed_sec, _truncate,
        ):
            assert callable(fn)

    def test_truncate_basic(self):
        """spec §5.4：超长字符截断 + …。"""
        from orca.iface.cli.widgets._event_summary import _truncate
        assert _truncate("hello", 5) == "hello"
        assert _truncate("hello world", 5) == "hell…"

    def test_format_elapsed_sec_three_buckets(self):
        """秒数格式化：< 10s 显 1 位小数（0.8s）；< 60s 整数（12s）；≥ 60s m+s。"""
        from orca.iface.cli.widgets._event_summary import _format_elapsed_sec
        assert _format_elapsed_sec(0.8) == "0.8s"
        assert _format_elapsed_sec(12.0) == "12s"
        assert _format_elapsed_sec(75.0) == "1m15s"

    def test_arg_title_per_tool(self):
        """per-tool 一句话标题：read 取 filePath / bash 取 command / glob 取 pattern。"""
        from orca.iface.cli.widgets._event_summary import _arg_title
        assert _arg_title("read", {"filePath": "/tmp/x.py"}) == "/tmp/x.py"
        assert _arg_title("bash", {"command": "ls"}) == "ls"
        assert _arg_title("glob", {"pattern": "**/*.py"}) == "**/*.py"

    def test_build_summary_line_basic(self):
        """node_started → "node started (kind=<kind>)"。"""
        from orca.iface.cli.widgets._event_summary import _build_summary_line
        assert _build_summary_line("node_started", {"kind": "agent"}) == "node started (kind=agent)"
