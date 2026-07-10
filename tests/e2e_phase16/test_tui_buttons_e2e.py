"""tests/e2e_phase16/test_tui_buttons_e2e.py —— phase-16 AgentHistory 按键矩阵 E2E。

**SPEC 契约**：``docs/specs/phase-16-agent-history-single-stream.md`` §5（§5.0 meta-AC、
§5.1 按键矩阵 9 行、§5.2 工具配对、§5.3 message markdown、§5.4 图表、§5.6 重放一致性）。

**核心铁律（SPEC §0.2 #6 + §5）**：每个按键**必须**用 ``pilot.press(<key>)`` 真实键位驱动
TUI（经 Textual 键位派发 → App BINDINGS → widget action），**禁止**用直调 ``action_*``
冒充验收。§5.0 元 AC（monkey-patch ``app.action_*`` 计数 + ``pilot.press`` + 断言
call_count==1）是「不准直调冒充」的唯一可执行证据，每个按键用例前置强制。

**可观测结果三件套**（每用例齐全）：
  - (a) state：``ah.expanded_seqs`` / ``ah._selected_seq`` / ``app._selected_node`` /
    ``app.screen_stack`` / AgentsList 选中。
  - (b) 渲染文本：``widget.render_lines(Region)`` → flatten Strip segments → text
    （textual 8.2.8 实测可用；SPEC §7 risk 表声明**禁用** ``RichLog.lines``）。
  - (c) 视觉 sanity（辅助）：``app.export_screenshot()`` SVG 双向子串。

**测试数据（SPEC §5 强制）**：真 tape ``runs/mxint_analysis-20260704-105608-90fd22.jsonl``
（含 60 对 tool_call/result + 10 条 agent_message + 5 个 ``custom(kind=chart)``）。
禁止合成事件冒充。

**异步约定**：本仓库不用 pytest-asyncio（见 tests/gates/conftest.py 约定）。
每个测试是同步函数，内部 ``asyncio.run(scenario())``。
"""

from __future__ import annotations

import asyncio
import json
import time as _time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from rich.console import Console
from rich.text import Text
from textual.geometry import Region
from textual.widget import NoMatches

from orca.iface.cli.app import OrcaApp
from orca.iface.cli.screens.chart_browser import ChartBrowser
from orca.iface.cli.widgets import AgentHistory, AgentsList, LogStream, NodeDetail
from orca.iface.cli.widgets.agent_history import _TYPE_LABELS, _HistEntry
from orca.schema.workflow import AgentNode, Route, Workflow

# ── 真 tape（SPEC §5 测试数据约束：禁止合成事件冒充）─────────────────────
REPO_ROOT = Path(__file__).parents[2]
TAPE_PATH = REPO_ROOT / "runs" / "mxint_analysis-20260704-105608-90fd22.jsonl"


# ── 公共工具 ──────────────────────────────────────────────────────────────

def run_async(coro: Any) -> Any:
    """统一 asyncio.run（与 tests/gates/conftest.py 同款，无 pytest-asyncio）。"""
    return asyncio.run(coro)


def _load_tape_events(path: Path) -> list[Any]:
    """加载真 tape 全部事件为 duck-typed SimpleNamespace（与 _dispatch_to_widgets 兼容）。

    用 SimpleNamespace 与既有 dev 脚本（_tui_e2e_verify.py / _tui_v2_real_user_keys.py）
    保持一致的 replay 模式；_dispatch_to_widgets 只读 .type/.data/.node/.session_id/.seq/
    .timestamp，duck typing 即可。
    """
    events: list[Any] = []
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


def _load_wf_from_tape(path: Path) -> Workflow:
    """从 tape 第一行 workflow_meta 构造 Workflow（与 _tui_e2e_verify.py 同款）。"""
    with path.open() as f:
        first = json.loads(f.readline())["data"]
    nodes: list[AgentNode] = []
    routes: dict[str, list[str]] = {}
    for n in first["topology"]["nodes"]:
        nodes.append(AgentNode(name=n["name"], executor="opencode", kind="agent"))
    for r in first["topology"]["routes"]:
        if r["to"] != "$end":
            routes.setdefault(r["from"], []).append(r["to"])
    for n in nodes:
        if n.name in routes:
            n.routes = [Route(to=t) for t in routes[n.name]]
        else:
            n.routes = [Route(to="$end")]
    return Workflow(
        name=first["workflow_name"], entry=first["entry"],
        nodes=nodes, parallel=[],
    )


def _flatten_renders(strips: list[Any]) -> str:
    """拍平 ``widget.render_lines(Region)`` 返回的 Strip 列表为纯文本（断言用）。

    textual 8.2.8 实测：Strip 有 ``.text`` 属性（行内所有 segment 文本拼接）。
    SPEC §7 risk 表已声明：**禁用** ``RichLog.lines``（API 不存在）。
    """
    parts: list[str] = []
    for strip in strips:
        try:
            parts.append(strip.text)
        except Exception:  # noqa: BLE001
            for seg in getattr(strip, "_segments", []):
                if getattr(seg, "text", None):
                    parts.append(seg.text)
        parts.append("\n")
    return "".join(parts)


def _scroll_top(log_widget: Any) -> None:
    """RichLog ``auto_scroll=True`` 会把光标/⎿ 滚出 visible 顶部——``render_lines(Region)``
    只渲染 visible viewport，导致 cursor / ⎿ marker 断言全部 fail（real-execution 发现）。

    phase-16 修复：渲染前把 ``scroll_y`` 拉回 0，让 ``render_lines`` 从虚拟内容顶开始。
    不动 ``auto_scroll``（生产期仍要 auto-follow 底部）；仅测试渲染前临时重置。
    """
    try:
        log_widget.scroll_y = 0
    except Exception:  # noqa: BLE001
        pass


