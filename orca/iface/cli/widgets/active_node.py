"""active_node.py —— 右上「当前/选中节点详情」面板（SPEC §4.2）。

回答「当前 node 在干嘛？」：显示选中节点（默认 current node）的行摘要
（节点名 + 状态 + executor）+ 流式事件（agent_message/thinking/tool_call/tool_result）。

设计原则：
  - **壳无真相**：widget 内文本全部由注入的事件派生；不订阅 bus。
  - **纯渲染**：只 append 文本，不做业务判断（业务在 reducer / executor）。
"""

from __future__ import annotations

from textual.widgets import Static


class ActiveNode(Static):
    """选中节点的实时详情面板（SPEC §4.2）。

    用法（由 OrcaApp 驱动）::

        panel = app.query_one(ActiveNode)
        panel.set_active("research")          # 切换选中节点（清空 + 显示标题）
        panel.append_line("[r_a] tool: WebSearch")  # 流式事件 append
    """

    DEFAULT_CSS = """
    ActiveNode {
        width: 3fr;
        height: 1fr;
        border: round $accent;
        padding: 0 1;
        background: $surface;
    }
    """

    def __init__(self) -> None:
        super().__init__("", id="active-node")
        self._active: str | None = None

    def set_active(self, name: str | None) -> None:
        """切换选中节点。``None`` 清空。"""
        self._active = name
        if name is None:
            self.update("")
        else:
            self.update(f"ACTIVE NODE: {name}")

    @property
    def active(self) -> str | None:
        return self._active

    def append_line(self, line: str) -> None:
        """在当前内容下追加一行（流式事件渲染）。

        简单实现：read-modify-write（性能足够本场景的事件率；高吞吐归 phase 9 web）。
        """
        current = str(self.renderable) if self.renderable is not None else ""
        # 去掉已有 title 行后 append，避免 title 重复渲染（用 ``\\n`` 连接）。
        title = f"ACTIVE NODE: {self._active}" if self._active else ""
        body = current[len(title):].lstrip("\n") if title else current
        new_body = (body + "\n" + line) if body else line
        self.update(f"{title}\n{new_body}" if title else new_body)
