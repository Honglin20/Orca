"""runs.py —— 懒加载 REST 路由（SPEC §3 / §0 D10 assets）。

回答「前端怎么拿 run 列表 / 全量事件 / 单 run 状态 / agent 产出的图片资源？」：
  - ``GET /api/runs`` → ``list[RunMeta]``，**绝不返回事件**（懒加载红线，§0.1 铁律 2）。
  - ``GET /api/runs/<id>/events`` → ``list[Event]``（懒加载，唯一来源 ``tape.replay``）。
  - ``GET /api/runs/<id>`` → ``{meta, state}``（元数据 + RunState 快照，**不含全量事件**）。
  - ``GET /api/runs/<id>/assets/<path>`` → 图片资源字节流（SPEC §0 D10；markdown 内相对
    / file:// 路径前端重写到此处）。

依赖单向：本模块依赖 ``orca.iface.web.run_manager``（同层）+ fastapi，不含编排逻辑。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

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

    @router.get("/{run_id}/assets/{asset_path:path}")
    async def get_run_asset(run_id: str, asset_path: str) -> FileResponse:
        """某 run 的图片/二进制资源（SPEC §0 D10）。

        前端 markdown renderer 把 ``![](rel.png)`` / ``file://...`` / 裸文件名 rewrite 到
        ``/api/runs/<id>/assets/<encoded>``，由本端点解码 path 后从 ``<runs_dir>/<run_id>/
        assets/<path>`` 取文件。

        - 未知 run_id → 404
        - 路径越界（``..`` / 绝对路径）→ 404（fail loud，不暴露 fs）
        - 文件不存在 → 404

        解析 + 越界守卫委托 ``manager.resolve_asset_path``（SRP：路径解析在 manager，
        IO 字节流在 routes）。
        """
        candidate = manager.resolve_asset_path(run_id, asset_path)
        if candidate is None:
            # 不区分 unknown run / path escape / file missing——统一 404（不暴露 fs 细节）。
            raise HTTPException(
                status_code=404,
                detail=f"asset not found: {asset_path}",
            )
        return FileResponse(str(candidate))

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
