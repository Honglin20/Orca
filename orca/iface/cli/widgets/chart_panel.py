"""chart_panel.py —— 图表集合投影（phase-12 SPEC §2.2 §4.3 §6.4）。

内部持 ``node -> label -> title -> ChartPayload`` 的**确定性 fold**（与
``DagTree._node_status`` 同模式，壳无真相）。由 ``_dispatch_to_widgets`` 的 chart
分支调 ``upsert`` 维护；同 ``label+title`` 幂等替换（对齐 phase-9d §2.7 实时更新）。

设计原则：
  - **壳无真相**：真相永远在 tape；投影是 ``tape.replay()`` 过滤 ``custom(chart)`` 的
    确定性 fold——清空→重放同段事件→投影完全一致（SPEC §6.0.3 单测证伪）。
  - **幂等**：``upsert`` 同输入同输出（同 label+title 替换不堆积）。
  - **公共 API**：``all_charts()`` 供 ``ChartBrowser`` 跨节点浏览（不读 ``_projection`` 私有）。
  - **依赖单向**：仅 import textual + stdlib；**不** import ``orca.exec`` / ``orca.run``
    / ``orca.iface.mcp`` / chart-producer（SPEC §0.3）。
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from textual.widgets import Static

from orca.iface.cli.widgets.chart_canvas import ChartCanvas

logger = logging.getLogger(__name__)

# workflow 级图表桶名（``node=None`` 归此；SPEC §3.3 决策 D2-a）。
WORKFLOW_BUCKET = "__workflow__"


class ChartPanel(Static):
    """图表集合：按 label 分组、同 label+title 幂等替换（SPEC §2.1/§2.2/§4.3）。

    用法（由 NodeDetail 转发，或 ChartBrowser 读）::

        panel = ChartPanel()
        panel.upsert("analyze", {"chart_type": "line", "label": "loss",
                                 "title": "epoch-loss", "data": [...]})
        panel.charts_for("analyze")        # -> {"loss": [<payload>]}
        list(panel.all_charts())           # -> [("analyze", {"loss": [...]}), ...]

    确定性 fold：``_projection`` 完全由 upsert 调用序列决定——清空后重放同样序列，
    投影必然一致（SPEC §6.0.3 单测证伪）。
    """

    DEFAULT_CSS = """
    ChartPanel {
        height: 1fr;
        padding: 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__("", id="chart-panel")
        # node_key -> label -> title -> payload（同 label+title 幂等替换）。
        self._projection: dict[str, dict[str, dict[str, dict]]] = {}
        # 焦点图（聚焦时 j/k 切）：(node_key, label, title) 或 None。
        self._focus: tuple[str, str, str] | None = None
        # 当前展示的节点 key（NodeDetail 透传 _selected）。
        self._node_key: str | None = None
        self._canvas = ChartCanvas()

    # ── 投影维护（确定性 fold）────────────────────────────────────────────

    def upsert(self, node_key: str | None, payload: dict) -> None:
        """幂等 upsert（SPEC §2.7 同 label+title 替换不堆积）。

        残缺 payload（缺 ``chart_type`` / ``data`` 非 list）→ 跳过 + warning（SPEC §6.4）。
        ``node_key=None`` → 归 ``__workflow__`` 桶（SPEC §3.3 D2-a）。
        """
        bucket = node_key if node_key is not None else WORKFLOW_BUCKET
        if not isinstance(payload, dict):
            logger.warning("chart payload 非 dict，跳过: %r", type(payload).__name__)
            return
        ctype = payload.get("chart_type")
        label = payload.get("label")
        title = payload.get("title")
        data = payload.get("data")
        if not ctype or not isinstance(ctype, str):
            logger.warning("chart payload 缺 chart_type，跳过: %r", payload)
            return
        if not isinstance(data, list):
            logger.warning("chart payload data 非 list，跳过: %r", type(data).__name__)
            return
        if not label or not title:
            # SPEC §2.1/§2.7 + web types.ts：``label``/``title`` 必填（分组键 + 唯一键）。
            # 缺它们无法分组/去重（破坏 §2.7 同 label+title 替换语义），跳过 + warning。
            # SPEC §6.4 文本只列 chart_type/data，但 label/title 是契约级必填（types.ts 非 optional）。
            logger.warning("chart payload 缺 label/title，跳过: %r", payload)
            return
        # node -> label -> title -> payload（同 label+title 替换）。
        self._projection.setdefault(bucket, {}).setdefault(label, {})[title] = dict(payload)
        self._rerender()

    def clear(self) -> None:
        """清空投影（确定性 fold 测试用：清空→重放→一致）。"""
        self._projection.clear()
        self._focus = None
        self._rerender()

    # ── 查询（公共 API）────────────────────────────────────────────────────

    def charts_for(self, node_key: str) -> dict[str, list[dict]]:
        """返回 ``{label: [ChartPayload, ...]}``（去重后，保持插入顺序）。

        不含 ``__workflow__`` 桶（那是 workflow 级，ChartBrowser 单独列顶层）。
        """
        labels = self._projection.get(node_key, {})
        return {label: list(titles.values()) for label, titles in labels.items()}

    def all_charts(self) -> Iterator[tuple[str, dict[str, list[dict]]]]:
        """迭代 ``(node_key, {label: [charts]})``，``__workflow__`` 永远在前（顶层）。

        SPEC §4.5：ChartBrowser 顶层展示 workflow 级图。
        """
        if WORKFLOW_BUCKET in self._projection:
            labels = self._projection[WORKFLOW_BUCKET]
            yield WORKFLOW_BUCKET, {
                label: list(titles.values()) for label, titles in labels.items()
            }
        for node_key, labels in self._projection.items():
            if node_key == WORKFLOW_BUCKET:
                continue
            yield node_key, {
                label: list(titles.values()) for label, titles in labels.items()
            }

    def count_for(self, node_key: str) -> int:
        """某节点的图表数（用于 ``图表(n)`` tab 标题）。不含 workflow 桶。"""
        labels = self._projection.get(node_key, {})
        return sum(len(titles) for titles in labels.values())

    # ── 节点绑定（NodeDetail 透传 _selected）────────────────────────────────

    def set_node(self, node_key: str | None) -> None:
        """切换展示的节点 key（NodeDetail.set_node 透传）。"""
        self._node_key = node_key
        # 切节点时重置焦点到该节点第一张图（若有）。
        self._focus = self._first_chart_key(node_key)
        self._rerender()

    def _first_chart_key(self, node_key: str | None) -> tuple[str, str, str] | None:
        if node_key is None:
            return None
        labels = self._projection.get(node_key, {})
        for label, titles in labels.items():
            for title in titles:
                return (node_key, label, title)
        return None

    # ── 焦点图导航（聚焦时 j/k）────────────────────────────────────────────

    def focus_next(self) -> None:
        keys = self._flat_keys(self._node_key)
        if not keys:
            return
        if self._focus is None or self._focus not in keys:
            self._focus = keys[0]
        else:
            idx = keys.index(self._focus)
            self._focus = keys[(idx + 1) % len(keys)]
        self._rerender()

    def focus_prev(self) -> None:
        keys = self._flat_keys(self._node_key)
        if not keys:
            return
        if self._focus is None or self._focus not in keys:
            self._focus = keys[-1]
        else:
            idx = keys.index(self._focus)
            self._focus = keys[(idx - 1) % len(keys)]
        self._rerender()

    def _flat_keys(self, node_key: str | None) -> list[tuple[str, str, str]]:
        """当前节点 (label, title) 扁平列表（插入序）。"""
        if node_key is None:
            return []
        labels = self._projection.get(node_key, {})
        out: list[tuple[str, str, str]] = []
        for label, titles in labels.items():
            for title in titles:
                out.append((node_key, label, title))
        return out

    # ── 渲染 ──────────────────────────────────────────────────────────

    def _rerender(self) -> None:
        node_key = self._node_key
        if node_key is None or node_key not in self._projection:
            self.update("暂无图表")
            return
        charts = self.charts_for(node_key)
        if not charts:
            self.update("暂无图表")
            return
        # 焦点图大图 + 其余列表。
        focus = self._focus
        lines: list[str] = []
        for label, payload_list in charts.items():
            lines.append(f"┌ charts · {label} ─────────────────")
            for payload in payload_list:
                title = payload.get("title", "")
                key = (node_key, label, title)
                marker = "▶" if focus == key else "◦"
                lines.append(f"{marker} {title}")
            # 焦点图大图渲染。
            if focus and focus[1] == label:
                fp = self._find_payload(focus)
                if fp is not None:
                    self._canvas.render_payload(fp)
                    # 取 canvas 最近渲染文本（公共 last_rendered，不扒 Static 私有）。
                    rendered = self._canvas.last_rendered
                    if rendered:
                        lines.append("  " + rendered.replace("\n", "\n  "))
            lines.append("└────────────────────────────────────")
        lines.append("[j/k 切焦点图 · C 全屏]")
        self.update("\n".join(lines))

    def _find_payload(self, key: tuple[str, str, str]) -> dict | None:
        node_key, label, title = key
        return self._projection.get(node_key, {}).get(label, {}).get(title)

    @property
    def projection(self) -> dict[str, dict[str, dict[str, dict]]]:
        """内部投影（仅供确定性 fold 测试断言，非公共 API）。"""
        return self._projection

    @property
    def canvas(self) -> ChartCanvas:
        """内部 canvas（测试用）。"""
        return self._canvas


__all__ = ["ChartPanel", "WORKFLOW_BUCKET"]
