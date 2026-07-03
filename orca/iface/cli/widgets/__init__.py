"""orca.iface.cli.widgets —— TUI 组件（Textual widgets）。

壳的渲染层。所有 widget **无业务真相**：状态来自注入的 Event（由 OrcaApp 从
EventBus 订阅后分发），widget 只负责把事件投影成可见字符。重启从 tape replay
重放同样的事件流，widget 渲染必然一致（SPEC §6.0 铁律 1）。

依赖单向：widget 只 import textual + stdlib + 本包的常量，**不 import orca.*** 的
业务模块（事件以普通 dict/dataclass 形式注入，widget 不耦合 Event pydantic 模型）。
这样 widget 可独立单测（headless），也不被 schema 变更影响。

phase-12：``DagTree`` / ``ActiveNode`` 替换为 ``DagGraph``（拓扑图）/
``NodeDetail``（tab 化详情）。``ChartPanel`` / ``ChartCanvas`` 是图表渲染。
"""

from __future__ import annotations

from orca.iface.cli.widgets.dag_graph import DagGraph
from orca.iface.cli.widgets.node_detail import NodeDetail
from orca.iface.cli.widgets.log_stream import LogStream
from orca.iface.cli.widgets.header import Header
from orca.iface.cli.widgets.chart_panel import ChartPanel
from orca.iface.cli.widgets.chart_canvas import ChartCanvas
from orca.iface.cli.widgets._icons import NODE_STATUS_ICONS

__all__ = [
    "DagGraph",
    "NodeDetail",
    "LogStream",
    "Header",
    "ChartPanel",
    "ChartCanvas",
    "NODE_STATUS_ICONS",
]
