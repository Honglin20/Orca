"""dialog_modal.py —— agent 跑完后多轮追问的 ModalScreen（phase 11 SPEC §6.1）。

回答「用户按 d 时怎么弹 UI 让他就一个已完成的 agent output 多轮追问？」：Textual
``ModalScreen[None]``，由 OrcaApp ``action_dialog`` 经 ``push_screen`` 弹出（**非**
``push_screen_wait``——dialog 是 fire-and-forget，dismiss 不返回值，所有交互在 modal 内完成）。

设计原则（SPEC §6.1 / §6.2 / §6.3）：
  - **标题 + agent output 摘要**：``💬 DIALOG · node=<X>`` + output 前 ~200 字符预览面板。
  - **滚动历史区**：``RichLog`` 显示 ``user>`` / ``agent>`` 交替的对话 transcript。
  - **Input + 发送/结束 按钮**：用户敲问题回车或点「发送」→ 触发 ``handler.send_turn``（@work
    异步，UI 不卡）；点「结束对话」→ ``handler.end_dialog`` → dismiss。
  - **Esc = 结束**：与 InterruptModal 的「Esc = abort」同语义（Esc 等价结束 dialog）。

与 InterruptModal 的区别（SPEC §6.3）：
  - InterruptModal：node 跑**中**纠偏，dismiss 返回 ``(action, guidance)`` 给 orchestrator。
  - DialogModal：node 跑**完后**追问，dismiss 无返回值（dialog 不影响 DAG 推进，仅写 tape）。
  两者各自独立类（语义不同），不共享基类。

**3-method split 的 UI 适配（PLAN correction #7）**：DialogHandler 拆 ``start_dialog`` /
``send_turn`` / ``end_dialog`` 正是为了本 modal——每轮 send_turn 完成后控制权交还 UI 让用户
敲下一句（单阻塞 ``run_dialog`` 做不到这点）。本 modal 在 ``__init__`` 调 ``start_dialog`` 拿
``dialog_id``，每轮「发送」按钮触发独立 ``@work`` worker 调 ``send_turn``。

依赖单向（铁律 4）：本模块 import ``orca.gates.dialog``（DialogHandler）+ textual。
``orca.iface.cli`` → ``orca.gates`` 是允许方向（iface 是最上层）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RichLog

if TYPE_CHECKING:
    from orca.events.bus import EventBus
    from orca.exec.context import RunContext
    from orca.gates.dialog import DialogHandler

logger = logging.getLogger(__name__)


class DialogModal(ModalScreen[None]):
    """多轮对话模态（SPEC §6.1）。

    ``dismiss`` 返回 ``None``（dialog 是 fire-and-forget——交互在 modal 内完成，dialog_*
    事件已写 tape，OrcaApp 不需要返回值）。OrcaApp 用 ``push_screen``（非 wait）弹本屏。

    Args:
        handler: DialogHandler 实例（start/send/end 三方法）。
        node: 用户追问的目标 node 名。
        agent_output: 该 node 的产出（任意对象，喂给 start_dialog 作 system context）。
        ctx: 当前 RunContext（透传给 handler 三方法）。
        bus: EventBus（start_dialog 已在 handler 内 emit，本参数预留给 modal 自身写错误提示
            到 LogStream——当前实现直接写 RichLog，不写 bus，故 bus 仅留作扩展。YAGNI：暂不删，
            因为 modal 未来可能 emit 一个 ``custom`` 事件记录 dialog transcript 摘要）。
    """

    DEFAULT_CSS = """
    DialogModal {
        align: center middle;
    }
    DialogModal > Vertical {
        width: 80;
        max-width: 95%;
        height: 24;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    DialogModal Label.title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    DialogModal Label.detail {
        margin-bottom: 1;
        color: $text-muted;
    }
    DialogModal #dialog-output-preview {
        height: 4;
        border: solid $primary;
        padding: 0 1;
        margin-bottom: 1;
        color: $text-muted;
    }
    DialogModal #dialog-history {
        height: 1fr;
        border: solid $success;
        padding: 0 1;
        margin-bottom: 1;
    }
    DialogModal #dialog-input {
        margin-bottom: 1;
    }
    DialogModal #dialog-buttons {
        align-horizontal: center;
    }
    DialogModal Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "end", "结束对话", show=False),
    ]

    def __init__(
        self,
        handler: DialogHandler,
        node: str,
        agent_output: Any,
        ctx: RunContext,
        bus: EventBus | None = None,
    ) -> None:
        super().__init__()
        self._handler = handler
        self._node = node
        self._agent_output = agent_output
        self._ctx = ctx
        self._bus = bus
        # dialog_id：__init__ 不调 start_dialog（async，需 event loop），改在 on_mount（@work）调。
        self._dialog_id: str | None = None
        # dismiss 已发生标记（防重复 dismiss，与 GateModal/InterruptModal 同边界）。
        self._dismissed: bool = False
        # 发送中（send_turn 在跑）：禁用发送按钮防重复提交。
        self._sending: bool = False

    # ── 渲染 ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-modal"):
            yield Label(
                f"💬 DIALOG · node={self._node}", classes="title", id="dialog-title",
            )
            yield Label("agent 之前的 output 摘要：", classes="detail")
            yield RichLog(id="dialog-output-preview", markup=False, wrap=True, auto_scroll=True)
            yield RichLog(id="dialog-history", markup=False, wrap=True, auto_scroll=True)
            yield Input(placeholder="输入追问后回车或点发送", id="dialog-input")
            with Vertical(id="dialog-buttons"):
                yield Button("发送", id="dialog-send", variant="success")
                yield Button("结束对话", id="dialog-end", variant="warning")

    def on_mount(self) -> None:
        """挂载后写 output 预览 + 起 start_dialog worker（拿 dialog_id）。"""
        preview = self._format_output_preview(self._agent_output)
        self.query_one("#dialog-output-preview", RichLog).write(preview)
        self.query_one("#dialog-history", RichLog).write("（dialog 已开始，输入问题开始追问）")
        self._start_dialog_work()

    @work(name="dialog-start")
    async def _start_dialog_work(self) -> None:
        """on_mount 起：调 handler.start_dialog（async emit dialog_started）拿 dialog_id。"""
        try:
            self._dialog_id = await self._handler.start_dialog(
                self._node, self._agent_output, self._ctx,
            )
        except Exception:  # noqa: BLE001 —— start 失败 fail loud 记 log，用户可 Esc 退出
            logger.exception("dialog start_dialog 异常")
            self._safe_write_history("(dialog 启动失败，按 Esc 退出)")

    # ── 用户交互 ──────────────────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """回车 = 发送（与「发送」按钮等价）。"""
        self._on_send()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """发送 / 结束 按钮。"""
        if self._dismissed:
            return
        if event.button.id == "dialog-send":
            self._on_send()
        elif event.button.id == "dialog-end":
            self._on_end()

    def action_end(self) -> None:
        """Esc = 结束对话（与 InterruptModal Esc=abort 同语义，安全退出）。"""
        self._on_end()

    def _on_send(self) -> None:
        """触发 send_turn worker：取 input 文本 → 清空 → append user> → 起 worker。

        发送中（_sending=True）时忽略新提交（防连点导致 history 错乱）。
        空文本不发送（无意义）。
        """
        if self._dismissed or self._sending:
            return
        if self._dialog_id is None:
            # dialog 还没 start 完（on_mount worker 未跑完）→ 提示稍后
            self._safe_write_history("(dialog 还在初始化，稍等再发)")
            return
        input_box = self.query_one("#dialog-input", Input)
        text = input_box.value.strip()
        if not text:
            return  # 空文本不发
        input_box.value = ""  # 清空输入框
        self._sending = True
        self._set_send_disabled(True)
        self._safe_write_history(f"user> {text}")
        self._send_turn_work(self._dialog_id, text)

    @work(name="dialog-send")
    async def _send_turn_work(self, dialog_id: str, user_text: str) -> None:
        """异步调 handler.send_turn：spawn claude（拼历史）拿 agent reply → append agent>。

        fail loud（SPEC §6 / 模块 docstring）：spawn 失败 → 在 history 显示错误（不静默丢一轮），
        交还控制让用户重试或结束。worker 内异常不冒泡出 modal（Textual worker 异常仅记 log）。
        """
        try:
            reply = await self._handler.send_turn(dialog_id, user_text, self._ctx)
            self._safe_write_history(f"agent> {reply}")
        except Exception as e:  # noqa: BLE001 —— spawn 失败 / 未知 dialog_id 等
            logger.exception("dialog send_turn 异常")
            self._safe_write_history(f"(本轮失败：{e}；可重试或结束对话)")
        finally:
            self._sending = False
            self._set_send_disabled(False)

    def _on_end(self) -> None:
        """结束对话 → 起 end_dialog worker → dismiss。"""
        if self._dismissed:
            return
        self._dismissed = True
        self._end_dialog_work()

    @work(name="dialog-end")
    async def _end_dialog_work(self) -> None:
        """异步调 handler.end_dialog（emit dialog_ended 写 tape）→ dismiss modal。

        dialog_id 可能 None（start 未跑完就 Esc）→ 跳过 end（无 state 可清），直接 dismiss。
        """
        if self._dialog_id is not None:
            try:
                await self._handler.end_dialog(self._dialog_id, self._ctx)
            except Exception:  # noqa: BLE001 —— end 失败不阻塞退出
                logger.exception("dialog end_dialog 异常")
        try:
            self.dismiss(None)
        except Exception:  # noqa: BLE001 —— modal 可能已被外部 pop
            pass

    # ── helpers ──────────────────────────────────────────────────────────

    def _safe_write_history(self, text: str) -> None:
        """写历史区（widget 可能已不在屏上——极端 race，安全忽略）。"""
        try:
            self.query_one("#dialog-history", RichLog).write(text)
        except Exception:  # noqa: BLE001
            pass

    def _set_send_disabled(self, disabled: bool) -> None:
        """发送中禁用发送按钮（防连点）。查询失败安全忽略。"""
        try:
            self.query_one("#dialog-send", Button).disabled = disabled
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _format_output_preview(agent_output: Any, limit: int = 200) -> str:
        """格式化 agent output 摘要（前 ~200 字符，可读单行/JSON）。"""
        try:
            import json as _json

            text = _json.dumps(agent_output, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(agent_output)
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"