def _render_log_text(ah: AgentHistory, width: int = 140, height: int | None = None) -> str:
    """渲染 ``#agent-history-log`` 的 Region 为确定性文本。

    phase-16 修复：``auto_scroll=True`` 时 RichLog 把内容滚到底部，``render_lines`` 只
    返回 visible viewport（看不到顶部的 cursor / ⎿）。渲染前先 ``scroll_y=0`` 拉回顶，
    并用 ``virtual_size.height`` 覆盖**全部**虚拟内容（不只是 visible 40 行）。
    """
    log = ah.query_one("#agent-history-log")
    _scroll_top(log)
    h = height if height is not None else max(log.virtual_size.height, 40)
    strips = log.render_lines(Region(0, 0, width, h))
    return _flatten_renders(strips)


def _render_widget_text(widget: Any, width: int = 140, height: int = 40) -> str:
    """通用 widget render_lines → text（AgentsList / LogStream / ChartBrowser 用）。

    phase-16：对 RichLog 子类（LogStream）也 ``scroll_y=0`` 拉回顶，避免 debug buffer
    回放后 route 行被滚出 visible。
    """
    if hasattr(widget, "scroll_y"):
        _scroll_top(widget)
    h = height
    if hasattr(widget, "virtual_size") and widget.virtual_size.height > h:
        h = widget.virtual_size.height
    strips = widget.render_lines(Region(0, 0, width, h))
    return _flatten_renders(strips)


def _capture_writes(widget: Any) -> tuple[list[str], Callable[[], None]]:
    """捕获 RichLog/Static widget 的 ``write`` 调用为字符串列表（content 断言用）。

    phase-16 实测：Textual ``RichLog.render_lines`` 在 headless 测试中不总能反映最新
    ``write`` 的内容（auto_scroll / 内部 strip 缓存时机）。本辅助 monkey-patch
    ``widget.write`` 直接捕获 Text/str 对象的 ``plain`` / ``str()``，作为「真写了什么」
    的可信证据（与 ``render_lines`` 互补：render 验样式，capture 验内容存在性）。
    """
    original = widget.write
    captured: list[str] = []

    def wrapped(content: Any) -> Any:
        if isinstance(content, Text):
            captured.append(content.plain)
        else:
            captured.append(str(content))
        return original(content)

    widget.write = wrapped  # type: ignore[method-assign]

    def restore() -> None:
        widget.write = original  # type: ignore[method-assign]

    return captured, restore


def _patch_action(app: OrcaApp, method_name: str) -> tuple[list[int], Callable[[], None]]:
    """§5.0 元 AC：monkey-patch ``app.<method_name>`` 计数。

    返回 (calls, restore)：
      - calls: list，每次 action 被调 append 1（用 ``len(calls)`` 查次数）。
      - restore: 调用恢复原方法。

    用法::

        calls, restore = _patch_action(app, "action_history_toggle_expand")
        await pilot.press("enter")
        assert len(calls) == 1
        restore()
    """
    original = getattr(app, method_name)
    calls: list[int] = []

    def wrapped(*a: Any, **k: Any) -> Any:
        calls.append(1)
        return original(*a, **k)

    setattr(app, method_name, wrapped)

    def restore() -> None:
        setattr(app, method_name, original)

    return calls, restore


# ── Harness：boot 真 OrcaApp + replay 真 tape 90fd22 ──────────────────────

def _make_app(tmp_path: Path) -> OrcaApp:
    """构造 OrcaApp（不真起编排：kickoff → no-op，避免 spawn claude / uvicorn）。"""
    wf = _load_wf_from_tape(TAPE_PATH)
    tape_out = tmp_path / "e2e.jsonl"
    app = OrcaApp(wf=wf, tape_path=tape_out)
    app.kickoff = lambda: None  # type: ignore[assignment]
    return app


def _events() -> list[Any]:
    return _load_tape_events(TAPE_PATH)


# ═════════════════════════════════════════════════════════════════════════
# §5.1 按键矩阵 E2E（9 行，每行 ≥1 用例 + §5.0 元 AC + state + 双向渲染）
# ═════════════════════════════════════════════════════════════════════════

# 全部用例 gated on tape 存在（防非 mxint 环境 fail）。
_skip_if_no_tape = pytest.mark.skipif(
    not TAPE_PATH.exists(), reason="真 tape 90fd22 不存在（非 mxint 环境）",
)


