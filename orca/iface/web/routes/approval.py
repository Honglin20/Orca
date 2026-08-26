"""approval.py —— in-session 权限审批 HTTP 端点（SPEC §4.1 / §4.2 / §3.2）。

回答「hook POST /approval 到哪？前端 POST /approval/respond 怎么 resolve？」：本模块
挂两个端点到 FastAPI app：

  - ``POST /approval``：hook 桥入口。body ``{session_id?, tool, tool_input, hook_event}`` →
    ``broker.request(payload, http_request)``（阻塞，await fut）。返回
    ``{behavior, approval_id, resolved_by}``（hook emit ``decision.behavior``）。
  - ``POST /approval/respond``：前端 resolve。body ``{approval_id, answer, source?}`` →
    ``broker.resolve``。返回 ``{ok, approval_id, resolved_by}``（first-wins；late → ok=False）。
  - ``POST /approval/yolo``：前端 toggle yolo。body ``{yolo: bool}`` → ``broker.set_yolo``。
  - ``GET /approval/snapshot``：调试 / MCP 用（前端 WS 走 ``request_approval_snapshot`` type，
    不走 HTTP；本端点仅作程序化客户端辅助）。

设计（SPEC §3.2）：
  - ``POST /approval`` 是 ``async def``，``await broker.request`` 内部 await fut（不阻塞
    uvicorn worker）。
  - ``http_request`` 透传给 broker 用于 disconnect 探测（P1）。
  - tool_input redact 在 broker 内部完成（不在此处重复）。

依赖单向：本模块依赖 ``orca.iface.web.approval_broker`` + fastapi，不依赖 run/exec/gates。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request

if TYPE_CHECKING:
    from orca.iface.web.approval_broker import ApprovalBroker

logger = logging.getLogger(__name__)


def build_router(broker: ApprovalBroker) -> APIRouter:
    """构造 approval 相关 HTTP 路由。"""
    router = APIRouter(prefix="", tags=["approval"])

    @router.post("/approval")
    async def approval_endpoint(
        payload: dict[str, Any], request: Request,
    ) -> dict[str, Any]:
        """hook → broker.request。阻塞至 resolve/timeout/disconnect。"""
        try:
            result = await broker.request(payload, http_request=request)
        except Exception:
            logger.exception("approval_endpoint broker.request 异常")
            # SPEC §7：broker 自身异常 → 500 → hook 视作 HTTP 错 → deny+warn。
            raise HTTPException(status_code=500, detail="approval request failed")
        return result

    @router.post("/approval/respond")
    async def approval_respond_endpoint(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """前端 → broker.resolve。first-wins；late → ok=False + 审计 approval_resolved_late。"""
        approval_id = payload.get("approval_id")
        answer = payload.get("answer")
        source = payload.get("source", "web")
        if not approval_id or answer is None:
            raise HTTPException(
                status_code=400, detail="missing approval_id or answer",
            )
        if answer not in ("allow", "deny"):
            raise HTTPException(
                status_code=400, detail=f"invalid answer: {answer!r}",
            )
        return broker.resolve(str(approval_id), str(answer), str(source))

    @router.post("/approval/yolo")
    async def approval_yolo_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
        """前端 toggle yolo。body ``{yolo: bool}``。"""
        if "yolo" not in payload:
            raise HTTPException(status_code=400, detail="missing yolo field")
        value = bool(payload["yolo"])
        new_value = broker.set_yolo(value)
        return {"yolo": new_value}

    @router.get("/approval/snapshot")
    async def approval_snapshot_endpoint() -> dict[str, Any]:
        """程序化客户端用（MCP / 调试）。前端经 WS ``request_approval_snapshot`` type 拿。"""
        return broker.snapshot()

    return router
