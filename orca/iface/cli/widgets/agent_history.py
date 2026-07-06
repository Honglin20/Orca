"""agent_history.py —— v2 右上 70% Agent History widget（spec §2.3，Step 3 填充）。

Step 1a 占位空 shell：仅声明 class 让 app.py compose 不破。Step 3 落地 Conductor
Activity 风格双行 entry + last message 默认展开 + tool_call_id cache（迁自 v1.1.1
ActivityStream GAP-B/C）。

依赖单向（与 widgets/__init__.py 一致）：只 import textual + stdlib + 本包常量，
不 import orca.* 的业务模块（事件以 SimpleNamespace / Event 注入，widget 不耦合 pydantic）。
"""

from __future__ import annotations

from textual.widgets import Static


class AgentHistory(Static):
    """v2 右上 agent 历史流（Step 3 填充实现）。"""
