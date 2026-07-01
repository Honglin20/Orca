"""interrupt_modal.py —— 中断 workflow 纠偏的 ModalScreen（phase 11 SPEC §3.1）。

回答「用户按 Ctrl+G 时怎么弹 UI 让他选 continue/skip/abort + 填纠偏话？」：Textual
``ModalScreen[tuple[str, str | None]]``，由 OrcaApp ``action_interrupt`` 经
``push_screen_wait`` 弹出，dismiss 返回 ``(action, guidance)``。

设计原则（SPEC §3.1 / §6.0 铁律 5）：
  - **标题 + 当前 node + 已耗时**：``⏸ INTERRUPT · node=<X>（已跑 Ys）``。
  - **guidance TextArea（可选）**：CONTINUE 时把文本拼进后续 agent prompt 的
    ``[User Guidance]`` 段（SPEC §4.3）；SKIP/ABORT 忽略 guidance。
  - **3 按钮**：CONTINUE / SKIP / ABORT，button id 即 action 名（``continue``/``skip``/``abort``）。
  - **Esc = abort**：与 GateModal 的「Esc 安全优先」同语义（中止最保守）。

与 GateModal 的边界（SPEC §3.1 vs §4.5）：
  - GateModal 是「等决策」（工具权限 / agent 问答），返回 str（答案）。
  - InterruptModal 是「等用户意图」（中断纠偏），返回 tuple（动作 + 纠偏话）。
  两者各自独立类（语义不同），不共享基类（仅共享 broadcaster pattern 在 handler 层）。

依赖单向：本模块只 import textual + orca.gates.types（InterruptRequest 纯 dataclass），
不 import run/exec/events（SPEC §6.0 铁律 4：iface 是最上层，不被 import）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, TextArea

if TYPE_CHECKING:
    from orca.gates.types import InterruptRequest


class InterruptModal(ModalScreen[tuple[str, str | None]]):
    """中断纠偏模态（SPEC §3.1）。

    ``dismiss`` 返回 ``(action, guidance)``：action 是 ``"continue"``/``"skip"``/``"abort"``，
    guidance 是用户输入的纠偏话（CONTINUE 时可能非 None，其余 None）。
    """

    DEFAULT_CSS = """
    InterruptModal {
        align: center middle;
    }
    InterruptModal > Vertical {
        width: 72;
        max-width: 90%;
        height: auto;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }
    InterruptModal Label.title {
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }
    InterruptModal Label.detail {
        margin-bottom: 1;
        color: $text-muted;
    }
    InterruptModal TextArea {
        height: 5;
        margin-bottom: 1;
        border: solid $accent;
    }
    InterruptModal #interrupt-buttons {
        align-horizontal: center;
        margin-top: 1;
    }
    InterruptModal Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "abort", "中止", show=False),
    ]

    def __init__(self, ireq: InterruptRequest) -> None:
        super().__init__()
        self.ireq = ireq
        # dismiss 已发生标记（防重复 dismiss 触发 ScreenStackError，与 GateModal 同边界）。
        self._dismissed: bool = False

    # ── 渲染 ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        ireq = self.ireq
        with Vertical(id="interrupt-modal"):
            yield Label(
                f"⏸ INTERRUPT · node={ireq.node}", classes="title", id="interrupt-title",
            )
            elapsed = ireq.elapsed_at_request
            yield Label(
                f"当前 node 已跑 {elapsed:.1f}s（中断在 node 边界生效）",
                classes="detail",
            )
            yield Label("Guidance（可选，CONTINUE 时拼进后续 agent prompt）：", classes="detail")
            yield TextArea(id="guidance-input")
            with Vertical(id="interrupt-buttons"):
                yield Button("CONTINUE", id="gate-continue", variant="success")
                yield Button("SKIP", id="gate-skip", variant="warning")
                yield Button("ABORT", id="gate-abort", variant="error")

    # ── 用户答 ─────────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """用户点了按钮 → 反解 action + 取 guidance textarea → dismiss。"""
        if self._dismissed:
            return  # 防重复 dismiss
        # button id 即 action 名（continue / skip / abort），见 compose。
        action = _action_from_button(event.button.id)
        guidance = self._read_guidance(action)
        self._dismissed = True
        self.dismiss((action, guidance))

    def action_abort(self) -> None:
        """Esc = abort（最保守中止，与 GateModal Esc 安全优先同语义）。"""
        if self._dismissed:
            return
        self._dismissed = True
        self.dismiss(("abort", None))

    def _read_guidance(self, action: str) -> str | None:
        """CONTINUE 时取 guidance textarea 内容（空 → None）；SKIP/ABORT 忽略 guidance。"""
        if action != "continue":
            return None
        try:
            text = self.query_one("#guidance-input", TextArea).text.strip()
        except Exception:  # noqa: BLE001 —— widget 未就绪（极端），保守返回 None
            return None
        return text or None


# ── 纯函数 helper（测试可单独断言）──────────────────────────────────────────


def _action_from_button(button_id: str | None) -> str:
    """button id → action 名。id 形如 ``gate-continue`` / ``gate-skip`` / ``gate-abort``。

    与 GateModal 的 ``_answer_from_button`` 同 pattern（id 反解语义值）。
    未知 id 兜底 ``abort``（最保守，安全优先）。
    """
    if button_id == "gate-continue":
        return "continue"
    if button_id == "gate-skip":
        return "skip"
    # gate-abort 或未知 id → abort（保守）
    return "abort"
