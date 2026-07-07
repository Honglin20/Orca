"""test_widgets.py —— TUI widget 渲染逻辑单测（phase-12 SPEC §6.2 §6.4 §6.6）。

用 Textual ``run_test()`` pilot（headless，CI 友好）。覆盖：
  - AgentsList / AgentHistory：v2 三块布局 widget（Step 2/3 填充；Step 1a 仅占位 import）
  - LogStream ``format_event`` v2 单测在 ``tests/iface/cli/test_log_stream.py``
    （spec §2.4 Conductor Log View 风格 + EVENT_LEVEL 表派生）
  - Header stats 渲染（done/total/awaiting）
  - ChartPanel：同 label+title 幂等替换 / label 分组 / all_charts / 确定性 fold
  - ChartCanvas：7 chart_type 分派 / braille / 降级 / fail loud
  - NodeDetail：6 kind 永不空白 / ● 徽标 / executor-agnostic 流式
"""

from __future__ import annotations

import asyncio
import sys

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
    NodeDetail,
)
from orca.iface.cli.widgets.chart_panel import WORKFLOW_BUCKET
from orca.iface.cli.widgets.header import HeaderStats
# 注：LogStream ``format_event`` v2 单测拆到 ``tests/iface/cli/test_log_stream.py``
# （spec §2.4 Conductor Log View 风格 + EVENT_LEVEL 表派生；spec v2 §4.2 改造点）。


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
    """SPEC §4.1 + ADR §8.1：6 种状态图标（全覆盖 canonical Status Literal）。常量锁定防 drift。"""

    def test_six_icons_defined(self):
        # ADR §8.1 守门：icon 表 key 必须与 Status Literal 完全一致。
        assert set(NODE_STATUS_ICONS.keys()) == {
            "pending", "running", "done", "failed", "skipped", "blocked",
        }

    def test_icon_values_locked(self):
        # SPEC §4.1 明确：✓ done / ✽ running / ⏸ blocked / ! failed / ○ pending
        assert NODE_STATUS_ICONS["done"] == "✓"
        assert NODE_STATUS_ICONS["running"] == "✽"
        assert NODE_STATUS_ICONS["blocked"] == "⏸"
        assert NODE_STATUS_ICONS["failed"] == "!"
        assert NODE_STATUS_ICONS["pending"] == "○"
        # skipped（ADR §8.1 全覆盖要求，icon 表不漏 key）
        assert NODE_STATUS_ICONS["skipped"] == "⊘"


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
        """未知 name 静默忽略（防御；与 AgentsList._projections.get(name) 同语义）。"""
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
        #（绕过 textual 真起 app pilot；headless widget 单测同模式）。
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


