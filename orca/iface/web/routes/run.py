"""run.py —— POST /api/run 启动新 run（SPEC §3.3）。

回答「前端怎么启动一个 run？」：``POST /api/run`` body ``{yaml_path, inputs?, task?, max_iter?}``
→ ``manager.start_run`` → ``{run_id, status: "queued"}``。

依赖单向：本模块依赖 ``orca.iface.web.run_manager`` + ``orca.compile``（ConfigurationError
透传成 400）+ fastapi，不含编排逻辑（start_run 才是托管入口）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from orca.compile import ConfigurationError

if TYPE_CHECKING:
    from orca.iface.web.run_manager import RunManager


class RunRequest(BaseModel):
    """``POST /api/run`` body（SPEC §3.3）。

    ``yaml_path`` 必填；其余可选。``additionalProperties=False`` 透传由 pydantic 默认。
    """

    yaml_path: str
    inputs: dict[str, Any] | None = None
    task: str | None = None
    max_iter: int | None = Field(default=None, ge=1)


def build_router(manager: RunManager) -> APIRouter:
    """构造 ``/api/run`` 路由（manager 注入）。"""
    router = APIRouter(prefix="/api", tags=["run"])

    @router.post("/run")
    async def start_run(body: RunRequest) -> dict[str, Any]:
        """启动新 run。``ConfigurationError`` → 400（fail loud，SPEC §3.1）。

        ``start_run`` 不阻塞：返回时 run 已注册（status=queued），实际执行在后台。
        """
        try:
            run_id = await manager.start_run(
                body.yaml_path, body.inputs, body.task, body.max_iter
            )
        except ConfigurationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except FileNotFoundError as e:
            raise HTTPException(status_code=400, detail=f"yaml not found: {e}")
        return {"run_id": run_id, "status": "queued"}

    return router