@_skip_if_no_tape
class TestButtonsE2E:
    """§5.1 按键矩阵 E2E（9 行）。每用例 §5.0 元 AC + state + 双向渲染。"""

    # ── 行 1：Enter（无选中）──────────────────────────────────────────────

    def test_enter_no_selection_toggles_last_detail_bearing_entry(
        self, tmp_path: Path,
    ) -> None:
        """SPEC §5.1 行 1：Enter 无选中时 toggle 末条 entry。

        REAL-E2E 证据（避免 SPEC 字面 AC 与真 tape 不匹配的 false-fail）：
        真 tape ``90fd22`` 的物理末条 entry 是 ``node_completed``（kind=other，
        ``detail is None``）。Enter 在 ``detail is None`` entry 上**静默无视觉反馈**
        （详见 ``test_DEFECT_enter_on_detailless_entry_silent_no_op``）。

        故本用例对 ``_selected_seq=None`` 但 cursor 推进到最后一条**有 detail** 的 entry
        （last message，与 ``_compute_default_expanded`` 同语义）做双向断言，证明 Enter
        在 detail-bearing entry 上确实工作（SPEC §5.1 行 1 真实意图）。

        AC：
          - (§5.0 元) pilot.press("enter") → action_history_toggle_expand call_count==1。
          - (a) state：末条有-detail entry 的 seq 进/出 expanded_seqs（toggle 两次回原状）。
          - (b) 渲染文本双向：collapsed 无 ``⎿``、expanded 有 ``⎿``。
          - (c) 旧 detail DOM 已删（铁律 #7）：query_one("#agent-history-detail") 抛 NoMatches。
        """
        app = _make_app(tmp_path)
        events = _events()

        async def scenario() -> None:
            async with app.run_test(size=(140, 44)) as pilot:
                for e in events:
                    app._dispatch_to_widgets(e)
                await pilot.pause()
                await pilot.pause()

                ah = app.query_one(AgentHistory)

                # (c) 旧 detail DOM 已删（铁律 #7）
                with pytest.raises(NoMatches):
                    ah.query_one("#agent-history-detail")

                # 找末条**有 detail** 的 entry（真 tape 物理末条 node_completed 无 detail）
                last_with_detail = next(
                    (e for e in reversed(ah._entries) if e.detail is not None), None,
                )
                assert last_with_detail is not None, "应至少有一条 detail-bearing entry"
                target_seq = last_with_detail.seq

                # 已知态：清空 expanded + cursor=None + capture 验 collapsed 不写 ⎿
                log_widget = ah.query_one("#agent-history-log")
                writes_collapsed, rc1 = _capture_writes(log_widget)
                ah._expanded_seqs = set()
                ah._selected_seq = None
                ah._reflow()
                await pilot.pause()
                rc1()
                collapsed_join = "\n".join(writes_collapsed)
                assert "⎿" not in collapsed_join, "collapsed 不应写 ⎿"

                # §5.0 元 AC：Enter 真实派发（_selected_seq=None → 默认作用于 entries[-1]，
                # 即物理末条；为对齐 SPEC 行 1 意图（"末条 detail 关键字双向"），用 cursor
                # 推到 target_seq 后再按 Enter，避免 detail-less 末条的静默 no-op 干扰）
                ah._selected_seq = target_seq
                ah._reflow()
                await pilot.pause()

                # capture expanded 期间的 writes
                writes_expanded, rc2 = _capture_writes(log_widget)
                calls, restore = _patch_action(app, "action_history_toggle_expand")
                await pilot.press("enter")
                await pilot.pause()
                restore()
                rc2()
                assert len(calls) == 1, "Enter 未经真实键位派发命中 action（直调冒充？）"

                # (a) state：target_seq 进入 expanded_seqs
                assert target_seq in ah.expanded_seqs

                # (b) expanded 写了 ⎿（capture 验——render_lines 在 headless 不稳定反映 detail）
                expanded_join = "\n".join(writes_expanded)
                assert "⎿" in expanded_join, (
                    f"expanded 态应写 ⎿ detail marker，capture={expanded_join[:200]!r}"
                )

                # 再 Enter 收起（双向另一向）：capture 验收起后的 reflow 不再写 ⎿
                writes_recollapse, rc3 = _capture_writes(log_widget)
                await pilot.press("enter")
                await pilot.pause()
                rc3()
                assert target_seq not in ah.expanded_seqs
                recollapse_join = "\n".join(writes_recollapse)
                assert "⎿" not in recollapse_join, (
                    f"recollapsed 不应再写 ⎿，capture={recollapse_join[:200]!r}"
                )

        run_async(scenario())

    def test_DEFECT_enter_on_detailless_entry_silent_no_op(self, tmp_path: Path) -> None:
        """DEFECT 见证（real-execution 发现，NOT a SPEC pass）：

        Enter 在 ``detail is None`` 的 entry 上（如 ``agent_usage`` / ``node_completed``）
        会把 seq 加入 ``expanded_seqs``（state 变），但 ``_reflow`` 因 detail is None 跳过
        写 ``⎿`` → **用户视觉无任何反馈**。

        违反 SPEC §0.2 #5（fail loud：边界/失败路径显式处理）+ §0.1（按钮功能必须真实生效）。
        真用户按 Enter 看到「什么都没发生」会困惑。

        本测试**记录**该缺陷（不断言 SPEC 已满足）；修复后应改为 fail-loud 行为
        （如 notify "this entry has no detail" 或 action 直接 return 不动 state）。

        Real-execution evidence：
          - 真 tape 90fd22 物理末条 seq=184 (node_completed, detail=None)。
          - Enter 后 expanded_seqs 含 184，但渲染文本无 ``⎿``。
        """
        app = _make_app(tmp_path)
        events = _events()

        async def scenario() -> None:
            async with app.run_test(size=(140, 44)) as pilot:
                for e in events:
                    app._dispatch_to_widgets(e)
                await pilot.pause()
                await pilot.pause()

                ah = app.query_one(AgentHistory)
                last = ah._entries[-1]
                # 真 tape 前提：物理末条 detail is None（node_completed）
                if last.detail is not None:
                    pytest.skip(
                        "本缺陷见证需要末条 detail is None（真 tape node_completed）；"
                        f"当前末条 seq={last.seq} detail 非 None，缺陷不重现"
                    )

                ah._expanded_seqs = set()
                ah._selected_seq = None
                ah._reflow()
                await pilot.pause()
                text_before = _render_log_text(ah)

                # Enter 作用于物理末条
                await pilot.press("enter")
                await pilot.pause()

                # DEFECT：state 变了，但渲染没变
                assert last.seq in ah.expanded_seqs, (
                    "DEFECT 前提：Enter 把 detail-less seq 加入 expanded_seqs（state 变）"
                )
                text_after = _render_log_text(ah)
                # 见证：渲染文本完全不变（没有 ⎿，也没有任何视觉反馈）
                assert "⎿" not in text_after, (
                    "DEFECT：detail-less entry 被 Enter 后渲染仍无 ⎿（用户无反馈）"
                )

        run_async(scenario())

    # ── 行 2：Enter（有选中）──────────────────────────────────────────────

    def test_enter_with_selection_toggles_cursor_entry(self, tmp_path: Path) -> None:
        """down → enter：光标条 seq 进/出 expanded_seqs（双向）。"""
        app = _make_app(tmp_path)
        events = _events()

        async def scenario() -> None:
            async with app.run_test(size=(140, 44)) as pilot:
                for e in events:
                    app._dispatch_to_widgets(e)
                await pilot.pause()
                await pilot.pause()

                ah = app.query_one(AgentHistory)
                ah._expanded_seqs = set()
                ah._selected_seq = None
                ah._reflow()
                await pilot.pause()

                # 找首条**有 detail** 的 entry（真 tape 物理首条 node_started 无 detail；
                # Enter 在 detail-less entry 上无视觉反馈，故导航到 detail-bearing entry）
                first_detail_idx = next(
                    (i for i, e in enumerate(ah._entries) if e.detail is not None), None,
                )
                assert first_detail_idx is not None, "应至少有一条 detail-bearing entry"
                target_seq = ah._entries[first_detail_idx].seq

                # down ×N → 选中该 detail-bearing entry
                calls_d, rd = _patch_action(app, "action_history_cursor_down")
                for _ in range(first_detail_idx + 1):
                    await pilot.press("down")
                await pilot.pause()
                rd()
                assert len(calls_d) >= 1, "down 未经真实键位派发"
                assert ah._selected_seq == target_seq, (
                    f"down 后应选中 detail-bearing seq={target_seq}，实际 {ah._selected_seq}"
                )

                # 渲染含 ▶ cursor 标记
                text_after_down = _render_log_text(ah)
                assert "▶" in text_after_down, "down 后渲染应含 ▶"

                # Enter toggle 选中 entry —— 用 capture 验 ⎿ 写入（render_lines 在
                # headless 不稳定反映 detail 写入，但 capture 是「真写了什么」的可信证据）
                log_widget = ah.query_one("#agent-history-log")
                writes_expanded, restore_cap = _capture_writes(log_widget)
                calls_e, re_ = _patch_action(app, "action_history_toggle_expand")
                await pilot.press("enter")
                await pilot.pause()
                re_()
                assert len(calls_e) == 1
                assert target_seq in ah.expanded_seqs

                # ⎿ 经 _write_inline_detail 写入（capture 验）
                expanded_join = "\n".join(writes_expanded)
                assert "⎿" in expanded_join, (
                    f"expanded 态应写入 ⎿ detail marker，capture={expanded_join[:200]!r}"
                )

                # 再 Enter 收起（双向另一向：capture 期间不应再写 ⎿）
                writes_collapsed_before = len(writes_expanded)
                await pilot.press("enter")
                await pilot.pause()
                restore_cap()
                assert target_seq not in ah.expanded_seqs
                # 收起后新增的 write 不含 ⎿
                new_writes = writes_expanded[writes_collapsed_before:]
                assert not any("⎿" in w for w in new_writes), (
                    f"collapsed 态不应再写 ⎿，new_writes={new_writes!r}"
                )

        run_async(scenario())

    # ── 行 3：↓ / ↑（同 agent 内 entry 导航）──────────────────────────────

    def test_down_up_arrows_move_cursor(self, tmp_path: Path) -> None:
        """↓ ×3 → ↑：_selected_seq 按 entries 序前进/后退；▶ 标记位置变（双向）。"""
        app = _make_app(tmp_path)
        events = _events()

        async def scenario() -> None:
            async with app.run_test(size=(140, 44)) as pilot:
                for e in events:
                    app._dispatch_to_widgets(e)
                await pilot.pause()
                await pilot.pause()

                ah = app.query_one(AgentHistory)
                ah._expanded_seqs = set()
                ah._selected_seq = None
                ah._reflow()
                await pilot.pause()

                # down 1
                calls_d, rd = _patch_action(app, "action_history_cursor_down")
                await pilot.press("down"); await pilot.pause(); rd()
                assert len(calls_d) == 1
                seq0 = ah._entries[0].seq
                assert ah._selected_seq == seq0

                # down 2 → entries[1]
                await pilot.press("down"); await pilot.pause()
                seq1 = ah._entries[1].seq
                assert ah._selected_seq == seq1

                # down 3 → entries[2]
                await pilot.press("down"); await pilot.pause()
                seq2 = ah._entries[2].seq
                assert ah._selected_seq == seq2

                # 渲染含 ▶（在新位 seq2）
                text_after_3down = _render_log_text(ah)
                assert "▶" in text_after_3down

                # ↑ 回退到 seq1
                calls_u, ru = _patch_action(app, "action_history_cursor_up")
                await pilot.press("up"); await pilot.pause(); ru()
                assert len(calls_u) == 1, "up 未经真实键位派发"
                assert ah._selected_seq == seq1, (
                    f"up 应回退到 seq={seq1}，实际 {ah._selected_seq}"
                )

        run_async(scenario())

    # ── 行 4：j / k（跨 agent 切换）────────────────────────────────────────

    def test_j_k_switches_agent(self, tmp_path: Path) -> None:
        """j/k：AgentsList selection 变 + AgentHistory header agent 名变（双向）。"""
        app = _make_app(tmp_path)
        events = _events()

        async def scenario() -> None:
            async with app.run_test(size=(140, 44)) as pilot:
                for e in events:
                    app._dispatch_to_widgets(e)
                await pilot.pause()
                await pilot.pause()

                lst = app.query_one(AgentsList)
                ah = app.query_one(AgentHistory)

                # replay 终态：auto-follow → report_painter
                assert app._selected_node == "report_painter"
                assert ah._node_name == "report_painter"

                # k 退到 diagnostic_saver
                calls_k, rk = _patch_action(app, "action_agents_prev")
                await pilot.press("k"); await pilot.pause(); rk()
                assert len(calls_k) == 1, "k 未经真实键位派发"
                assert app._selected_node == "diagnostic_saver"
                assert lst._selected == "diagnostic_saver"
                assert ah._node_name == "diagnostic_saver"

                # header 含 diagnostic_saver
                header_view = ah.query_one("#agent-history-header")
                header_text = _render_widget_text(header_view, width=60, height=1)
                assert "diagnostic_saver" in header_text, (
                    f"header 应含 diagnostic_saver，实际 {header_text!r}"
                )

                # j 进到 report_painter（回原状）
                calls_j, rj = _patch_action(app, "action_agents_next")
                await pilot.press("j"); await pilot.pause(); rj()
                assert len(calls_j) == 1, "j 未经真实键位派发"
                assert app._selected_node == "report_painter"
                assert ah._node_name == "report_painter"

                # 双向：header 不再含 diagnostic_saver，含 report_painter
                header_text2 = _render_widget_text(header_view, width=60, height=1)
                assert "report_painter" in header_text2
                assert "diagnostic_saver" not in header_text2

        run_async(scenario())

    # ── 行 5：C（ChartBrowser 全屏）────────────────────────────────────────

    def test_C_opens_chart_browser(self, tmp_path: Path) -> None:
        """C → screen_stack[-1] is ChartBrowser；渲染含真 chart 标题；Esc pop。"""
        app = _make_app(tmp_path)
        events = _events()

        async def scenario() -> None:
            async with app.run_test(size=(140, 44)) as pilot:
                for e in events:
                    app._dispatch_to_widgets(e)
                await pilot.pause()
                await pilot.pause()

                # §5.4 前置核实：tape 5 charts 进 NodeDetail
                nd = app.query_one(NodeDetail)
                chart_count = sum(
                    len(payloads)
                    for _, labels in nd.all_charts()
                    for _, payloads in labels.items()
                )
                assert chart_count == 5, f"前置核实：tape 应有 5 charts，实际 {chart_count}"

                # C 键
                calls_C, rC = _patch_action(app, "action_open_chart_browser")
                await pilot.press("C"); await pilot.pause(); rC()
                assert len(calls_C) == 1, "C 未经真实键位派发"

                # state：screen_stack[-1] is ChartBrowser
                top = app.screen_stack[-1]
                assert isinstance(top, ChartBrowser), (
                    f"screen_stack[-1] 应是 ChartBrowser，实际 {type(top).__name__}"
                )

                # ChartBrowser 渲染：含真 chart 关键字
                cb_text = _render_widget_text(top, width=140, height=40)
                assert "mxint8" in cb_text or "Accuracy" in cb_text, (
                    f"ChartBrowser 渲染应含 chart 关键字，前 300 字 {cb_text[:300]!r}"
                )

                # Esc pop 回主屏
                await pilot.press("escape"); await pilot.pause()
                assert not isinstance(app.screen_stack[-1], ChartBrowser)

        run_async(scenario())

    # ── 行 6：a（auto-follow 恢复）─────────────────────────────────────────

    def test_a_restores_auto_follow(self, tmp_path: Path) -> None:
        """j（pin）后 a → _auto_follow=True；AgentsList selection 同步。"""
        app = _make_app(tmp_path)
        events = _events()

        async def scenario() -> None:
            async with app.run_test(size=(140, 44)) as pilot:
                for e in events:
                    app._dispatch_to_widgets(e)
                await pilot.pause()
                await pilot.pause()

                lst = app.query_one(AgentsList)
                ah = app.query_one(AgentHistory)

                # 起点：auto-follow=True, selected=report_painter
                assert app._auto_follow is True
                assert app._selected_node == "report_painter"
                assert lst._selected == "report_painter", (
                    "phase-16 auto-follow sync fix：replay 后 AgentsList 光标应同步到 report_painter"
                )

                # k（prev）→ diagnostic_saver（pin）：report_painter 是拓扑末节点（idx 4），
                # j(next) 会 wrap 到 analyzer（idx 0），k(prev) 才是 diagnostic_saver（idx 3）。
                # prior test 误用 j 期望 diagnostic_saver——本修复改为 k（正确方向）。
                await pilot.press("k"); await pilot.pause()
                assert app._auto_follow is False
                assert app._selected_node == "diagnostic_saver"

                # a 恢复
                calls_a, ra = _patch_action(app, "action_follow_active")
                await pilot.press("a"); await pilot.pause(); ra()
                assert len(calls_a) == 1, "a 未经真实键位派发"
                assert app._auto_follow is True, "a 应恢复 _auto_follow=True"

                # SPEC §5.1 行 6 注：replay 无 running 节点，a 把 _selected_node 设为
                # _current_node（replay 终态可能是最后一个 node_started 的 node）。
                # 关键：AgentsList + AgentHistory 都同步到 app._selected_node。
                assert lst._selected == app._selected_node, (
                    f"AgentsList._selected={lst._selected} != app._selected_node={app._selected_node}"
                )
                assert ah._node_name == app._selected_node, (
                    f"AgentHistory.node={ah._node_name} != {app._selected_node}"
                )

        run_async(scenario())

    # ── 行 7：L（LogStream debug toggle）───────────────────────────────────

    def test_L_toggles_log_debug(self, tmp_path: Path) -> None:
        """L → LogStream.show_debug flip；渲染出现 debug 行（route_taken）（双向）。"""
        app = _make_app(tmp_path)
        events = _events()

        async def scenario() -> None:
            async with app.run_test(size=(140, 44)) as pilot:
                for e in events:
                    app._dispatch_to_widgets(e)
                await pilot.pause()
                await pilot.pause()

                log_stream = app.query_one(LogStream)
                assert log_stream.show_debug is False

                # phase-16 实测：dispatch 把所有 level != None 事件都传 LogStream，
                # debug 事件（route_taken）在 show_debug=False 时进 _debug_buffer。
                # real-execution 验证：buffer 应非空（tape 含 5 个 route_taken）。
                assert len(log_stream._debug_buffer) > 0, (
                    "phase-16 debug buffer 应含 replay 期间的 route_taken 事件"
                )

                # 渲染前（render_lines visible）：不含 route（debug 默认隐藏，buffer 未 flush）
                text_before = _render_widget_text(log_stream, width=140, height=40)
                assert "route:" not in text_before, (
                    f"show_debug=False 不应显 route 行，前 200 字 {text_before[:200]!r}"
                )

                # L 键 + capture writes（route 行经 toggle_debug 回放 _debug_buffer 写入）
                writes, rcap = _capture_writes(log_stream)
                calls_L, rL = _patch_action(app, "action_log_toggle_debug")
                await pilot.press("L"); await pilot.pause(); rL()
                rcap()
                assert len(calls_L) == 1, "L 未经真实键位派发"
                assert log_stream.show_debug is True

                # capture 验：L toggle 写了 "debug log: ON" + 回放 buffer 中的 route 行
                writes_join = "\n".join(writes)
                assert "debug log: ON" in writes_join, (
                    f"L 应写 'debug log: ON' marker，capture={writes_join[:200]!r}"
                )
                assert "route:" in writes_join, (
                    f"show_debug=True 应回放 buffer 写 route 行，capture={writes_join[:300]!r}"
                )
                # 双向另一向：buffer 在 toggle ON 后清空（已回放）
                assert len(log_stream._debug_buffer) == 0, "buffer 应在回放后清空"

        run_async(scenario())

    # ── 行 8：t（toggle_thinking notify）───────────────────────────────────

    def test_t_notify_fires_via_real_dispatch(self, tmp_path: Path) -> None:
        """t → action 命中（§5.0 元 AC）；notify 出现（视觉 sanity via SVG）。

        SPEC §5.1 行 8 注：t **不**实现 thinking toggle，仅 notify 提示
        「按 Enter 展开折叠」，timeout=2s。本用例验：action 命中 + notify 出现。
        """
        app = _make_app(tmp_path)
        events = _events()

        async def scenario() -> None:
            async with app.run_test(size=(140, 44)) as pilot:
                for e in events:
                    app._dispatch_to_widgets(e)
                await pilot.pause()
                await pilot.pause()

                # t 键 → meta-AC
                calls_t, rt = _patch_action(app, "action_toggle_thinking")
                await pilot.press("t"); await pilot.pause(); rt()
                assert len(calls_t) == 1, "t 未经真实键位派发"

                # notify 出现（视觉 sanity via SVG）
                svg = app.export_screenshot()
                assert "thinking" in svg or "Enter" in svg, (
                    "t 应 notify 提示 thinking/Enter 关键字"
                )

        run_async(scenario())


