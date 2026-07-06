"""orca.iface.cli.widgets —— TUI 组件（Textual widgets）。

壳的渲染层。所有 widget **无业务真相**：状态来自注入的 Event（由 OrcaApp 从
EventBus 订阅后分发），widget 只负责把事件投影成可见字符。重启从 tape replay
重放同样的事件流，widget 渲染必然一致（SPEC §6.0 铁律 1）。

依赖单向：widget 只 import textual + stdlib + 本包的常量，**不 import orca.*** 的
业务模块（事件以普通 dict/dataclass 形式注入，widget 不耦合 Event pydantic 模型）。
这样 widget 可独立单测（headless），也不被 schema 变更影响。

**v2 三块布局**（spec §2.1）：
- 左 30% ``AgentsList``（拓扑序纵向，j/k 切换 + auto-follow）—— Step 2 填充
- 右上 70% ``AgentHistory``（单 agent 视图，Conductor Activity 风格）—— Step 3 填充
- 右下 30% ``LogStream``（高层节点事件，Conductor Log View 风格）—— Step 4 改造
- ``NodeDetail`` 保留实例（display:none；chart 路径唯一入口，spec §6.3）
- ``ChartPanel`` / ``ChartCanvas`` 是图表渲染（保留）

**v2 共享事件派生函数**（Step 1b 迁入）：
- ``_event_summary._truncate`` / ``_format_elapsed_sec`` / ``_arg_title``：基础派生
- ``_event_summary._build_summary_line`` / ``_build_meta_line``：双行 entry 行 1/行 2
- ``_event_summary._build_detail_renderable``：折叠详情（调 phase-15 render_tool / render_message）

下划线前缀表私有（不在 ``__all__``）；AgentHistory（Step 3）+ LogStream（Step 4）经
``from orca.iface.cli.widgets._event_summary import _build_summary_line, ...`` 直接 import。
"""

from __future__ import annotations

from orca.iface.cli.widgets.agents_list import AgentsList
from orca.iface.cli.widgets.agent_history import AgentHistory
from orca.iface.cli.widgets.node_detail import NodeDetail
from orca.iface.cli.widgets.log_stream import LogStream
from orca.iface.cli.widgets.header import Header
from orca.iface.cli.widgets.chart_panel import ChartPanel
from orca.iface.cli.widgets.chart_canvas import ChartCanvas
from orca.iface.cli.widgets._icons import NODE_STATUS_ICONS
# spec v2 §2.3/§2.4：共享事件派生纯函数（下划线前缀表私有，不在 __all__）。
# AgentHistory（Step 3）/ LogStream（Step 4）直接 from ... import 即可。
from orca.iface.cli.widgets._event_summary import (
    _arg_title,
    _build_detail_renderable,
    _build_meta_line,
    _build_summary_line,
    _format_elapsed_sec,
    _truncate,
)

__all__ = [
    "AgentsList",
    "AgentHistory",
    "NodeDetail",
    "LogStream",
    "Header",
    "ChartPanel",
    "ChartCanvas",
    "NODE_STATUS_ICONS",
]
