"""chart_canvas.py —— 单个 ChartPayload 渲染器（phase-12 SPEC §1.2 §6.4）。

按 ``chart_type`` 分派：line/bar/area/scatter/pareto → plotext braille；table →
Textual ``DataTable``；radar / heatmap → DataTable 降级 +「见 Web」提示；未知 ``chart_type``
→ fail loud（对齐 web ``ChartWidget.tsx:30``）。

设计原则：
  - **plotext 主依赖**：import 时探测一次（缓存 ``_PLOTEXT_OK``）；完整 install 下
    line/bar/area/scatter/pareto **必须 braille 渲染**（SPEC §6.1）。缺包仅开发期
    monkeypatch 测试降级（SPEC §6.4），生产路径不降级。
  - **壳无真相**：``render_payload(payload)`` 只渲染传入的 dict；不订阅、不读 tape。
  - **依赖单向**：仅 import textual + plotext（探测式）+ stdlib + ``orca.chart._limits``
    纯常量层（允许：_limits 零依赖、是 chart 包常量真相源，与 events/chart_ingestor 同模式）；
    **不** import ``orca.exec`` / ``orca.run`` / ``orca.iface.mcp`` / chart-producer 渲染逻辑
    （SPEC §0.3）。
"""

from __future__ import annotations

import logging
from typing import Any

from textual.widgets import Static

from orca.chart._limits import ALLOWED_CHART_TYPES

logger = logging.getLogger(__name__)

# plotext import 探测（一次性缓存，SPEC §1.2）。完整 install 下为 True；缺包则 False
# （开发期 monkeypatch 测试模拟）。生产依赖 textual-plotext，正常恒为 True。
_PLOTEXT_OK: bool
try:
    import plotext as _plt  # type: ignore[import-not-found]

    _PLOTEXT_OK = True
except Exception:  # noqa: BLE001 —— 缺包是开发期降级路径，生产不会触发
    _plt = None  # type: ignore[assignment]
    _PLOTEXT_OK = False
    logger.warning(
        "plotext 未安装：line/bar/area/scatter/pareto 将降级为 DataTable。"
        "生产 install 含 textual-plotext，不应缺包。",
    )

# chart_type 集合来自 ``_limits.ALLOWED_CHART_TYPES``（单一来源，与 web/backend/events/ingestor
# 同源；防三端 drift）。复制 allowlist 是项目硬规则明确禁止（_limits.py docstring）。
_CHART_TYPES = ALLOWED_CHART_TYPES
# 走 plotext braille 渲染的类型（SPEC §1.2 决策；radar/heatmap/table 不在其中）。
_PLOTEXT_TYPES = {"line", "bar", "area", "scatter", "pareto"}


