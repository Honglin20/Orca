"""gate_modal.py —— gate 人工确认 ModalScreen（SPEC §4.5）。

回答「gate 触发时怎么阻塞等用户答、不让背景冻结？」：Textual ``ModalScreen`` +
``push_screen_wait`` 由 OrcaApp 调用，阻塞编排 worker 但 UI 事件循环继续刷新
（DAG/日志，Textual 决定性优势，SPEC §1 决策 1）。

设计原则（SPEC §4.5 决策 5：gate 双重身份）：
  - 收到 ``human_decision_requested`` → OrcaApp push 本屏参与竞速。
  - 用户答（按钮 / 回车）→ ``dismiss(answer)`` → OrcaApp 拿到后调
    ``gate_handler.resolve(gate.id, answer, "cli")``。
  - 收到 ``human_decision_resolved``（别壳先答）→ OrcaApp 调 ``notify_resolved`` →
    本屏 dismiss 显示「已被 [source] 答」（广播输家）。

两种 source 渲染（SPEC §1.2）：
  - ``tool_permission``：显示工具名 + 参数 + ``[allow] [deny]`` 按钮。
  - ``agent_ask``：显示问题 + 选项按钮（无 options 则 ``Input`` 自由文本）。

依赖单向：本模块只 import textual + orca.gates.types（HumanGate 数据模型，纯 dataclass），
不 import run/exec/events。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

if TYPE_CHECKING:
    from orca.gates.types import HumanGate


class GateModal(ModalScreen[str]):
    """gate 人工确认模态（SPEC §4.5）。

    ``dismiss`` 的返回值是用户答案（选项文本或自由文本），由 OrcaApp 的
    ``push_screen_wait`` await 拿到。
    """

    DEFAULT_CSS = """
    GateModal {
        align: center middle;
    }
    GateModal > Vertical {
        width: 72;
        max-width: 90%;
        height: auto;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }
    GateModal Label.title {
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }
    GateModal Label.detail {
        margin-bottom: 1;
    }
    GateModal Button {
        margin: 0 1;
    }
    GateModal #gate-buttons {
        align-horizontal: center;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "取消", show=False),
    ]

    # gate.source 已是 Literal，这里只是渲染分支依据；常量避免拼写错。
    SOURCE_TOOL_PERMISSION = "tool_permission"
    SOURCE_AGENT_ASK = "agent_ask"

    def __init__(self, gate: HumanGate) -> None:
        super().__init__()
        self.gate = gate
        self._resolved_externally: tuple[str, str] | None = None  # (source, answer)
        # dismiss 已发生标记（无论赢家还是广播输家）。Textual ``dismiss`` 后本屏 pop 出栈，
        # 后续按钮 / Input / notify 调用都应在 widget 侧静默忽略，避免重复 dismiss 触发
        # ``ScreenStackError``（pop 空栈）。这是测试「先答后广播」用例发现的真实边界。
        self._dismissed: bool = False

    # ── 渲染 ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        gate = self.gate
        with Vertical(id="gate-modal"):
            yield Label(f"🔒 GATE: {gate.node or 'workflow'}", classes="title")
            yield Label(gate.prompt, classes="detail")
            # source 分支：tool_permission 显示工具+参数；agent_ask 显示选项/输入。
            if gate.source == self.SOURCE_TOOL_PERMISSION:
                ctx = gate.context or {}
                tool = ctx.get("tool", "<unknown>")
                tool_input = ctx.get("tool_input", {})
                yield Label(f"工具：{tool}", classes="detail")
                yield Label(f"参数：{_truncate(tool_input)}", classes="detail")
                # hook 桥固定 allow/deny，本屏渲染为中文按钮（answer 仍是 "allow"/"deny"）。
                with Vertical(id="gate-buttons"):
                    yield Button("批准 [allow]", id="gate-allow", variant="success")
                    yield Button("拒绝 [deny]", id="gate-deny", variant="error")
            else:  # agent_ask
                if gate.options:
                    with Vertical(id="gate-buttons"):
                        for opt in gate.options:
                            yield Button(opt, id=f"gate-opt-{_safe_id(opt)}")
                else:
                    yield Input(placeholder="输入答案后回车", id="gate-input")

    # ── 用户答（赢家路径）────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """用户点了选项按钮 → dismiss（按钮 id 或 label 反解出 answer）。"""
        if self._dismissed:
            return  # 别壳已答 / 用户已答，本屏的输入静默丢弃（防重复 dismiss）
        answer = _answer_from_button(self.gate, event.button)
        self._dismissed = True
        self.dismiss(answer)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """agent_ask 自由文本模式：用户回车 → dismiss 文本。"""
        if self._dismissed:
            return
        self._dismissed = True
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        """Esc 取消：tool_permission 视为 deny（安全优先），agent_ask 视为空串。"""
        if self._dismissed:
            return
        self._dismissed = True
        if self.gate.source == self.SOURCE_TOOL_PERMISSION:
            self.dismiss("deny")
        else:
            self.dismiss("")

    # ── 广播输家路径（SPEC §4.5 决策 5）─────────────────────────────────

    def notify_resolved_externally(self, source: str, answer: str) -> None:
        """收到 ``human_decision_resolved``（别壳先答）→ dismiss + 提示。

        OrcaApp 调本方法（不在 widget 内订阅 bus），传入赢家 source + answer。
        dismiss 传 ``__orca_broadcast__`` 哨兵：OrcaApp 的 ``push_screen_wait`` await
        收到后识别为「已被别壳答」，不再 resolve（赢家已 resolve 过），只 log 提示。
        """
        if self._dismissed:
            return
        self._dismissed = True
        self._resolved_externally = (source, answer)
        # 哨兵格式：``__orca_broadcast__:<source>:<answer>``（answer 可能含 ``:``，split 第一段）。
        self.dismiss(f"__orca_broadcast__:{source}:{answer}")


# ── 纯函数 helper（测试可单独断言）──────────────────────────────────────────


def _answer_from_button(gate: HumanGate, button: Button) -> str:
    """按钮 id / label → 标准 answer 字符串。

    tool_permission：按钮 id 是 ``gate-allow`` / ``gate-deny`` → answer 直接是
    ``allow`` / ``deny``（hook 桥要求的标准选项）。
    agent_ask：按钮 label 就是选项原文（直接当 answer 回传）。
    """
    if gate.source == "tool_permission":
        if button.id == "gate-allow":
            return "allow"
        if button.id == "gate-deny":
            return "deny"
    # agent_ask：button.label 即选项
    return str(button.label)


def _truncate(value: object, limit: int = 50) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _safe_id(text: str) -> str:
    """把选项文本转成合法 textual widget id（仅字母数字下划线连字符）。"""
    import re

    return re.sub(r"[^A-Za-z0-9_-]", "_", text)[:32] or "opt"
