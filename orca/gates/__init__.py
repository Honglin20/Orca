"""orca.gates —— HMIL 层（Human-in-the-Loop）。

回答「workflow 需要人决策时怎么暂停 / 把决策广播给三壳 / 等任一壳答 / 恢复？」：
  - types.py            → HumanGate 原语（统一两个决策来源）
  - handler.py          → HumanGateHandler（暂停/竞速/广播/恢复）+ _broadcaster
  - context_registry.py → claude session_id → (run_id, node) 映射（hook 桥定位）
  - hook_script.py      → PreToolUse hook HTTP 桥（claude spawn 的独立短命进程）
  - http_endpoint.py    → register_gate_routes：POST /gate + POST /gate/respond
  - ask_user.py         → agent 主动问（HumanGate 第二个来源）

核心铁律（SPEC §7.0 §10）：
  - **gate 事件写 Tape**：``human_decision_requested`` / ``human_decision_resolved``
    都 emit 到 EventBus 写 Tape（唯一真相，三壳读同一份，无第二份 gate 状态存储）。
  - **依赖单向**：gates → events，**不依赖 run/exec/iface**（也不依赖 schema——
    HumanGate 是纯 dataclass）。orchestrator 调 gates，gates 不知道 orchestrator 存在。
  - **安全优先**：hook 桥超时/不可达/响应非法 → exit 2（拒绝，绝不放行，SPEC §3.3）。
  - **广播语义**：任一壳 resolve → ``_broadcaster`` emit resolved → 全壳收到（视觉同步）。
  - **gate 无限等**：request 的 await fut 无 timeout（超时只在 hook 桥传输层）。
"""

from orca.gates.ask_user import ask_user
from orca.gates.context_registry import SessionContextRegistry
from orca.gates.handler import HumanGateHandler
from orca.gates.http_endpoint import register_gate_routes
from orca.gates.types import HumanGate

__all__ = [
    "HumanGate",
    "HumanGateHandler",
    "SessionContextRegistry",
    "ask_user",
    "register_gate_routes",
]
