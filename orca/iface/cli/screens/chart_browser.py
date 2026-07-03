"""chart_browser.py —— 全屏跨节点图表浏览（phase-12 SPEC §4.5 §6.5）。

``ChartBrowser(ModalScreen)``：``C`` 进入，树状导航（node_key/label，``__workflow__``
顶层）+ 大图预览（``ChartCanvas``）。数据源 = ``NodeDetail.all_charts()``（公共 API，
不读 ``_projection`` 私有）。``Esc/q`` 退出。

设计原则：
  - **壳无真相**：数据从 ``NodeDetail`` 的确定性 fold 投影读，不订阅 bus。
  - **依赖单向**：仅 import textual + stdlib + 本包 widget；**不** import ``orca.exec``
    / ``orca.run`` / ``orca.iface.mcp`` / chart-producer（SPEC §0.3）。
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import ListItem, ListView, Static

from orca.iface.cli.widgets import NodeDetail
from orca.iface.cli.widgets.chart_canvas import ChartCanvas
from orca.iface.cli.widgets.chart_panel import WORKFLOW_BUCKET


class ChartBrowser(ModalScreen):
    """全屏跨节点图表浏览（SPEC §4.5）。

    数据源 = ``app.query_one(NodeDetail).all_charts()``（含 ``__workflow__`` 桶，
    永远顶层）。左侧 ListView 列全部图（node_key / label / title），选中后右侧
    ChartCanvas 大图预览。``Esc/q`` 退出。
    """

    CSS = """
    ChartBrowser {
        layout: vertical;
    }
    #cb-main {
        height: 1fr;
    }
    #cb-list {
        width: 40;
        border: round $primary;
    }
    #cb-preview {
        width: 1fr;
    }
    """

    BINDINGS = [
        Binding("escape", "app.pop_screen", "退出", show=True),
        Binding("q", "app.pop_screen", "退出", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        # 扁平化的图表项：(node_key, label, payload) 列表（__workflow__ 在前）。
        self._items: list[tuple[str, str, dict]] = []

    # ── compose ────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Horizontal(id="cb-main"):
            yield ListView(id="cb-list")
            with Vertical(id="cb-preview"):
                yield Static("（选一张图预览）", id="cb-preview-title")
                yield ChartCanvas()

    def on_mount(self) -> None:
        """从 NodeDetail.all_charts() 拉数据 + 填 ListView。"""
        try:
            nd = self.app.query_one(NodeDetail)
        except Exception:  # noqa: BLE001 —— NodeDetail 不在树（极端）
            return
        # all_charts() yield (node_key, {label: [payloads]})，__workflow__ 在前。
        items: list[tuple[str, str, dict]] = []
        for node_key, labels in nd.all_charts():
            for label, payload_list in labels.items():
                for payload in payload_list:
                    items.append((node_key, label, payload))
        self._items = items

        lv = self.query_one("#cb-list", ListView)
        for node_key, label, payload in items:
            title = payload.get("title", "?")
            ctype = payload.get("chart_type", "?")
            # __workflow__ 显示为顶层标识；其余显示节点名。
            node_display = "workflow" if node_key == WORKFLOW_BUCKET else node_key
            lv.append(ListItem(Static(f"[{ctype}] {node_display} · {label} · {title}")))
        if not items:
            lv.append(ListItem(Static("（无图表）")))

    # ── 选中 → 大图预览 ──────────────────────────────────────────────────

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """ListView 选中项 → ChartCanvas 大图预览（SPEC §6.5）。"""
        if not self._items:
            return
        idx = event.list_view.index
        if idx is None or idx >= len(self._items):
            return
        node_key, label, payload = self._items[idx]
        title = payload.get("title", "")
        node_display = "workflow" if node_key == WORKFLOW_BUCKET else node_key
        self.query_one("#cb-preview-title", Static).update(
            f"{node_display} · {label} · {title}",
        )
        self.query_one(ChartCanvas).render_payload(payload)


__all__ = ["ChartBrowser"]
