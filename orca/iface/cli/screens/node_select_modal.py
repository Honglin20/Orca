"""node_select_modal.py —— SKIP 时选目标 node 的 ModalScreen（phase 11 §9 P4）。

回答「用户选 SKIP 后，怎么让他挑跳到哪个 node？」：Textual ``ModalScreen[str | None]``。
``InterruptModal`` dismiss ``("skip", None)`` 后，OrcaApp push 本 modal；用户选一个 node
→ dismiss 返回目标 node 名；Esc / 取消 → dismiss 返回 None（取消 skip，回到 workflow）。

设计（SPEC §9.1 / §10.2 item12）：
  - 列出 workflow 所有 node 名（排除当前 node；含 parallel 组名）。
  - 顶部含一个 **「route-default (next)」** 选项（=``None`` 语义，走兜底 route / 默认下一 node），
    覆盖 §10.2 item12「当前 node 有 when=None 兜底 route」的快速路径。
  - 选择列表用 ``OptionList``（Textual 原生，键盘上下选 + Enter 确认）。
  - Esc = 取消（dismiss None，不 skip，回到 workflow 原状）。

与 InterruptModal 的边界（pattern A：InterruptModal → app 推本 modal）：
  - InterruptModal 专注「continue/skip/abort + guidance」三选一。
  - 本 modal 专注「skip 到哪」，由 app 串联（保持各自单一职责，不相互 import）。

依赖单向：本模块只 import textual + 标准库，不 import run/exec/events（SPEC §6.0 铁律 4）。
``candidate_nodes`` 由调用方（app）从 ``wf`` 派生后传入（本 modal 不依赖 schema）。
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option


# 「route-default (next)」选项的固定 id（dismiss 返回 None 语义走此 id）。
_ROUTE_DEFAULT_ID = "__route_default__"


class NodeSelectModal(ModalScreen[str | None]):
    """SKIP 选目标 node 的模态（SPEC §9.1 / §10.2 item12）。

    ``dismiss`` 返回目标 node 名（str）；用户选「route-default」或 Esc → 返回 None
    （= 走 route 求值 / 取消 skip）。
    """

    DEFAULT_CSS = """
    NodeSelectModal {
        align: center middle;
    }
    NodeSelectModal > Vertical {
        width: 64;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }
    NodeSelectModal Label.title {
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }
    NodeSelectModal Label.detail {
        margin-bottom: 1;
        color: $text-muted;
    }
    NodeSelectModal OptionList {
        height: auto;
        max-height: 16;
        border: solid $accent;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "取消", show=False),
    ]

    def __init__(self, current_node: str, candidate_nodes: list[str]) -> None:
        super().__init__()
        self.current_node = current_node
        # 候选 = 调用方传入的全部 node 名（已排除当前 node，由 app 派生时过滤）。
        # 防御性再滤一次（双保险，避免重复 / 当前 node 混入）。
        seen: set[str] = set()
        deduped: list[str] = []
        for n in candidate_nodes:
            if n == current_node or n in seen:
                continue
            seen.add(n)
            deduped.append(n)
        self.candidate_nodes = deduped
        # dismiss 已发生标记（防重复 dismiss 触发 ScreenStackError，与 InterruptModal 同边界）。
        self._dismissed: bool = False

    # ── 渲染 ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical(id="node-select-modal"):
            yield Label(
                f"⏭ SKIP · 跳到哪个 node？（当前 node={self.current_node}）",
                classes="title", id="node-select-title",
            )
            yield Label(
                "上下选 + Enter 确认；Esc 取消（不 skip，回到 workflow）",
                classes="detail",
            )
            option_list: OptionList = OptionList(id="node-select-list")
            # 顶部固定「route-default (next)」选项（走兜底 route / 默认下一 node）。
            option_list.add_option(Option(
                "route-default (next) —— 沿兜底 route 跳",
                id=_ROUTE_DEFAULT_ID,
            ))
            # 候选 node（已排除当前 node）。
            for name in self.candidate_nodes:
                option_list.add_option(Option(name, id=name))
            yield option_list

    # ── 用户答 ─────────────────────────────────────────────────────────────

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected,
    ) -> None:
        """用户选了一个 option → dismiss 返回目标 node 名（route-default → None）。"""
        if self._dismissed:
            return  # 防重复 dismiss
        option_id = event.option.id
        self._dismissed = True
        if option_id == _ROUTE_DEFAULT_ID:
            self.dismiss(None)  # 走 route 求值
        elif option_id is not None:
            self.dismiss(option_id)  # 显式目标
        else:
            # 无 id 的 option（不应发生，防御）—— 视为取消。
            self.dismiss(None)

    def action_cancel(self) -> None:
        """Esc = 取消（不 skip，回到 workflow 原状，dismiss None）。"""
        if self._dismissed:
            return
        self._dismissed = True
        self.dismiss(None)
