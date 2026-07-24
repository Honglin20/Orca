"""run.py —— POST /api/run 启动新 run（SPEC §3.3 + §13.2 B-1）。

回答「前端怎么启动一个 run？」：``POST /api/run`` body ``{yaml_path, inputs?, task?, max_iter?,
project_path}`` → ``manager.start_run`` → ``{run_id, status: "queued"}``。

SPEC §13.2 B-1：``project_path`` 在 body **必填**（缺 → 400 fail loud，与 SPEC 契约对齐，
**不**用 FastAPI 默认 422）；``manager.start_run`` 接收为 keyword-only（manager 内
``detect_project_root`` + ``register_project`` 自填的 fallback 仅服务 cli 直接调用的 in-process
路径，**不**经此端点）。

依赖单向：本模块依赖 ``orca.iface.web.run_manager`` + ``orca.compile``（ConfigurationError
透传成 400）+ fastapi，不含编排逻辑（start_run 才是托管入口）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

from orca.compile import ConfigurationError

if TYPE_CHECKING:
    from orca.iface.web.run_manager import RunManager


class RunRequest(BaseModel):
    """``POST /api/run`` body（SPEC §3.3 + §13.2 B-1）。

    ``yaml_path`` / ``project_path`` 必填；其余可选。
    """

    yaml_path: str
    project_path: str
    inputs: dict[str, Any] | None = None
    task: str | None = None
    max_iter: int | None = Field(default=None, ge=1)


def build_router(manager: RunManager) -> APIRouter:
    """构造 ``/api/run`` 路由（manager 注入）。"""
    router = APIRouter(prefix="/api", tags=["run"])

    @router.post("/run")
    async def start_run(payload: dict[str, Any]) -> dict[str, Any]:
        """启动新 run。``ConfigurationError`` → 400（fail loud，SPEC §3.1）。

        ``start_run`` 不阻塞：返回时 run 已注册（status=queued），实际执行在后台。

        SPEC §13.2 B-1 / §5.4：``project_path`` 必填（缺 → 400 fail loud）。**不用** pydantic
        body 自动校验（FastAPI 默认 422，与 SPEC 「400 fail loud」契约不符），改为手 parse + 显式 400。
        """
        # 手 parse + ValidationError → 400（与 SPEC 契约一致，而非 FastAPI 默认 422）
        try:
            body = RunRequest(**payload)
        except ValidationError as e:
            raise HTTPException(status_code=400, detail=e.errors())
        try:
            run_id = await manager.start_run(
                body.yaml_path,
                body.inputs,
                body.task,
                body.max_iter,
                project_path=body.project_path,
            )
        except ConfigurationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except FileNotFoundError as e:
            raise HTTPException(status_code=400, detail=f"yaml not found: {e}")
        except ValueError as e:
            # project_path 校验失败（register_project 拒绝 / resolve 失败）→ 400
            raise HTTPException(status_code=400, detail=str(e))
        return {"run_id": run_id, "status": "queued"}

    return router
