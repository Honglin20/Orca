"""runs.py —— 懒加载 REST 路由（SPEC §3）。

回答「前端怎么拿 run 列表 / 全量事件 / 单 run 状态？」：
  - ``GET /api/runs`` → ``list[RunMeta]``，**绝不返回事件**（懒加载红线，§0.1 铁律 2）。
  - ``GET /api/runs/<id>/events`` → ``list[Event]``（懒加载，唯一来源 ``tape.replay``）。
  - ``GET /api/runs/<id>`` → ``{meta, state}``（元数据 + RunState 快照，**不含全量事件**）。

依赖单向：本模块依赖 ``orca.iface.web.run_manager``（同层）+ fastapi，不含编排逻辑。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from orca.events.replay import replay_state

if TYPE_CHECKING:
    from orca.iface.web.run_manager import RunManager


def build_router(manager: RunManager) -> APIRouter:
    """构造 ``/api/runs`` 路由（manager 注入，避免全局状态）。

    返回 APIRouter，由 ``server.create_app`` include。
    """
    router = APIRouter(prefix="/api/runs", tags=["runs"])

    @router.get("")
    async def list_runs() -> list[dict[str, Any]]:
        """run 列表（元数据，**无事件**）。SPEC §3.1 / §0.1 铁律 2。

        返回 ``list[RunMeta.dict]``——dataclass 转 dict（fastapi 序列化）。
        """
        return [_meta_to_dict(m) for m in manager.list_runs()]

    @router.get("/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        """单 run 元数据 + RunState 快照（**不含全量事件**，SPEC §3.1）。

        未知 run_id → 404。用 ``get_run_meta``（单 run replay，避免 N+1）。
        """
        handle = manager.get_handle(run_id)
        if handle is None:
            raise HTTPException(status_code=404, detail=f"unknown run_id: {run_id}")
        meta = manager.get_run_meta(run_id)
        if meta is None:  # 理论不可达（get_handle 有就 meta 有），fail loud
            raise HTTPException(status_code=404, detail=f"run meta missing: {run_id}")
        state = replay_state(handle.tape)
        return {"meta": _meta_to_dict(meta), "state": state.model_dump()}

    @router.get("/{run_id}/events")
    async def get_run_events(run_id: str) -> list[dict[str, Any]]:
        """某 run 全量事件（懒加载，唯一来源 ``tape.replay``，§0.1 铁律 1）。

        未知 run_id → 404。
        """
        try:
            events = manager.get_run_events(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown run_id: {run_id}")
        return [e.model_dump() for e in events]

    return router


def _meta_to_dict(meta: Any) -> dict[str, Any]:
    """RunMeta dataclass → dict（懒加载契约：**无 events 字段**，SPEC §0.1 铁律 2）。"""
    return {
        "run_id": meta.run_id,
        "workflow_name": meta.workflow_name,
        "status": meta.status,
        "progress": meta.progress,
        "cost": meta.cost,
        "elapsed": meta.elapsed,
        "error": meta.error,
    }
