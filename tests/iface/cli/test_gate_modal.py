"""test_gate_modal.py —— GateModal 两种 source 渲染 + dismiss 返回值（SPEC §6.3）。

覆盖（计划 C4.6）：
  - tool_permission：显示工具+参数+2 按钮（allow/deny）
  - agent_ask with options：显示问题+选项按钮
  - agent_ask without options：显示问题+Input
  - 用户答按钮 → dismiss 返回选项
  - 用户答 Input 回车 → dismiss 返回文本
  - notify_resolved_externally（别壳先答）→ dismiss 返回 ``__orca_broadcast__`` 哨兵
"""

from __future__ import annotations

import asyncio

from orca.gates.types import HumanGate
from orca.iface.cli.screens.gate_modal import GateModal
from textual import work
from textual.app import App, ComposeResult
from textual.widgets import Button, Input, Label


def run_async(coro):
    return asyncio.run(coro)


def _gate(*, source="tool_permission", prompt="批准 Bash？", options=None,
          context=None, gate_id="g1", node="review", run_id="r1",
          session_id="sess-x") -> HumanGate:
    return HumanGate(
        id=gate_id,
        prompt=prompt,
        options=options,
        context=context if context is not None else (
            {"tool": "Bash", "tool_input": {"command": "rm -rf x"}, "tool_use_id": "tu1"}
            if source == "tool_permission" else {}
        ),
        source=source,
        run_id=run_id,
        node=node,
        session_id=session_id,
    )


class _ModalApp(App):
    """push GateModal 并捕获 dismiss 返回值的临时 app。

    ``push_screen_wait`` 必须从 ``@work`` worker 调用（Textual 强约束：wait 模式下
    不能在主 pump 上阻塞）。``on_mount`` 启动 ``_run_modal`` worker，dismiss 后存值
    + ``exit``，测试侧 ``await pilot.pause()`` 等 dismissed 被赋值。
    """

    def __init__(self, gate: HumanGate) -> None:
        super().__init__()
        self.gate = gate
        self.dismissed: str | None = None

    def compose(self) -> ComposeResult:
        yield Label("bg")  # 背景占位（modal 覆盖之）

    def on_mount(self) -> None:
        self._run_modal()

    @work
    async def _run_modal(self) -> None:
        result = await self.push_screen_wait(GateModal(self.gate))
        self.dismissed = result
        self.exit()


# ── tool_permission 渲染（SPEC §1.2）─────────────────────────────────────


class TestGateModalToolPermission:
    """tool_permission：工具+参数+allow/deny 按钮。"""

    def test_renders_tool_and_args_and_buttons(self):
        gate = _gate(source="tool_permission")

        async def scenario():
            app = _ModalApp(gate)
            async with app.run_test() as pilot:
                await pilot.pause()
                modal = app.screen
                assert isinstance(modal, GateModal)
                # Label.content 拿到文本；Button.label 拿到按钮文本
                labels = [str(w.content) for w in modal.query(Label)]
                joined = "\n".join(labels)
                assert "Bash" in joined  # 工具名
                assert "rm -rf x" in joined or "参数" in joined  # 参数
                buttons = list(modal.query(Button))
                ids = {b.id for b in buttons}
                assert "gate-allow" in ids
                assert "gate-deny" in ids
                app.exit()
        run_async(scenario())

    def test_press_allow_dismisses_with_allow(self):
        gate = _gate(source="tool_permission")

        async def scenario():
            app = _ModalApp(gate)
            async with app.run_test() as pilot:
                # 等 modal 上来
                await pilot.pause()
                await pilot.click("#gate-allow")
                await pilot.pause()
                assert app.dismissed == "allow"
        run_async(scenario())

    def test_press_deny_dismisses_with_deny(self):
        gate = _gate(source="tool_permission")

        async def scenario():
            app = _ModalApp(gate)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.click("#gate-deny")
                await pilot.pause()
                assert app.dismissed == "deny"
        run_async(scenario())


# ── agent_ask 渲染（SPEC §1.2）────────────────────────────────────────────


class TestGateModalAgentAsk:
    """agent_ask：问题 + 选项按钮（有 options）/ Input（无 options）。"""

    def test_with_options_renders_option_buttons(self):
        gate = _gate(source="agent_ask", prompt="选哪个？", options=["A", "B", "C"])

        async def scenario():
            app = _ModalApp(gate)
            async with app.run_test() as pilot:
                await pilot.pause()
                modal = app.screen
                assert isinstance(modal, GateModal)
                # Button.label 拿到按钮文本（选项原文）
                labels = {str(b.label) for b in modal.query(Button)}
                assert {"A", "B", "C"} <= labels
                # 无 Input（有 options 不渲染 Input）
                assert len(list(modal.query(Input))) == 0
                app.exit()
        run_async(scenario())

    def test_pressing_option_dismisses_with_label(self):
        gate = _gate(source="agent_ask", prompt="选？", options=["A", "B"])

        async def scenario():
            app = _ModalApp(gate)
            async with app.run_test() as pilot:
                await pilot.pause()
                modal = app.screen
                # 点 B 按钮（Button.label 是选项原文）
                b_btn = next(b for b in modal.query(Button) if str(b.label) == "B")
                await pilot.click(b_btn)
                await pilot.pause()
                assert app.dismissed == "B"
        run_async(scenario())

    def test_without_options_renders_input(self):
        gate = _gate(source="agent_ask", prompt="输入连接串", options=None)

        async def scenario():
            app = _ModalApp(gate)
            async with app.run_test() as pilot:
                await pilot.pause()
                modal = app.screen
                assert isinstance(modal, GateModal)
                inputs = list(modal.query(Input))
                assert len(inputs) == 1
                # 无按钮（无 options）
                assert len(list(modal.query(Button))) == 0
                app.exit()
        run_async(scenario())

    def test_input_submit_dismisses_with_text(self):
        gate = _gate(source="agent_ask", prompt="输入", options=None)

        async def scenario():
            app = _ModalApp(gate)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("h", "i")
                await pilot.press("enter")
                await pilot.pause()
                assert app.dismissed == "hi"
        run_async(scenario())


# ── 广播输家（SPEC §4.5 决策 5）──────────────────────────────────────────


class TestGateModalBroadcastLoser:
    """收到 human_decision_resolved（别壳先答）→ dismiss 哨兵。"""

    def test_notify_resolved_dismisses_with_sentinel(self):
        gate = _gate(source="tool_permission")

        async def scenario():
            app = _ModalApp(gate)
            async with app.run_test() as pilot:
                await pilot.pause()
                modal = app.screen
                assert isinstance(modal, GateModal)
                modal.notify_resolved_externally(source="web", answer="allow")
                await pilot.pause()
                assert app.dismissed is not None
                assert app.dismissed.startswith("__orca_broadcast__:web:")
        run_async(scenario())

    def test_notify_resolved_after_user_answer_is_noop(self):
        """本壳用户已答（dismissed）→ 后续广播不应重复 dismiss。"""
        gate = _gate(source="tool_permission")

        async def scenario():
            app = _ModalApp(gate)
            async with app.run_test() as pilot:
                await pilot.pause()
                modal = app.screen
                # 先点 allow（赢家路径）
                await pilot.click("#gate-allow")
                await pilot.pause()
                first = app.dismissed
                # 再调 notify（已被答 → noop）
                modal.notify_resolved_externally("web", "allow")
                await pilot.pause()
                # dismiss 值不变（赢家答案生效）
                assert app.dismissed == first == "allow"
        run_async(scenario())