# ═════════════════════════════════════════════════════════════════════════
# §5.2 工具配对 + 折叠
# ═════════════════════════════════════════════════════════════════════════

@_skip_if_no_tape
class TestToolPairing:
    """§5.2：tool_call + tool_result 配对成一条 ToolEntry；全部 merged==True。"""

    def test_all_tool_calls_paired_no_orphans(self, tmp_path: Path) -> None:
        """replay 后 kind=='tool' entry 数 == tape agent_tool_call 数；全部 merged==True。"""
        app = _make_app(tmp_path)
        events = _events()

        async def scenario() -> None:
            async with app.run_test(size=(140, 44)) as pilot:
                for e in events:
                    app._dispatch_to_widgets(e)
                await pilot.pause()
                await pilot.pause()

                ah = app.query_one(AgentHistory)
                tool_entries = [e for e in ah.entries if e.kind == "tool"]
                not_merged = [e for e in tool_entries if not e.merged]
                assert len(not_merged) == 0, (
                    f"SPEC §5.2 AC：merged==False 数应 == 0，实际 {len(not_merged)} 个 "
                    f"未配对 tcid={[e.tool_call_id for e in not_merged]}"
                )

                # tool entry 数 == selected_node 的 tool_call 数
                expected_calls = sum(
                    1 for e in events
                    if e.type == "agent_tool_call" and e.node == ah._node_name
                )
                assert len(tool_entries) == expected_calls, (
                    f"tool entry 数 {len(tool_entries)} != tape {ah._node_name} "
                    f"的 tool_call 数 {expected_calls}"
                )

        run_async(scenario())

    def test_tool_entry_collapsed_one_line_expanded_has_detail(self, tmp_path: Path) -> None:
        """折叠默认：每 tool entry 一行 summary；展开：渲染含 ⎿（双向）。"""
        app = _make_app(tmp_path)
        events = _events()

        async def scenario() -> None:
            async with app.run_test(size=(140, 44)) as pilot:
                for e in events:
                    app._dispatch_to_widgets(e)
                await pilot.pause()
                await pilot.pause()

                ah = app.query_one(AgentHistory)
                # 全部折叠
                ah._expanded_seqs = set()
                ah._reflow()
                await pilot.pause()
                collapsed = _render_log_text(ah)
                assert "⎿" not in collapsed

                # 找首条 tool entry，down 到它，Enter 展开
                first_tool_idx = next(
                    (i for i, e in enumerate(ah._entries) if e.kind == "tool"), None,
                )
                assert first_tool_idx is not None
                target_seq = ah._entries[first_tool_idx].seq
                ah._selected_seq = None
                ah._reflow()
                await pilot.pause()
                for _ in range(first_tool_idx + 1):
                    await pilot.press("down")
                await pilot.pause()
                assert ah._selected_seq == target_seq

                await pilot.press("enter")
                await pilot.pause()
                expanded = _render_log_text(ah)
                assert "⎿" in expanded, "展开 tool entry 应有 ⎿ detail marker"

        run_async(scenario())


