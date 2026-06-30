"""orca.schema —— 纯数据结构定义层（整个架构的地基）。

只回答三个第一性问题：
  - 跑什么（静态结构）  → workflow.py（Workflow / Node / Route / 各 kind）
  - 产出了什么（运行时产出）→ event.py（Event / EventType）
  - 现在到哪了（运行时状态）→ state.py（RunState / Status）

铁律：只有 pydantic 模型，零逻辑（无解析、无校验、无持久化），零依赖（除 pydantic）。
其他所有模块依赖 schema，schema 不依赖任何人。
"""

from orca.schema.event import Event, EventType
from orca.schema.state import RunState, Status, UsageSummary
from orca.schema.workflow import (
    AgentNode,
    AnnotatedNode,
    ForeachNode,
    InputDef,
    Node,
    ParallelGroup,
    Route,
    ScriptNode,
    SetNode,
    Workflow,
)

__all__ = [
    "Workflow",
    "InputDef",
    "Node",
    "Route",
    "AgentNode",
    "ScriptNode",
    "SetNode",
    "ForeachNode",
    "AnnotatedNode",
    "ParallelGroup",
    "Event",
    "EventType",
    "RunState",
    "Status",
    "UsageSummary",
]
