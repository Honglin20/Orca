"""tests/iface/cli/test_dialog_modal.py —— DialogModal 黑盒测试（phase 11 SPEC §6.1）。

headless Textual ``run_test()`` pilot 测试。覆盖：
  - compose：标题 + output 预览 + 历史区 + 输入框 + 发送/结束 按钮
  - 发送（带输入文本）→ history 显示 ``user>`` + ``agent>``（用 fake handler 的 send_turn 固定 reply）
  - 结束 → dismiss（modal 离屏）

测试模式（与 test_interrupt_modal.py / test_gate_modal.py 同款）：``_ModalApp`` 临时 App push
DialogModal，dismiss 后存 ``self.dismissed``。

**fake handler**（不 spawn 真 claude）：``FakeDialogHandler`` 实现三方法契约，send_turn 直接返回
固定 reply 字符串（不 await spawn）。这让 pilot 测试确定性 + 快。
"""

from __future__ import annotations

import asyncio

from textual import work
from textual.app import App, ComposeResult
from textual.widgets import Button, Label, RichLog

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.exec.context import RunContext
from orca.gates.dialog import DialogHandler
from orca.iface.cli.screens.dialog_modal import DialogModal
from orca.profiles import get_profile


def run_async(coro):
    return asyncio.run(coro)


class FakeDialogHandler:
    """DialogHandler 替身：三方法契约全实现，但 send_turn 不 spawn——直接返固定 reply。

    记录调用历史（turn_count / received user texts）让测试断言「发送按钮真的触发了 send_turn」。
    start_dialog / end_dialog 是 no-op（不 emit，modal 测试不关心 tape——只关心 UI 渲染）。

    ``fail_on_send``：非 None 时 send_turn raise 该异常（测 modal 的 fail-loud 路径：history
    显示错误 + 发送按钮复位）。
    """

    def __init__(self, reply_text: str = "fake agent reply", fail_on_send: BaseException | None = None):
        self._reply = reply_text
        self._fail_on_send = fail_on_send
        self.turn_count = 0
        self.received_texts: list[str] = []
        self.ended = False
        self.started = False

    async def start_dialog(self, node, agent_output, ctx) -> str:
        self.started = True
        return "fake-dialog-id"

    async def send_turn(self, dialog_id, user_text, ctx) -> str:
        if self._fail_on_send is not None:
            raise self._fail_on_send
        self.received_texts.append(user_text)
        self.turn_count += 1
        return self._reply

    async def end_dialog(self, dialog_id, ctx) -> None:
        self.ended = True


def _make_ctx(tmp_path) -> RunContext:
    return RunContext(inputs={}, outputs={}, run_id="r1")


def _make_bus(tmp_path) -> EventBus:
    return EventBus(Tape(tmp_path / "modal.jsonl", run_id="r1"))


class _ModalApp(App):
    """push DialogModal 并捕获 dismiss 的临时 app（同 test_interrupt_modal 模式）。

    ``push_screen``（非 wait）：dialog 是 fire-and-forget，dismiss 不返回值。本 app 用
    ``on_screen_change`` 风格检测 modal 离屏后 exit + 标记 dismissed。
    """

    def __init__(self, modal: DialogModal) -> None:
        super().__init__()
        self._modal = modal
        self.dismissed: bool = False

    def compose(self) -> ComposeResult:
        yield Label("bg")

    def on_mount(self) -> None:
        self._push()

    @work
    async def _push(self) -> None:
        await self.push_screen(self._modal)
        # 等到 modal 被 dismiss（self.screen 变回本 app 的主屏）
        while isinstance(self.screen, DialogModal):
            await asyncio.sleep(0.02)
        # 给 modal 的 end_dialog worker 一点时间跑完（dismiss 后 worker 可能还在 await emit）
        await asyncio.sleep(0.05)
        self.dismissed = True
        self.exit()


# ── compose ────────────────────────────────────────────────────────────────


class TestCompose:
    def test_modal_composes_all_widgets(self, tmp_path):
        """modal 打开后：标题 + output 预览 + 历史区 + 输入框 + 发送/结束 按钮齐全。"""
        handler = FakeDialogHandler()
        modal = DialogModal(
            handler, node="cfg", agent_output={"k": "v"},
            ctx=_make_ctx(tmp_path), bus=_make_bus(tmp_path),
        )
        app = _ModalApp(modal)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                await pilot.pause()  # 等 start_dialog worker 跑完
                assert isinstance(app.screen, DialogModal)
                assert app.screen.query_one("#dialog-title") is not None
                assert app.screen.query_one("#dialog-output-preview", RichLog) is not None
                assert app.screen.query_one("#dialog-history", RichLog) is not None
                assert app.screen.query_one("#dialog-input") is not None
                assert app.screen.query_one("#dialog-send") is not None
                assert app.screen.query_one("#dialog-end") is not None
        run_async(scenario())

    def test_modal_title_shows_node_name(self, tmp_path):
        """标题含 node 名（用户知道在追问哪个 node）。"""
        handler = FakeDialogHandler()
        modal = DialogModal(
            handler, node="configurator", agent_output="x",
            ctx=_make_ctx(tmp_path), bus=_make_bus(tmp_path),
        )
        app = _ModalApp(modal)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                await pilot.pause()
                title = app.screen.query_one("#dialog-title")
                assert "configurator" in str(title.render())
        run_async(scenario())


# ── 发送 → history 显示 user> + agent> ─────────────────────────────────────


