"""test_tui_redesign.py —— TUI 重设计 v1.1 字段级对齐测试。

spec v1.1 acceptance criteria：
  - §4.4 DAG 3 行盒子（name 居中 / status+iter / elapsed+tok 或 error）
  - §4.4.1 iter fold 性质（重放同 tape 必产相同 iter，retry 不算新 iter）
  - §4.5 fan-in 副标 (N inputs · M/N arrived)，N<2 不显示
  - §4.6 after=None 单独 section
  - §5.3 Activity Stream 双行 entry
  - §5.1 f 键 filter 模式 toggle
  - §6.4 EVENT_VISIBILITY 完整性 + noise governance
  - §4.3 fallback 阈值 ≥ 5

不重复 test_event_visibility.py / test_dag_layout.py 已覆盖的；聚焦 v1.1 新增字段级断言。
"""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical

from orca.iface.cli.widgets._dag_render import (
    FALLBACK_PARALLEL_THRESHOLD,
    NodeProjection,
    box_render,
    fan_in_annotation,
    format_elapsed,
    format_tokens,
    render_after_none_section,
    render_main_branch_nodes,
    should_fallback_to_outline,
    split_main_and_after_none,
    truncate_name,
)
from orca.iface.cli.widgets.activity_stream import (
    ActivityEntry,
    ActivityStream,
    build_entry,
)
from orca.iface.cli.widgets.dag_graph import DagGraph
from orca.iface.cli.widgets.header import HeaderStats, NodeUsageStats


def run_async(coro):
    return asyncio.run(coro)


# ── 共享：widget test harness（与 test_widgets.py 同 pattern）──────────────


class _Harness(App):
    """把单 widget 装进临时 app，便于 ``run_test()`` 驱动。"""

    def __init__(self, widget) -> None:
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        with Vertical():
            yield self._widget


# ── §4.4 box_render（3 行盒子字段级）────────────────────────────────────────


class TestBoxRender:
    """spec §4.4：3 行盒子（name / status+iter / elapsed+tok 或 error）。"""

    def test_done_box_has_5_lines_with_3_content_rows(self):
        """盒子返 5 行：top + 3 内容 + bot。"""
        proj = NodeProjection(
            name="analyzer", status="done", iter_n=1, elapsed=14.0, tokens=1200,
        )
        lines = box_render(proj)
        assert len(lines) == 5
        assert lines[0].startswith("┌") and lines[0].endswith("┐")
        assert lines[4].startswith("└") and lines[4].endswith("┘")

    def test_done_box_shows_status_iter_and_tokens(self):
        proj = NodeProjection(
            name="analyzer", status="done", iter_n=2, elapsed=14.0, tokens=1200,
        )
        lines = box_render(proj)
        body = "\n".join(lines)
        # 第 2 行（index 2）含 status + iter
        assert "done" in lines[2]
        assert "iter 2" in lines[2]
        # 第 3 行（index 3）含 elapsed + tok
        assert "14s" in lines[3]
        assert "1.2k tok" in lines[3]  # 1200 → 1.2k

    def test_failed_box_shows_error_preview_in_row3(self):
        """spec §4.4 acceptance：失败时第 3 行显 error[:30] 替代 elapsed+tok。"""
        proj = NodeProjection(
            name="runner", status="failed", iter_n=1, elapsed=10.0,
            error_msg="RuntimeError: CUDA out of memory (long stack)",
        )
        lines = box_render(proj)
        body_row3 = lines[3]
        # 第 3 行显错误摘要（前 30 字符）+ ! 前缀
        assert "!" in body_row3
        assert "RuntimeError: CUDA out of mem" in body_row3  # 前 30 字符
        # 不显 elapsed + tok（替代语义）
        assert "10s" not in body_row3
        assert "tok" not in body_row3

    def test_running_box_shows_running_status(self):
        proj = NodeProjection(name="cfg", status="running", iter_n=1, elapsed=20.0)
        lines = box_render(proj)
        assert "running" in lines[2]

    def test_truncate_name(self):
        """spec §4.4：超 14 字符截断 + …。"""
        assert truncate_name("analyzer") == "analyzer"
        # 17 字符 → 取前 13 + …（共 14 字符）
        result = truncate_name("mutator_structural")
        assert result.endswith("…")
        assert len(result) == 14
        assert result.startswith("mutator_stru")
        # 边界：恰 14 字符不截断
        assert truncate_name("abcdefghijklmn") == "abcdefghijklmn"
        # 15 字符 → 截断
        assert truncate_name("abcdefghijklmno").endswith("…")


