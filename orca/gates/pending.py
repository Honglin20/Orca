"""pending.py —— 从 tape 派生 pending gates（SPEC §3.2 / phase-10 §0.1 第二条 tape-only）。

回答「进程重启后 / 任意进程内，怎么查当前未 resolved 的 gate？」：纯函数
``pending_gates_from_tape(tape)``，扫一遍 ``tape.replay()``，requested 集合减去
resolved 集合，剩余的 requested 事件重建 HumanGate。

为什么不让 ``HumanGateHandler`` 自己暴露 ``list_pending()``？因为 SPEC phase-10 §3.4
明确禁止——``handler._pending`` / ``_gates_meta`` 是 **runtime await 状态**，重启即丢，
且只反映「当前进程持有过 future 的 gate」。tape 才是唯一真相（多壳 / 跨进程读同一份，
永远不漂移）。本函数是 tape-only query path 的 P0 缺口实现（phase-10 SPEC §3 / §5.1）。

设计规则：
  - **纯函数**：无 runtime 状态依赖，重启进程后仍能查（tape 在磁盘）。
  - **单次扫描**：``tape.replay()`` 一遍，requested / resolved 各自收集，O(n)。
  - **重建 HumanGate**：所有字段从 ``event.data`` 取（gate_id / prompt / options /
    context / source / run_id / node / session_id），不读 handler 内存。

依赖单向：本模块运行时只 import ``orca.events.tape``（Tape）+ ``orca.gates.types``
（HumanGate）。**不依赖** ``orca.iface`` / ``orca.run`` / ``orca.exec`` / ``orca.schema``
（依赖铁律：iface/run/exec 可调 gates，gates 不知它们存在；gates 包声明不依赖 schema，
HumanGate 是纯 dataclass——见 ``orca/gates/__init__.py`` 模块注释）。Event 类型在签名里
用 ``Any``（tape.replay 已校验过事件结构，不在 gates 层重新引入 schema 依赖）。
"""

from __future__ import annotations

from typing import Any

from orca.events.tape import Tape
from orca.gates.types import HumanGate


def pending_gates_from_tape(tape: Tape) -> list[HumanGate]:
    """从 tape 派生当前未 resolved 的 gate 列表（SPEC §3.2）。

    扫所有事件一遍：
      - 收集 ``human_decision_requested``（gate_id → event）
      - 减去已 ``human_decision_resolved`` 的（gate_id set）
      - 剩下的 requested 事件重建 HumanGate

    返回顺序：按 requested 出现的 tape 序（稳定，便于调用方断言）。

    幂等：同一 tape 调用多次结果相同（纯函数，SPEC §11 决策 4/8）。
    """
    # event 类型用 Any（不引入 orca.schema.Event 依赖——gates 包声明不依赖 schema，
    # 见 orca/gates/__init__.py 模块注释）。tape.replay() 已校验过事件结构。
    requested: dict[str, Any] = {}
    resolved: set[str] = set()
    for event in tape.replay():
        if event.type == "human_decision_requested":
            gate_id = event.data.get("gate_id")
            if gate_id is not None:
                requested[gate_id] = event
        elif event.type == "human_decision_resolved":
            gate_id = event.data.get("gate_id")
            if gate_id is not None:
                resolved.add(gate_id)

    pending: list[HumanGate] = []
    for gate_id, event in requested.items():
        if gate_id in resolved:
            continue
        data = event.data
        pending.append(
            HumanGate(
                id=data["gate_id"],
                prompt=data["prompt"],
                options=data.get("options"),
                context=data.get("context", {}),
                source=data["source"],
                run_id=data["run_id"],
                node=event.node,
                session_id=event.session_id,
            )
        )
    return pending
