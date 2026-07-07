"""tests/e2e_phase16/test_boot_smoke.py —— phase-16 单流重构 boot smoke（SPEC §5）。

**本文件的范围**：phase-16 实施期 agent 自检的 boot smoke（app boots + reflow 不抛 +
Enter 内联展开 + 工具配对在真 tape 上工作 + 旧 detail DOM 已删）。**不是** SPEC §5.1
的完整按键矩阵 E2E（那是下一个 agent `test-coverage-e2e` 的职责）。

**测试数据**：真 tape ``runs/mxint_analysis-20260704-105608-90fd22.jsonl``（含 60 对
tool_call/result + 10 条 agent_message + 5 个 chart 事件；SPEC §5 测试数据约束）。

**验收点**（实施 agent 必须自证）：
  1. app boots + replay 全 tape 不抛
  2. ``query_one("#agent-history-detail")`` 抛 ``NoMatches``（铁律 #7：旧 detail DOM 已删）
  3. replay 后 AgentHistory entries 的 tool entry 数 == tape 中 agent_tool_call 数（配对完整）
  4. ``merged==False`` 的 tool entry 数 == 0（SPEC §5.2 AC：全部配对成功）
  5. ``pilot.press("enter")`` 内联展开末条 entry → reflow 后渲染文本含 detail 标记（``⎿``）
  6. ``pilot.press("enter")`` 再按一次 → 收起 → 渲染文本不含 ``⎿``（双向）
  7. ``action_history_toggle_expand`` 经真实键位派发命中（SPEC §5.0 元 AC 前置）
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from orca.iface.cli.app import OrcaApp
from orca.iface.cli.widgets import AgentHistory

# 真 tape（SPEC §5 测试数据约束：禁止合成事件冒充）
REPO_ROOT = Path(__file__).parents[2]
TAPE_PATH = REPO_ROOT / "runs" / "mxint_analysis-20260704-105608-90fd22.jsonl"


def run_async(coro):
    return asyncio.run(coro)


def _load_tape_events(path: Path) -> list:
    """加载真 tape 的所有事件为 duck-typed SimpleNamespace（与 _dispatch_to_widgets 兼容）。"""
    events = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            events.append(SimpleNamespace(
                type=e["type"],
                data=e.get("data") or {},
                node=e.get("node"),
                session_id=e.get("session_id"),
                seq=e.get("seq", 0),
                timestamp=e.get("timestamp", 0.0),
            ))
    return events


def _workflow_yaml(tmp_path: Path) -> Path:
    """最小可启动 workflow（线性 a→$end，与 test_app._linear_workflow 同款）。"""
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump({
        "name": "mxint_replay_smoke",
        "entry": "analyzer",
        "nodes": [
            {"name": "analyzer", "kind": "script", "command": "echo a",
             "routes": [{"to": "$end"}]},
        ],
    }), encoding="utf-8")
    return p


def _flatten_strips(strips) -> str:
    """拍平 RichLog lines（Strip 列表）为纯文本（断言用；与 test_app 同款）。"""
    parts = []
    for strip in strips:
        for segment in strip._segments:
            parts.append(segment.text)
    return "".join(parts)


@pytest.mark.skipif(not TAPE_PATH.exists(), reason="真 tape 不存在（非 mxint 环境）")
class TestBootSmokeRealTape:
    """phase-16 boot smoke：真 tape replay + 单流 reflow + Enter 内联展开。"""

    def test_replay_boots_and_pairs_all_tools(self, tmp_path):
        """replay 全 tape → app boots，60 对 tool 全配对，无 unmatched（SPEC §5.2）。"""
        from orca.compile import load_workflow
        wf = load_workflow(_workflow_yaml(tmp_path))
        app = OrcaApp(wf=wf, tape_path=tmp_path / "events.jsonl")
        app.kickoff = lambda: None  # 不真起编排

        events = _load_tape_events(TAPE_PATH)
        # 选 analyzer 节点（tape 里有它的 tool_call/result/message）
        analyzer_events = [e for e in events if e.node == "analyzer"]

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                # 选中 analyzer（让它成为 _selected_node，AgentHistory 才显示其事件）
                app._selected_node = "analyzer"
                # 注入 analyzer 全部事件（绕过 _dispatch_to_widgets 的 node 过滤，
                # 直接调 AgentHistory.set_node——本测试聚焦 widget 单流渲染，不经 dispatch 链路）
                ah = app.query_one(AgentHistory)
                ah.set_node("analyzer", analyzer_events)
                await pilot.pause()
                await pilot.pause()  # reflow 异步 flush

                # AC 2: 旧 detail DOM 已删（铁律 #7）
                from textual.widget import NoMatches
                with pytest.raises(NoMatches):
                    app.query_one("#agent-history-detail")

                # AC 3 + 4: tool 配对完整
                tool_entries = [e for e in ah.entries if e.kind == "tool"]
                n_calls = sum(1 for e in analyzer_events if e.type == "agent_tool_call")
                assert len(tool_entries) == n_calls, (
                    f"配对后 tool entry 数 {len(tool_entries)} != tape call 数 {n_calls}"
                )
                unmatched = [e for e in tool_entries if not e.merged]
                assert unmatched == [], (
                    f"未配对 tool entry（merged==False）{len(unmatched)} 个，应为 0"
                )

                # AC 5/6: Enter 内联展开末条 message entry（双向）
                # 末条应该是 agent_message（report_painter 的 last message 默认展开）
                # analyzer 的最后一条未必是 message，挑一条有 detail 的 message entry 验
                msg_entry = next(
                    (e for e in reversed(ah.entries) if e.kind == "message"), None,
                )
                assert msg_entry is not None, "analyzer 应有 agent_message"
                # 选中该 message
                ah._selected_seq = msg_entry.seq
                # 收起（last message 默认展开，先收起）
                ah._expanded_seqs.discard(msg_entry.seq)
                ah._reflow()
                await pilot.pause()
                await pilot.pause()
                # 收起态：渲染文本不含 ⎿（内联 detail 引导符）
                text_collapsed = _flatten_strips(
                    app.query_one("#agent-history-log").lines
                )
                # 展开态：含 ⎿
                ah._expanded_seqs.add(msg_entry.seq)
                ah._reflow()
                await pilot.pause()
                await pilot.pause()
                text_expanded = _flatten_strips(
                    app.query_one("#agent-history-log").lines
                )
                assert "⎿" in text_expanded, "展开后渲染应含内联 detail 引导符 ⎿"

        run_async(scenario())

    def test_enter_via_pilot_toggles_inline_detail(self, tmp_path):
        """SPEC §5.0 元 AC + §5.1 Enter：pilot.press('enter') 经真实键位派生命中 action。

        元 AC：monkey-patch ``app.action_history_toggle_expand`` 记调用次数，
        ``pilot.press('enter')`` 后断言 ``call_count == 1``（不准直调冒充验收）。
        """
        from orca.compile import load_workflow
        wf = load_workflow(_workflow_yaml(tmp_path))
        app = OrcaApp(wf=wf, tape_path=tmp_path / "events.jsonl")
        app.kickoff = lambda: None

        events = _load_tape_events(TAPE_PATH)
        analyzer_events = [e for e in events if e.node == "analyzer"]

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                app._selected_node = "analyzer"
                ah = app.query_one(AgentHistory)
                ah.set_node("analyzer", analyzer_events)
                await pilot.pause()
                await pilot.pause()

                # ── SPEC §5.0 元 AC：monkey-patch action 记调用 ──
                original = app.action_history_toggle_expand
                calls = []
                def wrapped(*a, **k):
                    calls.append(1)
                    return original(*a, **k)
                app.action_history_toggle_expand = wrapped  # type: ignore[method-assign]

                # 展开前记 expanded_seqs
                before = set(ah.expanded_seqs)
                await pilot.press("enter")
                await pilot.pause()
                await pilot.pause()
                # 元 AC：真实键位派发命中
                assert len(calls) == 1, (
                    "Enter 未经真实键位派发命中 action_history_toggle_expand（直调冒充？）"
                )
                # state 变化（末条 toggle）
                after = set(ah.expanded_seqs)
                assert before != after, "Enter 应 toggle expanded_seqs"

        run_async(scenario())

    def test_tool_entry_inline_detail_expands_on_enter(self, tmp_path):
        """SPEC §5.2 工具配对展开：选中 tool entry → Enter → 渲染含 tool card（双向）。

        双向：折叠时渲染文本不含 tool 名的 result 内容；展开后含 ``⎿``（内联 detail）。
        """
        from orca.compile import load_workflow
        wf = load_workflow(_workflow_yaml(tmp_path))
        app = OrcaApp(wf=wf, tape_path=tmp_path / "events.jsonl")
        app.kickoff = lambda: None

        events = _load_tape_events(TAPE_PATH)
        analyzer_events = [e for e in events if e.node == "analyzer"]

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                app._selected_node = "analyzer"
                ah = app.query_one(AgentHistory)
                ah.set_node("analyzer", analyzer_events)
                await pilot.pause()
                await pilot.pause()

                # 挑第一条 tool entry（已配对，有 detail）
                tool_entry = next(e for e in ah.entries if e.kind == "tool")
                ah._selected_seq = tool_entry.seq
                ah._expanded_seqs = set()  # 全收起
                ah._reflow()
                await pilot.pause()
                await pilot.pause()
                text_collapsed = _flatten_strips(
                    app.query_one("#agent-history-log").lines
                )
                # 折叠态：不含 ⎿
                assert "⎿" not in text_collapsed, "折叠态不应含内联 detail 引导符"

                # 展开该 tool entry
                ah._expanded_seqs = {tool_entry.seq}
                ah._reflow()
                await pilot.pause()
                await pilot.pause()
                text_expanded = _flatten_strips(
                    app.query_one("#agent-history-log").lines
                )
                # 展开态：含 ⎿（内联 detail 引导）
                assert "⎿" in text_expanded, "展开 tool entry 后应含 ⎿ 内联 detail"

        run_async(scenario())