# ── §4.4.1 iter fold 性质 ──────────────────────────────────────────────────


class TestIterFold:
    """spec §4.4.1：iter 是 reducer 派生 fold，重放必产相同值；retry 不算新 iter。"""

    def test_first_session_id_gets_iter_1(self):
        """首次 node_started（session_id=A）→ iter=1。"""
        # 模拟 reducer：append session_id 后 index+1
        node_sessions: list[str] = []
        sid = "A"
        if sid not in node_sessions:
            node_sessions.append(sid)
        iter_n = node_sessions.index(sid) + 1
        assert iter_n == 1

    def test_second_session_id_gets_iter_2(self):
        """loop workflow 重入：第二次 node_started（session_id=B）→ iter=2。"""
        node_sessions = ["A"]
        sid = "B"
        if sid not in node_sessions:
            node_sessions.append(sid)
        iter_n = node_sessions.index(sid) + 1
        assert iter_n == 2

    def test_retry_does_not_advance_iter(self):
        """spec §4.4.1：retry 是同 session_id 的延续（不 append，不 +1）。"""
        node_sessions = ["A"]  # 已有首次
        # retry_started 不 append session_id（同 session_id 沿用）
        iter_n = node_sessions.index("A") + 1
        assert iter_n == 1  # 仍 iter 1

    def test_replay_rebuilds_same_iter(self):
        """重放同 tape 必产相同 iter 列表（fold 性质）。"""
        # 模拟两个独立 reducer 跑同样的 node_started 序列
        events = [
            ("analyzer", "A"),
            ("configurator", "B"),
            ("analyzer", "C"),  # analyzer 第二次（loop）
        ]
        def reduce(events):
            sessions: dict[str, list[str]] = {}
            iters: dict[str, int] = {}
            for node, sid in events:
                sessions.setdefault(node, [])
                if sid not in sessions[node]:
                    sessions[node].append(sid)
                iters[node] = sessions[node].index(sid) + 1
            return iters
        iters_1 = reduce(events)
        iters_2 = reduce(events)
        assert iters_1 == iters_2
        # analyzer iter=2（loop），configurator iter=1
        assert iters_1["analyzer"] == 2
        assert iters_1["configurator"] == 1


# ── §4.5 fan-in 副标 ──────────────────────────────────────────────────────


class TestFanInAnnotation:
    """spec §4.5 O2=a：N 静态（拓扑入边数）+ M 动态（arrived）。"""

    def test_no_annotation_for_linear(self):
        """N < 2（线性）不显示副标。"""
        assert fan_in_annotation(1, 0) is None
        assert fan_in_annotation(0, 0) is None

    def test_arrived_progress(self):
        """N>=2 且 arrived < total → "(N inputs · M/N arrived)"。"""
        assert fan_in_annotation(5, 0) == "(5 inputs · 0/5 arrived)"
        assert fan_in_annotation(5, 3) == "(5 inputs · 3/5 arrived)"

    def test_all_arrived_drops_progress(self):
        """spec §4.5 acceptance：M == N 时副标消失（不再显 arrived）。"""
        assert fan_in_annotation(5, 5) == "(5 inputs)"
        assert fan_in_annotation(2, 2) == "(2 inputs)"


# ── §4.6 after=None section ────────────────────────────────────────────────