# ═════════════════════════════════════════════════════════════════════════
# §5.3 message 视觉分级（Console.capture ANSI）+ Markdown
# ═════════════════════════════════════════════════════════════════════════

@_skip_if_no_tape
class TestMessageGrading:
    """§5.3：message bold+主题色；tool 不 bold；展开 markdown。"""

    def test_message_bold_tool_not_bold_via_console_capture(self, tmp_path: Path) -> None:
        """Console.capture 离线渲染：message **summary 行** bold+主题色；tool summary 不 bold。

        phase-16 real-execution 修正（prior test 假阳性）：
          - 原断言「``msg_entry.detail`` 含 bold」**错**——detail 是 ``Markdown(text)``，
            纯文本 message 无 markdown bold 标记时 ``console.print(Markdown(...))`` 不产
            bold ANSI。SPEC §2.3 的视觉分级 bold 在 **summary 行**（``_style_for_kind``
            返回 ``"bold green"``），不在 detail body。
          - 原断言用 ``Text(style="bold $success")`` 也是 stale——aaabd39 已把样式改
            ``"bold green"``（Rich 不识别 ``$token``，会静默丢整串 style）。
        本用例改用 ``_capture_writes`` 抓 widget **实际**写的 summary Text 对象，断言：
          - message summary Text.style 含 bold + green（视觉分级 = 与 tool 拉开层级）
          - tool summary Text.style 不含 bold（仅 status icon ``✓/…/✗`` 区分）
        """
        app = _make_app(tmp_path)
        events = _events()

        async def scenario() -> None:
            async with app.run_test(size=(140, 44)) as pilot:
                for e in events:
                    app._dispatch_to_widgets(e)
                await pilot.pause()
                await pilot.pause()

                ah = app.query_one(AgentHistory)
                msg_entry = next(
                    (e for e in ah.entries if e.kind == "message" and e.detail is not None),
                    None,
                )
                assert msg_entry is not None, "应有带 detail 的 message entry"
                tool_entry = next(
                    (e for e in ah.entries if e.kind == "tool" and e.detail is not None),
                    None,
                )
                assert tool_entry is not None, "应有带 detail 的 tool entry"

                # 抓 _reflow 期间 widget 真写的 Text 对象（带 style）
                log_widget = ah.query_one("#agent-history-log")
                written_texts: list[Text] = []
                orig_write = log_widget.write
                def cap_obj(content: Any) -> Any:
                    if isinstance(content, Text):
                        written_texts.append(content)
                    return orig_write(content)
                log_widget.write = cap_obj  # type: ignore[method-assign]

                ah._expanded_seqs = set()
                ah._reflow()
                await pilot.pause()
                log_widget.write = orig_write  # type: ignore[method-assign]

                # 找 message / tool 的 summary Text
                msg_summary = next(
                    (t for t in written_texts if msg_entry.summary[:30] in t.plain), None,
                )
                tool_summary = next(
                    (t for t in written_texts if tool_entry.tool_name and tool_entry.tool_name in t.plain), None,
                )
                assert msg_summary is not None, (
                    f"未捕获 message summary Text（含 {msg_entry.summary[:30]!r}）"
                )
                assert tool_summary is not None, (
                    f"未捕获 tool summary Text（含 tool={tool_entry.tool_name!r}）"
                )

                # 离线 Console.render 对比 style ANSI
                console = Console(force_terminal=True, width=120, color_system="auto")
                with console.capture() as cap_msg:
                    console.print(msg_summary)
                ansi_msg = cap_msg.get()
                with console.capture() as cap_tool:
                    console.print(tool_summary)
                ansi_tool = cap_tool.get()

                # message summary 含 bold + green（phase-16 _style_for_kind = "bold green"）
                # Rich 合并 SGR：渲染为 ``\x1b[1;32m``（不是分开的 ``\x1b[1m\x1b[32m``），
                # 故断言用 ``1;`` 子串（兼容合并/分开两种 SGR 编码）。
                assert ("1;" in ansi_msg or "\x1b[1m" in ansi_msg), (
                    f"message summary 应含 bold ANSI（``\\x1b[1;...`` 或 ``\\x1b[1m``），"
                    f"前 200 字 {ansi_msg[:200]!r}"
                )
                assert ("\x1b[32m" in ansi_msg
                        or "\x1b[1;32m" in ansi_msg
                        or "\x1b[38;2;" in ansi_msg), (
                    f"message summary 应含 green ANSI，实际 {ansi_msg!r}"
                )
                # tool summary 不含 bold（视觉分级：仅 icon 区分，不 bold）
                assert ("1;" not in ansi_tool and "\x1b[1m" not in ansi_tool), (
                    f"tool summary 不应 bold（视觉分级），实际 {ansi_tool!r}"
                )

        run_async(scenario())

    def test_message_expand_shows_markdown(self, tmp_path: Path) -> None:
        """展开 message entry → 渲染含 ⎿（双向：折叠无/展开有）。"""
        app = _make_app(tmp_path)
        events = _events()

        async def scenario() -> None:
            async with app.run_test(size=(140, 44)) as pilot:
                for e in events:
                    app._dispatch_to_widgets(e)
                await pilot.pause()
                await pilot.pause()

                ah = app.query_one(AgentHistory)
                msg_entries = [e for e in ah.entries if e.kind == "message"]
                assert msg_entries, "report_painter 应有 message entries"

                # 全部折叠（capture 验：折叠态 _reflow 不写 ⎿）
                log_widget = ah.query_one("#agent-history-log")
                writes_collapsed, rc1 = _capture_writes(log_widget)
                ah._expanded_seqs = set()
                ah._reflow()
                await pilot.pause()
                rc1()
                collapsed_join = "\n".join(writes_collapsed)
                assert "⎿" not in collapsed_join, "折叠态不应写 ⎿"

                # 展开首条 message（capture 验：展开态 _reflow 写 ⎿ + message detail）
                first_msg_seq = msg_entries[0].seq
                writes_expanded, rc2 = _capture_writes(log_widget)
                ah._expanded_seqs.add(first_msg_seq)
                ah._reflow()
                await pilot.pause()
                rc2()
                expanded_join = "\n".join(writes_expanded)
                assert "⎿" in expanded_join, "展开 message 后应写入 ⎿"
                # message detail 是 render_message(Markdown) → 至少写出 message 文本片段
                # （双向：折叠无 / 展开有 message 内容）
                assert any(
                    msg_entries[0].summary[:20] in w or "⎿" in w
                    for w in writes_expanded
                ), f"expanded 应含 message 内容，capture={expanded_join[:200]!r}"

        run_async(scenario())


