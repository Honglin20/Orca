"""test_interrupt_modal.py —— InterruptModal 黑盒测试（SPEC §3.1）。

headless Textual ``run_test()`` pilot 测试。覆盖：
  - compose：CONTINUE / SKIP / ABORT 三按钮 + guidance textarea
  - CONTINUE 无 guidance → dismiss ("continue", None)
  - CONTINUE 带 guidance → dismiss ("continue", "<text>")
  - SKIP → dismiss ("skip", None)（即使 textarea 有文本也忽略）
  - ABORT → dismiss ("abort", None)
  - Esc → 等价 ABORT（"abort", None）

测试模式（与 test_gate_modal.py 同款）：``_ModalApp`` 临时 App push InterruptModal，
dismiss 返回值存 ``self.dismissed``，测试侧 ``await pilot.pause()`` 等赋值。
"""

from __future__ import annotations

import asyncio

from textual import work
from textual.app import App, ComposeResult
from textual.widgets import Label

from orca.gates.types import InterruptRequest
from orca.iface.cli.screens.interrupt_modal import InterruptModal


def run_async(coro):
    return asyncio.run(coro)


def make_ireq(**kwargs) -> InterruptRequest:
    defaults = dict(
        id="i1", node="configurator", run_id="r1",
        session_id="sess-x", source="cli", elapsed_at_request=30.0,
    )
    defaults.update(kwargs)
    return InterruptRequest(**defaults)  # type: ignore[arg-type]


class _ModalApp(App):
    """push InterruptModal 并捕获 dismiss 返回值的临时 app（同 test_gate_modal 模式）。

    ``push_screen_wait`` 必须从 ``@work`` worker 调用（Textual 强约束）。
    ``on_mount`` 启 ``_run_modal`` worker，dismiss 后存值 + exit。
    """

    def __init__(self, ireq: InterruptRequest) -> None:
        super().__init__()
        self.ireq = ireq
        self.dismissed: tuple[str, str | None] | None = None

    def compose(self) -> ComposeResult:
        yield Label("bg")  # 背景占位（modal 覆盖之）

    def on_mount(self) -> None:
        self._run_modal()

    @work
    async def _run_modal(self) -> None:
        result = await self.push_screen_wait(InterruptModal(self.ireq))
        self.dismissed = result
        self.exit()


# ── compose ────────────────────────────────────────────────────────────────


class TestCompose:
    def test_modal_compose_three_buttons_and_textarea(self):
        app = _ModalApp(make_ireq())

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()  # 等 on_mount worker push modal
                modal = app.screen
                assert isinstance(modal, InterruptModal)
                assert modal.query_one("#gate-continue") is not None
                assert modal.query_one("#gate-skip") is not None
                assert modal.query_one("#gate-abort") is not None
                assert modal.query_one("#guidance-input") is not None

        run_async(scenario())

    def test_modal_title_shows_node_name(self):
        app = _ModalApp(make_ireq(node="runner"))

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                modal = app.screen
                assert isinstance(modal, InterruptModal)
                title = modal.query_one("#interrupt-title")
                assert "runner" in str(title.render())

        run_async(scenario())


# ── CONTINUE ────────────────────────────────────────────────────────────────


class TestContinue:
    def test_continue_without_guidance(self):
        app = _ModalApp(make_ireq())

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                await pilot.click("#gate-continue")
                await pilot.pause()
                await pilot.pause()
        run_async(scenario())
        assert app.dismissed == ("continue", None)

    def test_continue_with_guidance(self):
        app = _ModalApp(make_ireq())

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                modal = app.screen
                assert isinstance(modal, InterruptModal)
                ta = modal.query_one("#guidance-input")
                ta.insert("用更保守的方案")
                await pilot.pause()
                await pilot.click("#gate-continue")
                await pilot.pause()
                await pilot.pause()
        run_async(scenario())
        assert app.dismissed == ("continue", "用更保守的方案")


# ── SKIP / ABORT ─────────────────────────────────────────────────────────────


class TestSkipAbort:
    def test_skip_dismisses_skip_no_guidance(self):
        """SKIP 时即使 textarea 有文本，guidance 也忽略（None）。"""
        app = _ModalApp(make_ireq())

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                modal = app.screen
                assert isinstance(modal, InterruptModal)
                ta = modal.query_one("#guidance-input")
                ta.insert("不该被用的 guidance")
                await pilot.pause()
                await pilot.click("#gate-skip")
                await pilot.pause()
                await pilot.pause()
        run_async(scenario())
        assert app.dismissed == ("skip", None)

    def test_abort_dismisses_abort(self):
        app = _ModalApp(make_ireq())

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                await pilot.click("#gate-abort")
                await pilot.pause()
                await pilot.pause()
        run_async(scenario())
        assert app.dismissed == ("abort", None)


# ── Esc = abort ──────────────────────────────────────────────────────────────


class TestEscape:
    def test_escape_dismisses_abort(self):
        app = _ModalApp(make_ireq())

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                await pilot.pause()
        run_async(scenario())
        assert app.dismissed == ("abort", None)