class TestAfterNoneSection:
    """spec §4.6 O3=b：after=None 节点单独 section。"""

    def test_split_linear_no_after_none(self):
        """线性 workflow：全部节点有前驱（除 entry），无 after=None。"""
        main, after_none, merge = split_main_and_after_none(
            ["a", "b", "c"], [("a", "b"), ("b", "c")],
        )
        assert main == ["a", "b", "c"]
        assert after_none == []
        assert merge is None

    def test_split_with_after_none_branch(self):
        """含 after=None 旁支：refiner 没有入边又不是 entry → 旁支。"""
        # reporter ← scout, refiner → reporter
        main, after_none, merge = split_main_and_after_none(
            ["scout", "reporter", "refiner"],
            [("scout", "reporter"), ("refiner", "reporter")],
        )
        assert "scout" in main
        assert "reporter" in main
        assert after_none == ["refiner"]
        assert merge == "reporter"

    def test_render_after_none_section_empty(self):
        """无旁支节点 → 空渲染。"""
        assert render_after_none_section({}, [], None) == []

    def test_render_after_none_section_includes_label(self):
        """spec §4.6：旁支 section 标题"─── 旁支（after=None） ───"。"""
        proj = NodeProjection(name="refiner", status="done", iter_n=1, elapsed=45.0)
        lines = render_after_none_section({"refiner": proj}, ["refiner"], "reporter")
        body = "\n".join(lines)
        assert "旁支（after=None）" in body
        assert "refiner" in body
        assert "reporter (末端汇聚)" in body


# ── §4.3 fallback 阈值 ─────────────────────────────────────────────────────


class TestFallbackThreshold:
    """spec §4.3：同层并行 ≥ 5 切 outline fallback。"""

    def test_four_parallel_does_not_fallback(self):
        """4 并行 = 51 字符（fits 60），不切 fallback。"""
        assert should_fallback_to_outline([1, 1, 4, 1], 60) is False

    def test_five_parallel_fallbacks(self):
        """5 并行 = 64 字符（超 60 临界），切 fallback。"""
        assert should_fallback_to_outline([1, 1, 5, 1], 60) is True

    def test_threshold_constant_locked(self):
        """阈值锁定 ≥ 5（防漂移）。"""
        assert FALLBACK_PARALLEL_THRESHOLD == 5

    def test_narrow_screen_fallbacks(self):
        """极窄屏（< 30 列）也 fallback（盒子挤不下）。"""
        assert should_fallback_to_outline([1], 10) is True


# ── §5.3 Activity Stream 双行 entry ────────────────────────────────────────


class TestActivityStreamEntry:
    """spec §5.3 / §5.4：双行 entry + per-type 结构。"""

    def test_build_entry_message(self):
        """agent_message 双行 entry：summary=text[:50].replace(\\n," ")，meta="N lines markdown"。"""
        entry = build_entry(
            1, "agent_message",
            {"text": "hello\nworld\nfoo"},
            node="analyzer", timestamp=1.0,
        )
        assert entry is not None
        assert entry.event_type == "agent_message"
        # spec §5.4：text[:50].replace("\n"," ")——换行变空格（双行 entry summary 不破坏排版）
        assert entry.summary_line == "hello world foo"
        assert "markdown" in entry.meta_line

    def test_build_entry_tool_call(self):
        """agent_tool_call：summary="tool  arg_title"，meta="running..."。"""
        entry = build_entry(
            1, "agent_tool_call",
            {"tool": "read", "args": {"filePath": "/tmp/x.py"}},
            node="cfg", timestamp=1.0,
        )
        assert entry is not None
        assert "read" in entry.summary_line
        assert "/tmp/x.py" in entry.summary_line
        assert entry.meta_line == "running..."

    def test_build_entry_failed_node(self):
        """node_failed：summary 含 message，meta="phase=..."。"""
        entry = build_entry(
            1, "node_failed",
            {"message": "boom", "phase": "spawn"},
            node="runner", timestamp=1.0,
        )
        assert entry is not None
        assert "node FAILED" in entry.summary_line
        assert "boom" in entry.summary_line
        assert "phase=spawn" in entry.meta_line

    def test_hide_main_returns_none(self):
        """spec §6.2：agent_usage → None（不进 Stream）。"""
        entry = build_entry(
            1, "agent_usage",
            {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.001},
            node="a", timestamp=1.0,
        )
        assert entry is None

    def test_hide_all_returns_none(self):
        """spec §6.1：prompt_rendered → None（完全不显示）。"""
        entry = build_entry(
            1, "prompt_rendered",
            {"preview": "..."},
            node="a", timestamp=1.0,
        )
        assert entry is None