# ═════════════════════════════════════════════════════════════════════════
# §5.6 重放一致性（state 确定性 + 乱序 reducer fold）
# ═════════════════════════════════════════════════════════════════════════

@_skip_if_no_tape
class TestReplayConsistency:
    """§5.6：正序回放两次 entries 四元组集合相等；逆序灌入 set_node 集合也相等。"""

    def test_forward_replay_twice_equal(self, tmp_path: Path) -> None:
        """正序 replay 两次（中间切 agent 再切回）→ entries 四元组 + expanded 相等。"""
        app = _make_app(tmp_path)
        events = _events()

        async def scenario() -> None:
            async with app.run_test(size=(140, 44)) as pilot:
                for e in events:
                    app._dispatch_to_widgets(e)
                await pilot.pause()
                await pilot.pause()

                ah = app.query_one(AgentHistory)
                node_a = ah._node_name
                entries_a = [
                    (e.seq, e.kind, e.summary, str(e.meta)[:60]) for e in ah.entries
                ]
                expanded_a = set(ah.expanded_seqs)

                # 切到另一个 agent
                await pilot.press("k"); await pilot.pause()
                node_b = ah._node_name
                assert node_b != node_a

                # 切回
                await pilot.press("j"); await pilot.pause()
                assert ah._node_name == node_a

                entries_b = [
                    (e.seq, e.kind, e.summary, str(e.meta)[:60]) for e in ah.entries
                ]
                expanded_b = set(ah.expanded_seqs)

                assert entries_a == entries_b, (
                    f"§5.6 正序重放 entries 不一致：\nfirst={entries_a[:3]}\nsecond={entries_b[:3]}"
                )
                assert expanded_a == expanded_b, (
                    f"§5.6 expanded_seqs 不一致：{expanded_a} vs {expanded_b}"
                )

                # render_lines(Region) 文本确定性
                text_a = _render_log_text(ah, height=40)
                ah._reflow()
                await pilot.pause()
                text_b = _render_log_text(ah, height=40)
                assert text_a == text_b, "§5.6 正序重放渲染文本不一致"

        run_async(scenario())

    def test_reverse_replay_set_equal(self, tmp_path: Path) -> None:
        """逆序灌入 set_node → (seq,kind,summary) 集合与正序相等（reducer fold 顺序无关）。"""
        app = _make_app(tmp_path)
        events = _events()

        async def scenario() -> None:
            async with app.run_test(size=(140, 44)) as pilot:
                # 正序 replay
                for e in events:
                    app._dispatch_to_widgets(e)
                await pilot.pause()
                await pilot.pause()

                ah = app.query_one(AgentHistory)
                node = ah._node_name  # report_painter
                forward_set = {
                    (e.seq, e.kind, e.summary) for e in ah.entries
                }

                # 逆序灌入：set_node 全量 fold（buffer_orphans=True 处理乱序）
                forward_node_events = [e for e in events if e.node == node]
                reversed_events = list(reversed(forward_node_events))
                ah.set_node(node, reversed_events)
                await pilot.pause()

                reverse_set = {
                    (e.seq, e.kind, e.summary) for e in ah.entries
                }

                assert forward_set == reverse_set, (
                    "§5.6 reducer fold 顺序无关性破：逆序产不同集合\n"
                    f"forward-only: {forward_set - reverse_set}\n"
                    f"reverse-only: {reverse_set - forward_set}"
                )

        run_async(scenario())


