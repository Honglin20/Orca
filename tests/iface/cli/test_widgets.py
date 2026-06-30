"""test_widgets.py —— TUI widget 渲染逻辑单测（SPEC §6.2 / 计划 C4.6）。

用 Textual ``run_test()`` pilot（headless，CI 友好）。覆盖：
  - DagTree 5 种状态图标（✓✽⏸!○）映射正确
  - DagTree parallel 组：父 + 子 + 进度计数
  - LogStream ``format_event`` 格式（HH:MM:SS [session] <desc>）
  - Header stats 渲染（done/total/awaiting）
"""

from __future__ import annotations

import asyncio
import time

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical

from orca.iface.cli.widgets import (
    NODE_STATUS_ICONS,
    ActiveNode,
    DagTree,
    Header,
    LogStream,
)
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


# ── DagTree 状态图标 + parallel 组（SPEC §4.1）────────────────────────────


class TestDagTree:
    """DagTree：5 状态图标映射 + parallel 组父/子 + 进度计数。"""

    def test_build_from_nodes_all_pending(self):
        tree = DagTree()

        async def scenario():
            async with _Harness([tree]).run_test() as pilot:
                tree.build_from_workflow(["a", "b", "c"])
                await pilot.pause()
                assert tree.status_of("a") == "pending"
                # label 含 pending 图标
                assert "○ a" == tree.label_of("a")
        run_async(scenario())

    def test_set_status_updates_icon(self):
        tree = DagTree()

        async def scenario():
            async with _Harness([tree]).run_test() as pilot:
                tree.build_from_workflow(["a"])
                await pilot.pause()
                for status, icon in NODE_STATUS_ICONS.items():
                    tree.set_status("a", status)
                    await pilot.pause()
                    assert tree.label_of("a") == f"{icon} a"
        run_async(scenario())

    def test_set_status_idempotent(self):
        """SPEC §6.0 铁律 1：重放一致——多次同名同状态结果一致。"""
        tree = DagTree()

        async def scenario():
            async with _Harness([tree]).run_test() as pilot:
                tree.build_from_workflow(["a"])
                await pilot.pause()
                tree.set_status("a", "running")
                tree.set_status("a", "running")
                tree.set_status("a", "running")
                await pilot.pause()
                assert tree.label_of("a") == "✽ a"
        run_async(scenario())

    def test_unknown_status_ignored(self):
        tree = DagTree()

        async def scenario():
            async with _Harness([tree]).run_test() as pilot:
                tree.build_from_workflow(["a"])
                await pilot.pause()
                tree.set_status("a", "bogus")  # 防御：未知状态不崩
                await pilot.pause()
                assert tree.status_of("a") == "pending"  # 原状态保持
        run_async(scenario())

    def test_parallel_group_parent_children_and_progress(self):
        """SPEC §4.1：parallel 组为父节点 + branches 子节点 + 进度计数 (1/2)。"""
        tree = DagTree()

        async def scenario():
            async with _Harness([tree]).run_test() as pilot:
                tree.build_from_workflow(
                    node_names=["start", "end"],
                    parallel_groups=[("research", ["r_a", "r_b"])],
                )
                await pilot.pause()
                # 父组初始 pending + (0/2)
                assert "○ research (0/2)" == tree.label_of("research")
                # 子节点 pending
                assert "○ r_a" == tree.label_of("r_a")
                # r_a done → 进度 1/2
                tree.set_status("r_a", "done")
                tree.set_group_progress("research", done=1, total=2)
                await pilot.pause()
                assert "✓ r_a" == tree.label_of("r_a")
                assert "○ research (1/2)" == tree.label_of("research")
                # 组完成 → 父图标 done
                tree.set_status("r_b", "done")
                tree.set_group_progress("research", done=2, total=2)
                tree.set_group_status("research", "done")
                await pilot.pause()
                assert "✓ research (2/2)" == tree.label_of("research")
        run_async(scenario())

    def test_blocked_status_for_gate(self):
        """SPEC §4.1：blocked (⏸) 用于 gate 拦截的 node。"""
        tree = DagTree()

        async def scenario():
            async with _Harness([tree]).run_test() as pilot:
                tree.build_from_workflow(["review"])
                await pilot.pause()
                tree.set_status("review", "blocked")
                await pilot.pause()
                assert "⏸ review" == tree.label_of("review")
        run_async(scenario())


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