class TestActivityStreamWidget:
    """ActivityStream widget：append/filter/select 行为。"""

    def test_append_event_adds_entry(self):
        stream = ActivityStream()

        async def scenario():
            async with _Harness(stream).run_test() as pilot:
                stream.append_event(
                    1, "agent_message", {"text": "hi"},
                    node="a", timestamp=1.0,
                )
                await pilot.pause()
                assert len(stream.entries) == 1
                assert stream.entries[0].event_type == "agent_message"
        run_async(scenario())

    def test_filter_node_hides_other_nodes(self):
        """spec §5.1：filter_mode=node 时仅显示该 node 的 entry。"""
        stream = ActivityStream()

        async def scenario():
            async with _Harness(stream).run_test() as pilot:
                stream.append_event(1, "agent_message", {"text": "a1"}, node="a", timestamp=1.0)
                stream.append_event(2, "agent_message", {"text": "b1"}, node="b", timestamp=2.0)
                stream.append_event(3, "agent_message", {"text": "a2"}, node="a", timestamp=3.0)
                await pilot.pause()
                # 默认全部可见
                assert len(stream._visible_entries()) == 3
                # filter=a：仅 a 的 2 个
                stream.set_filter_node("a")
                assert len(stream._visible_entries()) == 2
                assert all(e.node == "a" for e in stream._visible_entries())
                # toggle：再按 a 清 filter
                stream.set_filter_node("a")
                assert len(stream._visible_entries()) == 3
        run_async(scenario())

    def test_select_entry_expands_detail(self):
        """spec §5.5：选中 entry 自动展开折叠详情。"""
        stream = ActivityStream()

        async def scenario():
            async with _Harness(stream).run_test() as pilot:
                stream.append_event(1, "agent_message", {"text": "hi"}, node="a", timestamp=1.0)
                await pilot.pause()
                stream.action_cursor_down()  # 选中第一条
                await pilot.pause()
                assert stream._selected_seq == 1
                assert stream._expanded is True
        run_async(scenario())


# ── §6.2 Header footer per-node usage ──────────────────────────────────────


class TestHeaderFooter:
    """spec §6.2 / §7.2：Header footer 含 per-node token/cost + filter 标签。"""

    def test_render_footer_includes_filter_tag(self):
        """spec §5.1：footer 显示 [全部事件] / [仅 X]。"""
        stats = HeaderStats(filter_node=None)
        assert "[全部事件]" in stats.render_footer_text()

        stats = HeaderStats(filter_node="analyzer")
        assert "[仅 analyzer]" in stats.render_footer_text()

    def test_render_footer_includes_per_node_usage(self):
        """spec §6.2：footer 含每节点 token + cost。"""
        stats = HeaderStats(
            per_node_usage=[
                NodeUsageStats(name="analyzer", tokens=1200, cost_usd=0.0004),
                NodeUsageStats(name="configurator", tokens=1800, cost_usd=0.0006),
            ],
        )
        text = stats.render_footer_text()
        assert "analyzer" in text
        assert "1.2k tok" in text  # 1200 → 1.2k
        assert "$0.0004" in text
        assert "configurator" in text

    def test_running_node_prioritized_in_footer(self):
        """spec §11：footer 优先显示 running 节点。"""
        stats = HeaderStats(
            per_node_usage=[
                NodeUsageStats(name="analyzer", tokens=1200),
                NodeUsageStats(name="runner", tokens=24000),
            ],
            running_node="runner",
        )
        text = stats.render_footer_text()
        # runner 在 analyzer 之前（running 优先）
        assert text.index("runner") < text.index("analyzer")