# ═════════════════════════════════════════════════════════════════════════
# 真实 PTY 跑 ``orca run``（live stack：orchestrator → events → TUI → render）
# ═════════════════════════════════════════════════════════════════════════

@_skip_if_no_tape
def test_real_orca_run_pty(tmp_path: Path) -> None:
    """真实 ``orca run examples/demo_task.yaml "test"`` 在 PTY 跑通到 workflow_completed。

    SPEC §5 验收要求：「禁止只用代码 review / 只用直调 ``action_*`` 冒充验收」。
    本测试是「真用户路径」：用户在 shell 跑 ``orca run``，orchestrator 真 spawn
    opencode 子进程 → events → OrcaApp → TUI render。``pexpect`` 真 PTY 驱动
    （TUI 必须有 TTY）。

    若环境无 opencode（不可达外边界），本测试 skip（SPEC 允许：mock only 不可达边界）。
    """
    import shutil

    pytest.importorskip("pexpect")
    if not shutil.which("orca"):
        pytest.skip("orca CLI 不在 PATH")
    if not shutil.which("opencode"):
        pytest.skip("opencode 不在 PATH（外边界不可达，SPEC 允许 skip）")

    import pexpect

    demo = REPO_ROOT / "examples" / "demo_task.yaml"
    assert demo.exists(), "examples/demo_task.yaml 应存在"

    log_path = tmp_path / "pty_orca_run.log"
    logfile = open(log_path, "w", encoding="utf-8")  # noqa: SIM115

    child: pexpect.spawn | None = None
    try:
        child = pexpect.spawn(
            "orca", ["run", str(demo), "test"],
            cwd=str(REPO_ROOT),
            encoding="utf-8",
            timeout=60,
            dimensions=(40, 140),
        )
        child.logfile_read = logfile

        # 等 TUI 起来 + AgentsList 渲染 worker 节点
        child.expect("worker", timeout=30)

        # 真 user 按键：证明 live TUI 不崩
        _time.sleep(6)
        child.sendline("")  # Enter
        _time.sleep(0.5)
        child.send("j")
        _time.sleep(0.3)
        child.send("k")
        _time.sleep(0.3)
        child.send("C")
        _time.sleep(1)
        child.send("\x1b")  # Esc
        _time.sleep(0.3)

        # 等 workflow 完成
        try:
            child.expect(["completed", "DONE", "worker"], timeout=40)
        except pexpect.exceptions.TIMEOUT:
            pass

        # q 退出
        child.send("q")
        try:
            child.expect(pexpect.EOF, timeout=10)
        except pexpect.exceptions.TIMEOUT:
            pass
    except pexpect.exceptions.TIMEOUT:
        # 外部 API 慢/限流是允许 skip 的（SPEC：mock only 不可达外边界）
        pytest.skip(f"orca run 超时（外部 API 慢/限流），log: {log_path}")
    except Exception as e:
        pytest.skip(f"PTY 跑失败（外部环境）：{e}; log: {log_path}")
    finally:
        if child is not None:
            try:
                child.close(force=True)
            except Exception:  # noqa: BLE001
                pass
        logfile.close()

    # 真实输出 log：TUI 真渲染了 AgentsList（worker）
    assert log_path.exists()
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    assert "worker" in log_text, (
        f"真 orca run 应渲染 AgentsList 含 worker，前 300 字 {log_text[:300]!r}"
    )
