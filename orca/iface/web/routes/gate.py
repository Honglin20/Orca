"""gate.py —— 多 run gate 端点分发（SPEC §5 / shells-draft §4 / phase-6 §3.5）。

回答「phase 9a 有多个 run 各自的 gate_handler，hook / 壳的 gate 请求怎么路由到正确的 run？」：

**多 run gate 分发决策（2026-06-30 定稿）**：phase-6 的 ``register_gate_routes(app,
handler, registry)`` 是**单 handler**（绑定一个 HumanGateHandler），无法表达「分发到
持有该 session_id 的 run 的 handler」。phase 9a 在 web 层加一个**薄分发器**：

  1. ``POST /gate``（hook 桥）：hook stdin 的 claude ``session_id`` → 共享 ``registry``
     查 ``(run_id, node)`` → ``manager.get_handle(run_id).gate_handler`` → 复用 phase-6
     的 ``build_gate_from_hook_payload`` 构造 ``HumanGate`` → ``handler.request``（阻塞等
     任一壳 resolve）。
  2. ``POST /gate/respond``（壳 resolve）：body 的 ``run_id``（或从 gate_id 反查）→ 该 run
     的 handler → ``handler.resolve``。

**为什么不分发到单 handler 而是新建薄分发器**：phase-6 ``register_gate_routes`` 的
``handler`` 是构造时绑定的单一实例；多 run 场景每个 run 有独立 handler（隔离铁律）。
强行把多 handler 塞进 phase-6 路由会破坏其 SRP。故 web 层新建分发器，**HumanGate 构造 +
session_id 反查复用 phase-6 的共享 helper**（DRY：``resolve_session_context`` +
``build_gate_from_hook_payload``），仅分发逻辑新增。phase-6 路由保持不变供 CLI 单 run 场景。

依赖单向：本模块依赖 ``orca.iface.web.run_manager``（取 handle）+ ``orca.gates``（HumanGate /
SessionContextRegistry / 共享 helper）+ fastapi。不含 gate 决策逻辑（决策在 handler.resolve）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from orca.gates.http_endpoint import (
    build_gate_from_hook_payload,
    resolve_session_context,
)
from orca.iface.web.run_manager import InProcessRunHandle

if TYPE_CHECKING:
    from orca.iface.web.run_manager import RunManager

logger = logging.getLogger(__name__)


def build_router(manager: RunManager) -> APIRouter:
    """构造 ``/gate`` + ``/gate/respond`` 路由（多 run 分发，SPEC §5）。

    与 phase-6 ``register_gate_routes`` 的区别：本分发器从 session_id/run_id 反查到具体
    run 的 ``handle.gate_handler``，再 delegate request/resolve（多 handler 分发）。
    HumanGate 构造 + session_id 反查逻辑复用 phase-6 共享 helper（DRY）。
    """
    router = APIRouter(prefix="", tags=["gate"])

    @router.post("/gate")
    async def gate_endpoint(payload: dict[str, Any]) -> dict[str, str]:
        """hook → POST /gate。session_id 反查 run → 该 run handler.request（阻塞）。

        - session_id 已注册 → 精确路由到该 run 的 handler。
        - session_id 未注册 → 仅当恰好一个活跃 run 时 fallback 到它（避免错路由）；
          多个活跃 run 时 400（要求 hook 带正确 session_id，fail loud）。
        - 无活跃 run → 400。
        - ``handler.request`` 阻塞至任一壳 resolve（gate 无限等）。
        """
        run_id_resolved, node = resolve_session_context(manager.registry, payload)
        # 精确路由
        handler = None
        if run_id_resolved != "unknown":
            handle = manager.get_handle(run_id_resolved)
            if isinstance(handle, InProcessRunHandle):
                handler = handle.gate_handler
        if handler is None:
            # session_id 未注册/未知 run：fallback 仅当恰好一个活跃 in-process run
            # （attached run 是 read-only，无 gate_handler；排除）
            active = [
                h for h in manager._runs.values()
                if isinstance(h, InProcessRunHandle)
                and h.status in ("running", "queued")
            ]
            if len(active) == 1:
                handler = active[0].gate_handler
                run_id_resolved = run_id_resolved if run_id_resolved != "unknown" else active[0].run_id
            else:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "no uniquely identifiable run for gate "
                        f"(session_id registered={run_id_resolved != 'unknown'}, "
                        f"active_runs={len(active)})"
                    ),
                )

        gate = build_gate_from_hook_payload(payload, run_id_resolved, node)

        try:
            answer, resolved_by = await handler.request(gate)
        except Exception:
            logger.exception("gate_endpoint handler.request 异常（gate=%s）", gate.id)
            raise HTTPException(status_code=500, detail="gate request failed")

        decision = "allow" if answer == "allow" else "deny"
        return {"decision": decision, "resolved_by": resolved_by, "gate_id": gate.id}

    @router.post("/gate/respond")
    async def gate_respond_endpoint(payload: dict[str, Any]) -> dict[str, bool | str | None]:
        """壳 → POST /gate/respond。body ``{gate_id, answer, source?, run_id?}`` → resolve。

        多 run 分发：若有 ``run_id`` → 该 run handler；否则遍历所有 run handler 找持有该
        pending gate 的 handler（``handler.has_pending``，公开 API）。返回
        ``{ok, gate_id, run_id}``。
        """
        gate_id = payload.get("gate_id")
        answer = payload.get("answer")
        source = payload.get("source", "web")
        run_id = payload.get("run_id")

        if not gate_id or answer is None:
            raise HTTPException(status_code=400, detail="missing gate_id or answer")

        handler = None
        resolved_run_id: str | None = None
        if run_id is not None:
            handle = manager.get_handle(run_id)
            if isinstance(handle, InProcessRunHandle):
                handler = handle.gate_handler
                resolved_run_id = run_id
        else:
            # 无 run_id：遍历找持有该 pending gate 的 in-process handler（attached 无 gate_handler）。
            for h in manager._runs.values():
                if not isinstance(h, InProcessRunHandle):
                    continue
                if h.gate_handler.has_pending(str(gate_id)):
                    handler = h.gate_handler
                    resolved_run_id = h.run_id
                    break
        if handler is None:
            raise HTTPException(status_code=404, detail="gate not found (no matching run)")

        ok = handler.resolve(str(gate_id), str(answer), str(source))
        return {"ok": ok, "gate_id": str(gate_id), "run_id": resolved_run_id}

    return router