class TestSend:
    def test_send_appends_user_and_agent_to_history(self, tmp_path):
        """输入文本 + 点发送 → history 显示 ``user> <text>`` + ``agent> <reply>``。"""
        handler = FakeDialogHandler(reply_text="这是 agent 的回答")
        modal = DialogModal(
            handler, node="cfg", agent_output={"o": 1},
            ctx=_make_ctx(tmp_path), bus=_make_bus(tmp_path),
        )
        app = _ModalApp(modal)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                await pilot.pause()  # 等 start_dialog worker
                input_box = app.screen.query_one("#dialog-input")
                input_box.value = "为什么是这个值？"
                await pilot.pause()
                _press_button(pilot, app, "#dialog-send")
                await pilot.pause()
                await pilot.pause()  # 等 send_turn worker + RichLog flush
                # handler 被调一次 send_turn，文本正确
                assert handler.turn_count == 1
                assert handler.received_texts == ["为什么是这个值？"]
                # history 区含 user> 和 agent>
                history = app.screen.query_one("#dialog-history", RichLog)
                text = _flatten(history.lines)
                assert "user>" in text
                assert "为什么是这个值？" in text
                assert "agent>" in text
                assert "这是 agent 的回答" in text
                # 输入框被清空
                assert input_box.value == ""
        run_async(scenario())

    def test_send_empty_text_does_not_send(self, tmp_path):
        """空输入 + 发送 → 不触发 send_turn（防无意义空轮）。"""
        handler = FakeDialogHandler()
        modal = DialogModal(
            handler, node="cfg", agent_output="x",
            ctx=_make_ctx(tmp_path), bus=_make_bus(tmp_path),
        )
        app = _ModalApp(modal)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                await pilot.pause()
                _press_button(pilot, app, "#dialog-send")  # 输入框空
                await pilot.pause()
                await pilot.pause()
                assert handler.turn_count == 0  # 没发
        run_async(scenario())


# ── 结束 → dismiss ─────────────────────────────────────────────────────────


class TestEnd:
    def test_end_button_dismisses_modal(self, tmp_path):
        """点「结束对话」→ end_dialog 被调 + modal dismiss（离屏）。"""
        handler = FakeDialogHandler()
        modal = DialogModal(
            handler, node="cfg", agent_output="x",
            ctx=_make_ctx(tmp_path), bus=_make_bus(tmp_path),
        )
        app = _ModalApp(modal)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                await pilot.pause()  # 等 start_dialog worker（dialog_id 就绪）
                _press_button(pilot, app, "#dialog-end")
                await pilot.pause()
                await pilot.pause()
                await pilot.pause()
                await pilot.pause()
                await pilot.pause()  # 等 end_dialog worker + dismiss + _push 的 0.1s sleep
                assert handler.ended is True
                assert app.dismissed is True
        run_async(scenario())

    def test_escape_dismisses_modal(self, tmp_path):
        """Esc = 结束对话（与 InterruptModal Esc=abort 同语义）。"""
        handler = FakeDialogHandler()
        modal = DialogModal(
            handler, node="cfg", agent_output="x",
            ctx=_make_ctx(tmp_path), bus=_make_bus(tmp_path),
        )
        app = _ModalApp(modal)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                await pilot.pause()
                await pilot.pause()
                assert handler.ended is True
                assert app.dismissed is True
        run_async(scenario())


# ── send 失败 → fail loud 显示错误 + 按钮复位（SPEC §6 / 模块 docstring）────


class TestSendFailure:
    def test_send_failure_shows_error_and_resets_button(self, tmp_path):
        """send_turn 抛异常 → history 显示错误信息 + 发送按钮重新可用（不卡死）。

        SPEC §6 / dialog.py docstring：spawn 失败 fail loud，modal 在 history 显示错误（不静默
        丢一轮），交还控制让用户重试或结束。这是「用户知道这轮没答上」的可观测性保证。
        """
        from textual.widgets import Button

        handler = FakeDialogHandler(fail_on_send=RuntimeError("claude binary missing"))
        modal = DialogModal(
            handler, node="cfg", agent_output={"o": 1},
            ctx=_make_ctx(tmp_path), bus=_make_bus(tmp_path),
        )
        app = _ModalApp(modal)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                await pilot.pause()  # 等 start_dialog worker
                input_box = app.screen.query_one("#dialog-input")
                input_box.value = "问题"
                await pilot.pause()
                _press_button(pilot, app, "#dialog-send")
                await pilot.pause()
                await pilot.pause()
                await pilot.pause()  # 等 send_turn worker 捕获异常 + 复位
                # handler 被调一次（异常发生在 handler 内，但仍计数调用）
                assert handler.turn_count == 0  # raise 前没递增
                # history 含错误提示（fail loud 可观测）
                history = app.screen.query_one("#dialog-history", RichLog)
                text = _flatten(history.lines)
                assert "失败" in text or "claude binary missing" in text
                # 发送按钮已复位（disabled=False，可再点）
                send_btn = app.screen.query_one("#dialog-send", Button)
                assert send_btn.disabled is False
        run_async(scenario())


# ── helpers ────────────────────────────────────────────────────────────────


def _flatten(lines) -> str:
    """把 RichLog 的 lines（Text 对象列表）拍平成纯字符串。"""
    return "".join(str(line) for line in lines)


def _press_button(pilot, app, selector: str) -> None:
    """可靠触发按钮按下：直接 post ``Button.Pressed`` 消息。

    ``pilot.click(selector)`` 在嵌套 Vertical + 固定高度布局下偶尔落不到按钮坐标（Textual
    按坐标 hit-test），导致 ``on_button_pressed`` 不触发。post_message 是 Textual 推荐的
    确定性测试路径（绕过坐标 hit-test，直接送消息给 handler）。这测的是「消息到达后 handler
    行为正确」，而非「坐标点击」——后者是 Textual 自身的职责，非本项目逻辑。
    """
    btn = app.screen.query_one(selector, Button)
    # post_message 返回 bool（是否入队成功），非 awaitable。直接调用即可。
    btn.post_message(Button.Pressed(btn))