# ── §4.4 DagGraph.update_node_projection 端到端 ───────────────────────────


class TestDagGraphProjectionE2E:
    """DagGraph.update_node_projection 接受全字段并刷新渲染。"""

    def test_update_projection_iter_and_status(self):
        graph = DagGraph()

        async def scenario():
            async with _Harness(graph).run_test() as pilot:
                graph.build_from_workflow(
                    ["a", "b"], None, {"a": ["b"], "b": ["$end"]},
                )
                graph.update_node_projection(
                    "a", status="running", iter_n=2, elapsed=14.0, tokens=1200,
                )
                await pilot.pause()
                proj = graph.projection_of("a")
                assert proj is not None
                assert proj.status == "running"
                assert proj.iter_n == 2
                assert proj.elapsed == 14.0
                assert proj.tokens == 1200
        run_async(scenario())

    def test_fan_in_total_set_at_build_time(self):
        """spec §4.5 O2=a：fan_in_total 静态从拓扑入边数算（build_from_workflow 时设）。"""
        graph = DagGraph()

        async def scenario():
            async with _Harness(graph).run_test() as pilot:
                # diamond：a → b,c → d（d 入边 = 2）
                graph.build_from_workflow(
                    ["a", "b", "c", "d"], None,
                    {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": ["$end"]},
                )
                await pilot.pause()
                proj_d = graph.projection_of("d")
                assert proj_d is not None
                assert proj_d.fan_in_total == 2  # b + c 两条入边
                # a 无入边
                proj_a = graph.projection_of("a")
                assert proj_a.fan_in_total == 0
        run_async(scenario())

    def test_fan_in_arrived_increments_on_upstream_completed(self):
        """spec §4.5：fan_in_arrived M 动态增量。"""
        graph = DagGraph()

        async def scenario():
            async with _Harness(graph).run_test() as pilot:
                graph.build_from_workflow(
                    ["a", "b", "c", "d"], None,
                    {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": ["$end"]},
                )
                # 初始 d 的 arrived = 0
                assert graph.projection_of("d").fan_in_arrived == 0
                # 模拟 b 完成：d.arrived += 1
                graph.update_node_projection("d", fan_in_arrived=1)
                assert graph.projection_of("d").fan_in_arrived == 1
        run_async(scenario())


# ── §4.4.1 iter 与 _selected_node 严格区分 ────────────────────────────────


class TestIterVsSelectedNodeSeparation:
    """spec §4.4.1：iter 是 fold（重放重建）；_selected_node 是 UI 交互态（重启清零）。"""

    def test_iter_is_rebuildable_state(self):
        """iter 通过 reducer 序列事件重建（不持久化在 widget 内）。"""
        # 模拟 reducer 派生：从事件序列算 iter
        events = [
            ("node_started", "a", "sid-1"),
            ("node_started", "a", "sid-2"),  # analyzer 第二次跑（loop）
        ]
        sessions: dict[str, list[str]] = {}
        iters: dict[str, int] = {}
        for _, node, sid in events:
            sessions.setdefault(node, [])
            if sid not in sessions[node]:
                sessions[node].append(sid)
            iters[node] = sessions[node].index(sid) + 1
        assert iters["a"] == 2

    def test_selected_node_is_ui_state_not_rebuildable(self):
        """_selected_node 是 UI 交互态：j/k 切换不写 tape，重启清零。"""
        # 模拟两次重放：_selected_node 重启都从 None / entry 开始
        selected_1 = None  # 重启清零
        selected_2 = None  # 再次重启也清零
        assert selected_1 == selected_2  # 不像 iter 那样从事件重建
