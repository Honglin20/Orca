"""test_node_select_modal.py —— NodeSelectModal 黑盒测试（phase 11 §9 P4）。

headless Textual ``run_test()`` pilot 测试。覆盖：
  - compose：含「route-default (next)」选项 + 候选 node 列表（排除当前 node）。
  - 选具体 node → dismiss 返回该 node 名。
  - 选「route-default」→ dismiss 返回 None（走 route 求值）。
  - Esc → dismiss 返回 None（取消 skip，回到 workflow）。
  - 当前 node 不出现在候选列表。

测试模式（与 test_interrupt_modal.py 同款）：``_ModalApp`` push NodeSelectModal，
dismiss 返回值存 ``self.dismissed``。
"""

from __future__ import annotations

import asyncio

from textual import work
from textual.app import App, ComposeResult
from textual.widgets import Label

from orca.iface.cli.screens.node_select_modal import NodeSelectModal


def run_async(coro):
    return asyncio.run(coro)


class _ModalApp(App):
    """push NodeSelectModal 并捕获 dismiss 返回值的临时 app。"""

    def __init__(self, current_node: str, candidates: list[str]) -> None:
        super().__init__()
        self.current_node = current_node
        self.candidates = candidates
        self.dismissed: str | None | object = "__pending__"  # sentinel：未 dismiss

    def compose(self) -> ComposeResult:
        yield Label("bg")

    def on_mount(self) -> None:
        self._run_modal()

    @work
    async def _run_modal(self) -> None:
        result = await self.push_screen_wait(
            NodeSelectModal(self.current_node, self.candidates)
        )
        self.dismissed = result
        self.exit()


# ── compose ────────────────────────────────────────────────────────────────


class TestCompose:
    def test_lists_route_default_plus_candidates_excluding_current(self):
        """compose：含 route-default 选项 + 候选 node（排除当前 node）。"""
        app = _ModalApp(
            current_node="configurator",
            candidates=["analyzer", "configurator", "runner"],
        )

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                modal = app.screen
                assert isinstance(modal, NodeSelectModal)
                # modal 内部 candidate_nodes 已排除当前 node + 去重。
                assert "configurator" not in modal.candidate_nodes
                assert "analyzer" in modal.candidate_nodes
                assert "runner" in modal.candidate_nodes

        run_async(scenario())

    def test_title_shows_current_node(self):
        app = _ModalApp(current_node="runner", candidates=["analyzer"])

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                modal = app.screen
                assert isinstance(modal, NodeSelectModal)
                title = modal.query_one("#node-select-title")
                assert "runner" in str(title.render())

        run_async(scenario())


# ── 选择具体 node ────────────────────────────────────────────────────────────


class TestSelectNode:
    def test_select_candidate_returns_its_name(self):
        """选列表里第二个候选（runner）→ dismiss 返回 "runner"。

        OptionList 初始无 highlight；按 down 进入 highlight=index 0，再 down 两次到 runner。
        """
        app = _ModalApp(
            current_node="analyzer",
            candidates=["configurator", "runner"],
        )

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                # option list：[0]=route-default, [1]=configurator, [2]=runner
                await pilot.press("down")  # 进入 highlight → index 0 (route-default)
                await pilot.press("down")  # → index 1 (configurator)
                await pilot.press("down")  # → index 2 (runner)
                await pilot.press("enter")  # 选中
                for _ in range(3):
                    await pilot.pause()

        run_async(scenario())
        assert app.dismissed == "runner"

    def test_select_route_default_returns_none(self):
        """选 route-default（index=0）→ dismiss 返回 None（走 route 求值）。"""
        app = _ModalApp(
            current_node="analyzer",
            candidates=["configurator", "runner"],
        )

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                await pilot.press("down")  # 进入 highlight → index 0 (route-default)
                await pilot.press("enter")  # 选中 index 0
                for _ in range(3):
                    await pilot.pause()

        run_async(scenario())
        assert app.dismissed is None


# ── Esc = 取消（不 skip）────────────────────────────────────────────────────


class TestEscape:
    def test_escape_returns_none_cancel_skip(self):
        """Esc → dismiss None（取消 skip，回到 workflow 原状）。"""
        app = _ModalApp(
            current_node="analyzer",
            candidates=["configurator", "runner"],
        )

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                await pilot.pause()

        run_async(scenario())
        assert app.dismissed is None