class ChartCanvas(Static):
    """渲染单个 ``ChartPayload``（SPEC §1.2 / §4.3）。

    用法::

        canvas = ChartCanvas()
        canvas.render_payload({"chart_type": "line", "data": [...], ...})

    未知 ``chart_type`` → fail loud（显示「未知 chart_type: X」）。残缺 payload 的防御
    在 ChartPanel 层（upsert 时校验），此处假设 payload 已校验。
    """

    DEFAULT_CSS = """
    ChartCanvas {
        height: 1fr;
        border: round $accent;
        padding: 0 1;
        background: $surface;
    }
    """

    def __init__(self) -> None:
        super().__init__("", id="chart-canvas")
        self._last_type: str | None = None
        self._last_rendered: str = ""

    @property
    def last_rendered(self) -> str:
        """最近渲染的文本（公共 API，避免调用方扒 Static 私有 ``_Static__content``）。"""
        return self._last_rendered

    def render_payload(self, payload: dict) -> None:
        """渲染单个 ChartPayload dict（SPEC §1.2 chart_type 分派）。"""
        ctype = payload.get("chart_type")
        if ctype not in _CHART_TYPES:
            # fail loud：未知 chart_type 显式提示（不静默崩）。
            self._set_text(f"未知 chart_type: {ctype!r}")
            self._last_type = ctype
            return

        data = payload.get("data", [])
        title = payload.get("title", "")
        label = payload.get("label", "")

        if ctype == "table":
            self._render_table(data, payload.get("columns"), title)
        elif ctype == "radar":
            # 终端性价比低 → DataTable 降级 +「见 Web」提示（SPEC §1.2 决策）。
            self._render_table(
                data, payload.get("columns"), title, hint="（radar 终端降级，完整图见 Web）",
            )
        elif ctype == "heatmap":
            # 热力图终端性价比低（行×列色阶在文本态不可读）→ DataTable 降级 +「见 Web」
            # 提示（与 radar 同策略，SPEC §1.2 决策）。完整色阶渲染见 Web 面板。
            self._render_table(
                data, payload.get("columns"), title,
                hint="（heatmap 终端降级，完整色阶矩阵见 Web）",
            )
        elif ctype in _PLOTEXT_TYPES:
            if _PLOTEXT_OK:
                self._render_plotext(ctype, payload, title, label)
            else:
                # 缺包降级（仅开发期测试路径）：DataTable + 提示。
                self._render_table(
                    data, payload.get("columns"), title,
                    hint=f"（{ctype} plotext 缺包，已降级 DataTable）",
                )
        else:  # 不可达（_CHART_TYPES 已穷尽），防御 fail loud。
            self._set_text(f"未知 chart_type: {ctype!r}")
        self._last_type = ctype

    def _set_text(self, text: str) -> None:
        """``update`` + 记录最近渲染文本（公共 ``last_rendered`` 读它，不扒 Static 私有）。"""
        self._last_rendered = text
        self.update(text)

    # ── plotext 渲染（line/bar/area/scatter/pareto）─────────────────────────

    def _render_plotext(
        self, ctype: str, payload: dict, title: str, label: str,
    ) -> None:
        """用 plotext 渲染图表为 braille 文本，写入 Static。

        SPEC §6.1 必测：完整 install 下 line chart 必须 braille 渲染。
        """
        assert _plt is not None  # 调用方已查 _PLOTEXT_OK
        _plt.clc()  # clear canvas（plotext 5 用 clc，旧版 clear）
        try:
            _plt.clf()  # clear figure
        except Exception:  # noqa: BLE001
            pass
        data = payload.get("data", [])
        x_key = payload.get("x")
        y_key = payload.get("y")
        hue = payload.get("hue")

        if not isinstance(data, list) or not data:
            self._set_text(f"[{label}] {title}\n（无数据）")
            return

        # 按 hue 分系列（若指定）；否则单系列。
        series: dict[str, list[float]] = {}
        xs_per: dict[str, list] = {}
        records = data
        if hue and isinstance(records[0], dict) and hue in records[0]:
            for rec in records:
                key = str(rec.get(hue, "default"))
                series.setdefault(key, []).append(_to_num(rec.get(y_key)))
                xs_per.setdefault(key, []).append(
                    _to_num(rec.get(x_key)) if x_key else len(series[key]),
                )
        else:
            ys = [_to_num(rec.get(y_key)) if y_key and isinstance(rec, dict)
                  else _to_num(rec) for rec in records]
            xs = ([_to_num(rec.get(x_key)) for rec in records] if x_key
                  else list(range(1, len(ys) + 1)))
            series["__main__"] = ys
            xs_per["__main__"] = xs

        for sname, ys in series.items():
            xs = xs_per[sname]
            marker = "braille"
            if ctype == "line":
                _plt.plot(xs, ys, marker=marker, label=sname if sname != "__main__" else None)
            elif ctype == "area":
                # plotext 5：填充用 ``filly``（向上填充）；``fill=`` 仅 bar 支持。
                _plt.plot(xs, ys, marker=marker, filly="up",
                          label=sname if sname != "__main__" else None)
            elif ctype == "scatter":
                _plt.scatter(xs, ys, marker=marker,
                             label=sname if sname != "__main__" else None)
            elif ctype == "bar":
                _plt.bar(xs, ys, marker=marker,
                         label=sname if sname != "__main__" else None)
            elif ctype == "pareto":
                # pareto = 排序后 bar + 累积折线。
                direction = payload.get("pareto_direction", "max")
                pairs = sorted(zip(xs, ys), key=lambda p: p[1],
                               reverse=(direction == "max"))
                xs_s = [p[0] for p in pairs]
                ys_s = [p[1] for p in pairs]
                _plt.bar(xs_s, ys_s, marker=marker)
                # 累积百分比折线。
                total = sum(ys_s) or 1
                cum = []
                acc = 0.0
                for v in ys_s:
                    acc += v
                    cum.append(acc / total * 100)
                _plt.plot(xs_s, cum, marker=marker)

        if title:
            _plt.title(title)
        # 限定 canvas 大小（让 build 输出可控宽度）。
        try:
            _plt.plotsize(None, 20)
        except Exception:  # noqa: BLE001
            pass
        rendered = _plt.build()
        prefix = f"[{label}] " if label else ""
        self._set_text(prefix + rendered)

    # ── DataTable 降级 / table 类型 ─────────────────────────────────────────

    def _render_table(
        self,
        data: list,
        columns: list[str] | None,
        title: str,
        hint: str = "",
    ) -> None:
        """渲染为文本表格（table 类型 / 降级路径）。

        不用 Textual ``DataTable`` 子组件（会引入 compose/refresh 时序复杂度）；用纯文本
        对齐表格写入 Static——足够 NodeDetail 图表 tab 的可读性，且 headless 测试简单。
        """
        if not isinstance(data, list) or not data:
            self._set_text(f"{title}\n（无数据）{hint}".strip())
            return
        # 推导列。
        if columns:
            cols = list(columns)
        elif isinstance(data[0], dict):
            cols = list(data[0].keys())
        else:
            cols = ["value"]

        # 渲染对齐表格。
        header = " | ".join(str(c) for c in cols)
        sep = "-+-".join("-" * max(len(str(c)), 4) for c in cols)
        rows = []
        for rec in data[:50]:  # 限制 50 行避免撑爆终端
            if isinstance(rec, dict):
                rows.append(" | ".join(_truncate(rec.get(c)) for c in cols))
            else:
                rows.append(_truncate(rec))
        body = "\n".join([header, sep, *rows])
        head = f"{title}\n" if title else ""
        tail = f"\n{hint}" if hint else ""
        self._set_text(head + body + tail)

    @property
    def last_type(self) -> str | None:
        """最近渲染的 chart_type（测试用）。"""
        return self._last_type

    @property
    def plotext_available(self) -> bool:
        """plotext 是否可用（测试用 + 降级判定）。"""
        return _PLOTEXT_OK


def _to_num(v: Any) -> float:
    """值 → float（无法转 → 0.0）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _truncate(v: Any, width: int = 30) -> str:
    """值 → 短文本（截断 + …）。"""
    s = str(v)
    if len(s) > width:
        return s[: width - 1] + "…"
    return s


__all__ = ["ChartCanvas"]
