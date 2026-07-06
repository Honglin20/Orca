"""agents_list.py —— v2 左 30% Agents List widget（spec §2.2，Step 2 填充）。

Step 1a 占位空 shell：仅声明 class 让 app.py compose 不破。Step 2 落地拓扑序渲染 +
status icon + j/k 导航 + auto-follow。

依赖单向（与 widgets/__init__.py 一致）：只 import textual + stdlib + 本包常量，
不 import orca.* 的业务模块（事件以 SimpleNamespace / Event 注入，widget 不耦合 pydantic）。
"""

from __future__ import annotations

from textual.widgets import Static


class AgentsList(Static):
    """v2 左侧 agent 列表（Step 2 填充实现）。"""