class TestAgentHistory:
    """phase-16 单流 inline AgentHistory widget 单元测试（spec §2.3 + §5.1 + §5.4 + phase-16 SPEC §5）。

    INTENT（CLAUDE.md 铁律 9）：测试不是「set_node 不崩」，而是「用户能看到 agent
    历史流 + last message 默认展开 + 切换 agent 时正确 reset + 工具配对成一条」——
    断言 entries 内容、kind 派生、_expanded_seqs 状态、tool_call+result 配对 merged、
    就地升级保持 seq/位置、乱序回放集合相等（reducer fold）、shallow copy 防御。

    headless 测试策略（不真起 textual pilot）：直接实例化 AgentHistory 调
    set_node / append_event，跳过 textual RichLog 渲染（_log is None 时各 path
    防御 skip）；真渲染 + Enter 内联展开 E2E 留 tests/e2e_phase16/。
    """

    def _make_event(
        self,
        seq: int,
        etype: str,
        data: dict | None = None,
        *,
        node: str = "analyzer",
        timestamp: float | None = None,
    ):
        """构造 canonical Event 测试 fixture。"""
        from orca.schema import Event
        return Event(
            seq=seq,
            type=etype,
            timestamp=timestamp if timestamp is not None else 1000000.0 + seq,
            node=node,
            session_id="test-session",
            data=data or {},
        )

    def test_set_node_full_reflow(self):
        """set_node([3 events]) → entries 含 3 条 + node_name 正确（spec §2.3 set_node）。"""
        hist = AgentHistory()
        events = [
            self._make_event(1, "node_started", {"kind": "agent"}),
            self._make_event(2, "agent_message", {"text": "hello"}),
            self._make_event(3, "agent_tool_call",
                             {"tool": "read", "args": {}, "tool_call_id": "tc1"}),
        ]
        hist.set_node("analyzer", events)
        assert len(hist.entries) == 3
        assert hist.node_name == "analyzer"

    def test_set_node_resets_expanded_to_last_message(self):
        """set_node(A, [msg1]) → set_node(B, [msg10]) → 切回 A：expanded_seqs reset。

        spec §2.3：set_node 强制 reset _expanded_seqs 到当前 agent last message，
        避免上一个 agent 的 seq 残留污染。
        """
        hist = AgentHistory()
        events_a = [self._make_event(1, "agent_message", {"text": "A msg"})]
        events_b = [self._make_event(10, "agent_message", {"text": "B msg"})]
        hist.set_node("a", events_a)
        assert hist.expanded_seqs == {1}
        hist.set_node("b", events_b)
        assert hist.expanded_seqs == {10}  # reset to B's last message
        # 切回 A：expanded_seqs 应该是 A 的 last message（1），不是之前的 {10}
        hist.set_node("a", events_a)
        assert hist.expanded_seqs == {1}
        assert 10 not in hist.expanded_seqs

    def test_set_node_defensive_copy(self):
        """set_node 后修改传入 list → widget 内部不污染（reviewer P1-7 浅拷贝）。

        浅拷贝防御：app 层后续 _node_events[node].append(event) 不应影响 widget
        内部 _entries 列表。
        """
        hist = AgentHistory()
        events = [self._make_event(1, "agent_message", {"text": "msg"})]
        hist.set_node("analyzer", events)
        # 调用方修改原 list
        events.append(self._make_event(2, "agent_message", {"text": "extra"}))
        # widget 内部 _entries 不应被污染（仍然是 1 条）
        assert len(hist.entries) == 1

    def test_append_event_incremental(self):
        """append 1 event → entry 数 +1（不全 reflow，spec §3 增量追加）。"""
        hist = AgentHistory()
        events = [self._make_event(1, "node_started", {"kind": "agent"})]
        hist.set_node("analyzer", events)
        assert len(hist.entries) == 1
        hist.append_event(self._make_event(2, "agent_message", {"text": "msg"}))
        assert len(hist.entries) == 2

    def test_kind_derivation_per_event_type(self):
        """每 event_type → 正确 EntryKind（phase-16 §3.2 _HistEntry.kind 派生）。

        tool_call + tool_result 配对后归一类 ``"tool"``；message/thinking/other 各自分类。
        """
        hist = AgentHistory()
        events = [
            self._make_event(1, "agent_thinking", {"text": "..."}),
            self._make_event(2, "agent_tool_call",
                             {"tool": "read", "args": {}, "tool_call_id": "x"}),
            self._make_event(3, "agent_tool_result",
                             {"tool_call_id": "x", "result": "..."}),
            self._make_event(4, "agent_message", {"text": "msg"}),
            self._make_event(5, "human_decision_requested",
                             {"gate_id": "g1", "prompt": "?", "source": "x"}),
            self._make_event(6, "interrupt_requested", {"node": "analyzer"}),
        ]
        hist.set_node("analyzer", events)
        # phase-16 §2.2：tool_call+result 配对成一条 → 总 entry 数 == 5（不是 6）
        assert len(hist.entries) == 5
        kinds = [e.kind for e in hist.entries]
        assert kinds[0] == "thinking"
        assert kinds[1] == "tool"        # 配对后的 tool entry
        assert "tool" not in kinds[2:]   # 不再有第二条 tool entry
        assert kinds[2] == "message"
        assert kinds[3] == "other"       # gate
        assert kinds[4] == "other"       # interrupt

    def test_tool_pairing_in_place_upgrade_keeps_seq_and_position(self):
        """phase-16 §2.2 核心：tool_result 到达时**就地升级**对应 call entry。

        - call.seq 保持（merged.seq == call.seq）
        - 列表位置保持（不 remove+append，避免 _selected_seq dangling）
        - merged.tool_status == "completed"；merged.merged == True
        - 配对后 _entries 数 == call 数（每对一条）
        """
        hist = AgentHistory()
        events = [
            self._make_event(1, "agent_tool_call",
                             {"tool": "read", "args": {"filePath": "/a"}, "tool_call_id": "tc1"}),
            self._make_event(2, "agent_tool_call",
                             {"tool": "bash", "args": {"command": "ls"}, "tool_call_id": "tc2"}),
            self._make_event(10, "agent_tool_result",
                             {"tool_call_id": "tc1", "result": "A"}, timestamp=1000010.0),
            self._make_event(11, "agent_tool_result",
                             {"tool_call_id": "tc2", "result": "B"}, timestamp=1000011.0),
        ]
        hist.set_node("analyzer", events)
        # 配对后 2 条 tool entry（不是 4）
        tool_entries = [e for e in hist.entries if e.kind == "tool"]
        assert len(tool_entries) == 2
        # seq 保持 call 的 seq（1 / 2），位置在前面（不是 append 到末尾的 10/11）
        assert tool_entries[0].seq == 1
        assert tool_entries[1].seq == 2
        assert tool_entries[0].merged is True
        assert tool_entries[1].merged is True
        assert tool_entries[0].tool_status == "completed"
        # summary 含 result 派生的 elapsed（meta 行）
        assert "0.0s" in tool_entries[0].meta or "s" in tool_entries[0].meta

    def test_tool_pairing_no_unmatched_in_normal_replay(self):
        """phase-16 §5.2 AC：正常 replay 后 ``merged==False`` tool entry 数 == 0。

        所有 call 都应被 result 配对；降级即 fail loud（测试期语义，SPEC §0.2 #5）。
        """
        hist = AgentHistory()
        events = [
            self._make_event(1, "agent_tool_call",
                             {"tool": "read", "args": {}, "tool_call_id": "tc1"}),
            self._make_event(2, "agent_tool_result",
                             {"tool_call_id": "tc1", "result": "x"}),
        ]
        hist.set_node("analyzer", events)
        unmatched = [e for e in hist.entries if e.kind == "tool" and not e.merged]
        assert unmatched == [], "正常配对不应有 unmatched tool entry（降级即 fail）"

    def test_tool_result_without_call_buffered_in_pending(self):
        """phase-16 §5.6 + §7：result 无 call 配对 → 暂存 _pending_results（不降级独立 entry）。

        set_node 全量 fold 路径：乱序 result 不立即成 entry（保证 reducer fold 顺序无关）；
        无 tcid 的异常 result 才降级独立 entry（merged=False）。
        """
        hist = AgentHistory()
        # result 有 tcid 但无 call → 进 pending（不成独立 entry）
        events = [
            self._make_event(1, "agent_tool_result",
                             {"tool_call_id": "orphan", "result": "no call"}),
        ]
        hist.set_node("analyzer", events)
        # 不成独立 entry（暂存 pending，等 call）
        assert len(hist.entries) == 0
        assert "orphan" in hist._pending_results
        # 无 tcid 的异常 result → 降级独立 entry（兜底）
        hist2 = AgentHistory()
        hist2.set_node("a", [
            self._make_event(1, "agent_tool_result", {"result": "no tcid"}),
        ])
        assert len(hist2.entries) == 1
        assert hist2.entries[0].kind == "tool"
        assert hist2.entries[0].merged is False

    def test_append_orphan_result_degrades_not_silent(self):
        """phase-16 §0.2 #5 fail loud：append_event 路径 orphan result 降级独立 entry。

        增量路径不缓冲 pending（生产期事件流时序保证 call 先于 result）；若 result 无
        call 配对（translator 丢事件），降级为独立 entry显示而非静默吞——fail loud。
        """
        hist = AgentHistory()
        hist.set_node("analyzer", [])  # 空 entries
        orphan = self._make_event(
            1, "agent_tool_result",
            {"tool_call_id": "lost", "result": "no call arrived"},
        )
        hist.append_event(orphan)
        # 降级为独立 entry（merged=False），不静默丢
        assert len(hist.entries) == 1
        assert hist.entries[0].kind == "tool"
        assert hist.entries[0].merged is False
        assert "lost" not in hist._pending_results  # 增量路径不缓冲

    def test_tool_result_error_marks_failed_status(self):
        """phase-16 §2.3：result.error 或非零 exit_code → tool_status='failed'（✗ icon）。

        merged ToolEntry 的 tool_status 派生自 result.data：error 字段存在或 exit_code!=0
        即 failed；用于 summary 行 status icon（✓/…/✗）。
        """
        hist = AgentHistory()
        call = self._make_event(1, "agent_tool_call",
                                {"tool": "bash", "args": {"command": "x"},
                                 "tool_call_id": "tc1"})
        # result 带 error → failed
        result_err = self._make_event(2, "agent_tool_result",
                                      {"tool_call_id": "tc1", "result": "",
                                       "error": "command not found"})
        hist.set_node("a", [call, result_err])
        tool_entry = next(e for e in hist.entries if e.kind == "tool")
        assert tool_entry.tool_status == "failed"
        assert tool_entry.merged is True

        # result 带 exit_code != 0 → failed
        hist2 = AgentHistory()
        call2 = self._make_event(1, "agent_tool_call",
                                 {"tool": "bash", "args": {}, "tool_call_id": "tc2"})
        result_exit = self._make_event(2, "agent_tool_result",
                                       {"tool_call_id": "tc2", "result": "",
                                        "exit_code": 127})
        hist2.set_node("a", [call2, result_exit])
        tool_entry2 = next(e for e in hist2.entries if e.kind == "tool")
        assert tool_entry2.tool_status == "failed"

        # 正常 result → completed
        hist3 = AgentHistory()
        call3 = self._make_event(1, "agent_tool_call",
                                 {"tool": "bash", "args": {}, "tool_call_id": "tc3"})
        result_ok = self._make_event(2, "agent_tool_result",
                                     {"tool_call_id": "tc3", "result": "ok"})
        hist3.set_node("a", [call3, result_ok])
        tool_entry3 = next(e for e in hist3.entries if e.kind == "tool")
        assert tool_entry3.tool_status == "completed"

    def test_out_of_order_replay_set_equal(self):
        """phase-16 §5.6 reducer fold：逆序回放 → (seq, kind, summary) 集合与正序相等。

        配对/派生必须是 event list 的纯函数（顺序无关）。逆序时 result 早于 call，
        但 _fold_event 暂存 pending result，call 到达时补配对——集合输出不变。
        若不等 → 配对是顺序敏感的假 fold，铁律 #1 破。
        """
        call = self._make_event(5, "agent_tool_call",
                                {"tool": "read", "args": {}, "tool_call_id": "tc1"})
        result = self._make_event(6, "agent_tool_result",
                                  {"tool_call_id": "tc1", "result": "x"})
        msg = self._make_event(7, "agent_message", {"text": "done"})

        # 正序
        hist_fwd = AgentHistory()
        hist_fwd.set_node("a", [call, result, msg])

        # 逆序（result 早于 call）
        hist_rev = AgentHistory()
        hist_rev.set_node("a", [msg, result, call])

        # 集合相等：(seq, kind) 多重集一致（summary 可能因 elapsed 微差，比对 seq+kind）
        fwd_set = sorted((e.seq, e.kind) for e in hist_fwd.entries)
        rev_set = sorted((e.seq, e.kind) for e in hist_rev.entries)
        assert fwd_set == rev_set, (
            f"reducer fold 顺序无关失败：正序 {fwd_set} 逆序 {rev_set}"
        )
        # 两边都应：1 条 tool (seq=5, merged) + 1 条 message (seq=7)
        assert fwd_set == [(5, "tool"), (7, "message")]
        # 两边的 tool entry 都已 merged（pending 补配对）
        fwd_tool = next(e for e in hist_fwd.entries if e.kind == "tool")
        rev_tool = next(e for e in hist_rev.entries if e.kind == "tool")
        assert fwd_tool.merged is True
        assert rev_tool.merged is True

    def test_deterministic_replay_same_set(self):
        """phase-16 §5.6：同一 tape 正序回放两次 → (seq, kind, summary) 四元组逐项相等。"""
        events = [
            self._make_event(1, "agent_tool_call",
                             {"tool": "read", "args": {"filePath": "/x"}, "tool_call_id": "tc1"}),
            self._make_event(2, "agent_tool_result",
                             {"tool_call_id": "tc1", "result": "y"}, timestamp=1000002.0),
            self._make_event(3, "agent_message", {"text": "done"}),
        ]
        hist1 = AgentHistory()
        hist1.set_node("a", events)
        hist2 = AgentHistory()
        hist2.set_node("a", events)
        assert [(e.seq, e.kind, e.summary, e.meta) for e in hist1.entries] == \
               [(e.seq, e.kind, e.summary, e.meta) for e in hist2.entries]

    def test_in_place_upgrade_keeps_selected_seq_valid(self):
        """phase-16 §2.2：就地升级（不 remove+append）防止 _selected_seq dangling。

        场景：用户 ↓ 选中 call entry（seq=5），随后 result 到达。若 remove+append，
        _selected_seq=5 会指向已删 entry；就地升级保持 seq=5 → 选中仍有效。
        """
        hist = AgentHistory()
        call = self._make_event(5, "agent_tool_call",
                                {"tool": "read", "args": {}, "tool_call_id": "tc1"})
        hist.set_node("analyzer", [call])
        hist._selected_seq = 5  # 选中 call entry
        # result 到达（增量 append）
        result = self._make_event(6, "agent_tool_result",
                                  {"tool_call_id": "tc1", "result": "x"})
        hist.append_event(result)
        # 配对后原 call 位 seq 仍为 5（不 dangling）
        assert hist._selected_seq == 5
        tool_entries = [e for e in hist.entries if e.kind == "tool"]
        assert len(tool_entries) == 1
        assert tool_entries[0].seq == 5  # 就地升级保持 seq
        assert tool_entries[0].merged is True

    def test_last_message_default_expanded(self):
        """3 events 末尾 MSG → 默认展开（spec §2.3 + 用户核心需求）。"""
        hist = AgentHistory()
        events = [
            self._make_event(1, "agent_thinking", {"text": "..."}),
            self._make_event(2, "agent_tool_call",
                             {"tool": "read", "args": {}, "tool_call_id": "x"}),
            self._make_event(3, "agent_message", {"text": "final answer"}),
        ]
        hist.set_node("analyzer", events)
        assert hist.expanded_seqs == {3}  # last message seq 默认展开

    def test_new_message_replaces_expanded(self):
        """append 新 MSG → 旧 MSG 收起，新 MSG 展开（spec §2.3 自动跟随）。"""
        hist = AgentHistory()
        events = [self._make_event(1, "agent_message", {"text": "msg1"})]
        hist.set_node("analyzer", events)
        assert hist.expanded_seqs == {1}
        hist.append_event(self._make_event(2, "agent_message", {"text": "msg2"}))
        assert hist.expanded_seqs == {2}  # 替换，不是 add
        assert 1 not in hist.expanded_seqs

    def test_tool_call_cache_derives_elapsed_on_merge(self):
        """phase-16 §2.2：call + 2.5s 后 result → merged entry meta 含 elapsed（GAP-B/C）。

        phase-16 变化：result 不再独立 entry；elapsed 派生在 merged ToolEntry 上。
        """
        hist = AgentHistory()
        call_event = self._make_event(
            1, "agent_tool_call",
            {"tool": "read", "args": {"filePath": "/tmp/x.py"}, "tool_call_id": "tc1"},
            timestamp=1000.0,
        )
        result_event = self._make_event(
            2, "agent_tool_result",
            {"tool_call_id": "tc1", "result": "file content"},
            timestamp=1002.5,  # 2.5s 后
        )
        hist.set_node("analyzer", [call_event, result_event])
        # 配对后唯一 tool entry 的 meta 应含 "2.5s"
        tool_entry = next(e for e in hist.entries if e.kind == "tool")
        assert "2.5s" in tool_entry.meta
        assert tool_entry.merged is True

    def test_folded_detail_uses_phase15(self):
        """内联 detail 调 phase-15 render_tool / render_message（spec §5.5 复用契约）。"""
        hist = AgentHistory()
        hist.set_executor("claude")
        events = [
            self._make_event(
                1, "agent_tool_call",
                {"tool": "read", "args": {"filePath": "/tmp/x.py"}, "tool_call_id": "tc1"},
            ),
            self._make_event(2, "agent_message", {"text": "hello world"}),
        ]
        hist.set_node("analyzer", events)
        # tool entry 应该有内联详情（render_tool 输出）
        tool_entry = next(e for e in hist.entries if e.kind == "tool")
        assert tool_entry.detail is not None
        # message entry 也应该有内联详情（render_message 输出）
        msg_entry = next(e for e in hist.entries if e.kind == "message")
        assert msg_entry.detail is not None

    def test_no_message_no_expanded(self):
        """events 无 agent_message → expanded_seqs 为空（spec §2.3 默认展开规则）。"""
        hist = AgentHistory()
        events = [self._make_event(1, "agent_thinking", {"text": "..."})]
        hist.set_node("analyzer", events)
        assert hist.expanded_seqs == set()

    def test_set_node_clears_tool_call_cache(self):
        """set_node 切换 agent → _tool_call_cache 清空（per-node 隔离）。"""
        hist = AgentHistory()
        events = [
            self._make_event(
                1, "agent_tool_call",
                {"tool": "read", "args": {}, "tool_call_id": "tc1"},
            ),
        ]
        hist.set_node("a", events)
        assert "tc1" in hist._tool_call_cache
        # 切换到 agent b（无 events）
        hist.set_node("b", [])
        assert len(hist._tool_call_cache) == 0

    def test_set_executor_changes_normalize_table(self):
        """set_executor('opencode') → 后续 normalize_tool 用 opencode 后端。"""
        hist = AgentHistory()
        hist.set_executor("opencode")
        assert hist._executor == "opencode"
        # 默认 claude
        hist2 = AgentHistory()
        assert hist2._executor == "claude"

    def test_action_toggle_expand(self):
        """action_toggle_expand：Enter 键切换选中 entry 的展开状态（reviewer P0-6 Enter）。"""
        hist = AgentHistory()
        events = [
            self._make_event(1, "agent_thinking", {"text": "..."}),
            self._make_event(2, "agent_message", {"text": "msg"}),
        ]
        hist.set_node("analyzer", events)
        # 初始：seq=2 (last msg) 展开，seq=1 折叠
        assert hist.expanded_seqs == {2}
        # 选中 seq=1（thinking），按 Enter 展开
        hist._selected_seq = 1
        hist.action_toggle_expand()
        assert 1 in hist.expanded_seqs
        assert 2 in hist.expanded_seqs  # last message 不被影响
        # 再 Enter → 收起
        hist.action_toggle_expand()
        assert 1 not in hist.expanded_seqs
        assert 2 in hist.expanded_seqs  # last message 保留展开

    def test_action_cursor_down_up(self):
        """j/k 导航：action_cursor_down/up 切换 _selected_seq。"""
        hist = AgentHistory()
        events = [
            self._make_event(1, "agent_thinking", {"text": "..."}),
            self._make_event(2, "agent_message", {"text": "msg"}),
            self._make_event(3, "agent_tool_call",
                             {"tool": "read", "args": {}, "tool_call_id": "x"}),
        ]
        hist.set_node("analyzer", events)
        # 初始未选中
        assert hist._selected_seq is None
        # j → 选中第 1 条
        hist.action_cursor_down()
        assert hist._selected_seq == 1
        # j → 第 2 条
        hist.action_cursor_down()
        assert hist._selected_seq == 2
        # j → 第 3 条
        hist.action_cursor_down()
        assert hist._selected_seq == 3
        # k → 回到第 2 条
        hist.action_cursor_up()
        assert hist._selected_seq == 2

    def test_action_cursor_no_wrap_at_boundary(self):
        """j 在末条不 wrap / k 在首条不 wrap（边界防御）。"""
        hist = AgentHistory()
        events = [
            self._make_event(1, "agent_message", {"text": "first"}),
            self._make_event(2, "agent_message", {"text": "last"}),
        ]
        hist.set_node("analyzer", events)
        # 跳到末条
        hist._selected_seq = 2
        hist.action_cursor_down()  # 末条之后 → 不动（不 wrap 回首条）
        assert hist._selected_seq == 2
        # 跳回首条
        hist._selected_seq = 1
        hist.action_cursor_up()  # 首条之前 → 不动（不 wrap 到末条）
        assert hist._selected_seq == 1

    def test_action_cursor_empty_entries_noop(self):
        """entries 为空时 j/k 不抛（headless 防御；spec §2.3 set_node(None, []) 路径）。"""
        hist = AgentHistory()
        hist.set_node(None, [])
        # 空 entries：j/k 不抛、不改 _selected_seq
        hist.action_cursor_down()
        assert hist._selected_seq is None
        hist.action_cursor_up()
        assert hist._selected_seq is None

    def test_action_toggle_expand_defaults_to_last(self):
        """未选中 entry 时 Enter 默认作用于最后一条（修复「Enter 没反应」体感 bug）。

        旧逻辑：``_selected_seq is None`` → 直接 return（用户必须先 ↓ 选中才能 Enter，
        反直觉）。新逻辑：无选中时作用于最后一条 entry，直接按 Enter 即可收起/展开。
        """
        hist = AgentHistory()
        events = [
            self._make_event(1, "agent_thinking", {"text": "..."}),
            self._make_event(2, "agent_message", {"text": "msg"}),
        ]
        hist.set_node("analyzer", events)
        assert hist._selected_seq is None
        assert hist.expanded_seqs == {2}  # last msg 默认展开
        # Enter（无选中）→ 默认作用于最后一条 seq=2 → 收起
        hist.action_toggle_expand()
        assert hist.expanded_seqs == set()
        # 再 Enter → 展开
        hist.action_toggle_expand()
        assert hist.expanded_seqs == {2}
        # 光标未被移动（保持 None）
        assert hist._selected_seq is None

    def test_action_toggle_expand_empty_entries_noop(self):
        """空 entries（set_node(None, [])）时 Enter 不抛、不改状态（headless 防御）。

        锁定 action_toggle_expand 的空 entries 早 return 守卫为显式契约（A.1 改动了
        该分支，从隐式覆盖升为显式断言）。
        """
        hist = AgentHistory()
        hist.set_node(None, [])  # 空 entries
        assert hist._entries == []
        assert hist._selected_seq is None
        # Enter 不抛、不改 expanded_seqs / _selected_seq
        hist.action_toggle_expand()
        assert hist.expanded_seqs == set()
        assert hist._selected_seq is None

    def test_tool_call_cache_lru_cap(self):
        """tool_call_id cache 超 _TOOL_CALL_CACHE_CAP → 丢最旧（防爆内存）。"""
        from orca.iface.cli.widgets.agent_history import _TOOL_CALL_CACHE_CAP
        hist = AgentHistory()
        # 填 _TOOL_CALL_CACHE_CAP + 1 条 call（无 result，全部 unmatched）
        events = []
        for i in range(_TOOL_CALL_CACHE_CAP + 1):
            events.append(self._make_event(
                i + 1, "agent_tool_call",
                {"tool": "read", "args": {}, "tool_call_id": f"tc{i}"},
            ))
        hist.set_node("analyzer", events)
        # cache 不超 cap
        assert len(hist._tool_call_cache) <= _TOOL_CALL_CACHE_CAP
        # 最旧的（tc0）应该被丢
        assert "tc0" not in hist._tool_call_cache

    def test_node_name_none_empties_entries(self):
        """set_node(None, []) → 清空状态（spec §2.3）。"""
        hist = AgentHistory()
        events = [self._make_event(1, "agent_message", {"text": "msg"})]
        hist.set_node("analyzer", events)
        assert len(hist.entries) == 1
        # 切换到 None agent
        hist.set_node(None, [])
        assert hist.node_name is None
        assert len(hist.entries) == 0
        assert hist.expanded_seqs == set()


# spec v2 §2.4：LogStream ``format_event`` + widget 行为单测拆到独立文件
# ``tests/iface/cli/test_log_stream.py``（Conductor Log View 风格 + EVENT_LEVEL 表派生）。


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
