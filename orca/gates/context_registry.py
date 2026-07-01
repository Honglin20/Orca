"""context_registry.py —— claude session_id → (run_id, node) 映射（hook 桥定位用）。

回答「hook 桥怎么知道当前 run/node？」：hook 是 claude spawn 的**独立短命进程**，
不知道 Orca 的 run 上下文，但 hook stdin 含 **claude 的 session_id**（注意：与 Orca
executor 生成的 session_id **不同**，phase 4 SPEC §3.2 决策 5）。

机制（SPEC §6）：
  1. orchestrator spawn claude 后，从 claude 流的 ``system/init`` 事件提取 claude 的
     session_id，调 ``register(claude_sid, run_id, node)``。
  2. hook 桥的 ``/gate`` 端点收到 hook POST（含 claude session_id），调
     ``lookup(claude_sid)`` 取 ``(run_id, node)``，注入构造的 ``HumanGate``。
  3. node 完成时 ``unregister(claude_sid)`` 清理（防内存泄漏）。

线程安全：``register`` / ``lookup`` / ``unregister`` 可能从多个 asyncio task 甚至
hook 子进程的 HTTP handler 线程并发调用，用 ``threading.Lock`` 保护（不是 asyncio
Lock——HTTP 框架的同步分发可能在不同线程；threading.Lock 跨线程安全且开销极小）。

依赖单向：本模块零依赖（仅 stdlib），不依赖任何 orca 子包。
"""

from __future__ import annotations

import threading
from typing import NamedTuple


class SessionLoc(NamedTuple):
    """``lookup`` 返回的 (run_id, node) 二元组（NamedTuple 提供字段名访问）。

    phase 11 §5.5（review B2）重命名：原 ``RunContext`` 与 ``orca.exec.context.RunContext``
    （frozen dataclass）同名混淆，本类改为 ``SessionLoc``（gates 语义：定位一次 claude
    session 所属的 run/node）。字段不变（``run_id`` / ``node``）——跨阶段契约变更，
    release note 记录。
    """

    run_id: str
    node: str


class SessionContextRegistry:
    """claude session_id → SessionLoc 映射（SPEC §6）。

    orchestrator spawn claude 时 ``register``；hook 桥的 ``/gate`` 端点 ``lookup``；
    node 完成时 ``unregister``。同 session_id 重复 register 走 last-writer-wins
    （claude 重连场景，新上下文覆盖旧的）。
    """

    def __init__(self) -> None:
        self._map: dict[str, SessionLoc] = {}
        self._lock = threading.Lock()

    def register(self, session_id: str, run_id: str, node: str) -> None:
        """注册 / 覆盖 session_id → SessionLoc(run_id, node)。last-writer-wins。"""
        with self._lock:
            self._map[session_id] = SessionLoc(run_id=run_id, node=node)

    def lookup(self, session_id: str) -> SessionLoc | None:
        """查询。未注册返回 None（hook 桥据此决定是否构造 workflow 级 gate）。"""
        with self._lock:
            return self._map.get(session_id)

    def unregister(self, session_id: str) -> None:
        """清理。未注册的 session_id 静默忽略（幂等，方便 node 完成路径统一调用）。"""
        with self._lock:
            self._map.pop(session_id, None)

    def unregister_run(self, run_id: str) -> int:
        """清理某 run 的**全部** session 路由（workflow 结束时调，防内存泄漏）。

        AgentToolsMcpServer 每个被编排的 claude session 都 register 一条；run 结束时
        orchestrator 调此方法一次性清空该 run 的所有条目（session_id 由 executor 内部 uuid
        生成，orchestrator 不持有，故按 run_id 批清）。返回清理的条目数（可观测）。
        """
        with self._lock:
            doomed = [sid for sid, loc in self._map.items() if loc.run_id == run_id]
            for sid in doomed:
                self._map.pop(sid, None)
            return len(doomed)
