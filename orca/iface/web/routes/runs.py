"""runs.py —— 懒加载 REST 路由（SPEC §3 / §0 D10 assets + §13 Phase C）。

回答「前端怎么拿 run 列表 / 全量事件 / 单 run 状态 / agent 产出的图片资源？」：
  - ``GET /api/runs`` → ``list[RunMeta]`` 或 ``?scope=all`` → ``list[RunSummary]``（跨项目 discovery）。
    **绝不返回事件**（懒加载红线，§0.1 铁律 2）。
  - ``GET /api/runs/<id>`` → ``{meta, state}``（元数据 + RunState 快照，**不含全量事件**）。
  - ``GET /api/runs/<id>/events`` → ``list[Event]``（懒加载，唯一来源 ``tape.replay``）。
  - ``GET /api/runs/<id>/meta`` → 扩展 meta（含 huge/overview）。
  - ``GET /api/runs/<id>/assets/<path>`` → 图片资源字节流（SPEC §0 D10）。
  - ``DELETE /api/runs/<id>`` → 删 tape + run 目录（SPEC §13 D10/B-5/M-3）。

懒挂载触发面（SPEC §13.2 I-3）：``{/meta, /events, /assets/<path>}`` 任一遇 unknown run_id
先 ``manager.ensure_attached``（WS subscribe 在 ws_handler 内 ensure_attached）。

依赖单向：本模块依赖 ``orca.iface.web.run_manager``（同层）+ fastapi，不含编排逻辑。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from orca.events.replay import replay_state
from orca.iface.web.run_manager import RunSummary

if TYPE_CHECKING:
    from orca.iface.web.run_manager import RunManager


def build_router(manager: RunManager) -> APIRouter:
    """构造 ``/api/runs`` 路由（manager 注入，避免全局状态）。

    返回 APIRouter，由 ``server.create_app`` include。
    """
    router = APIRouter(prefix="/api/runs", tags=["runs"])

    @router.get("")
    async def list_runs(
        scope: str | None = None,
        project: str | None = None,
        status: str | None = None,
        q: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """run 列表（元数据，**无事件**）。SPEC §3.1 / §0.1 铁律 2 / §13 §5.2 D5。

        - ``?scope=all`` → 跨项目 discovery（``list[RunSummary]``，M-5 白名单）。
        - 否则 → 内存 live run（向后兼容：``list[RunMeta]``）。

        过滤参数（仅 ``scope=all`` 生效）：``project`` / ``status`` / ``q`` / ``limit`` / ``offset``。
        """
        if scope == "all":
            summaries = manager.discover_runs()
            # 过滤
            if project is not None:
                summaries = [s for s in summaries if s.project_id == project]
            if status is not None:
                summaries = [s for s in summaries if s.status == status]
            if q is not None:
                ql = q.strip().lower()
                if ql:
                    summaries = [
                        s for s in summaries
                        if ql in s.run_id.lower()
                        or ql in (s.workflow_name or "").lower()
                    ]
            # offset/limit
            start = offset or 0
            if start:
                summaries = summaries[start:]
            if limit is not None:
                summaries = summaries[:limit]
            # response_model_exclude_unset=True（M-5）：让 legacy run 省略 project_id 等未设字段。
            return [
                s.model_dump(exclude_unset=True, exclude_none=False)
                for s in summaries
            ]
        # 默认（向后兼容）：内存 live run。
        return [_meta_to_dict(m) for m in manager.list_runs()]

    @router.get("/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        """单 run 元数据 + RunState 快照（**不含全量事件**，SPEC §3.1）。

        未知 run_id → 404。用 ``get_run_meta``（单 run replay，避免 N+1）。
        """
        await _ensure_attached(manager, run_id)
        handle = manager.get_handle(run_id)
        if handle is None:
            raise HTTPException(status_code=404, detail=f"unknown run_id: {run_id}")
        meta = manager.get_run_meta(run_id)
        if meta is None:  # 理论不可达（get_handle 有就 meta 有），fail loud
            raise HTTPException(status_code=404, detail=f"run meta missing: {run_id}")
        state = replay_state(handle.tape)
        return {"meta": _meta_to_dict(meta), "state": state.model_dump()}

    @router.get("/{run_id}/events")
    async def get_run_events(
        run_id: str,
        since: int | None = None,
        limit: int | None = None,
        tail: int | None = None,
    ) -> list[dict[str, Any]]:
        """某 run 事件（懒加载，唯一来源 ``tape.replay``，§0.1 铁律 1）。

        **M1**：本端点是 pure tape read——不 emit bus / 不 relay（bus 写入路径只在
        follow task）。前端 huge 模式经此拉 tail + 增量窗口；client-fold。
        """
        await _ensure_attached(manager, run_id)
        try:
            events = manager.get_run_events_window(
                run_id, since=since, limit=limit, tail=tail
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown run_id: {run_id}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return [e.model_dump() for e in events]

    @router.get("/{run_id}/meta")
    async def get_run_meta(run_id: str) -> dict[str, Any]:
        """扩展 meta（SPEC web-attach §3）。

        返回 ``{run_id, status, source, event_count, byte_size, oldest_seq, newest_seq,
        writable, huge, overview?}``。

        未知 run_id → 404。
        """
        await _ensure_attached(manager, run_id)
        meta = manager.get_run_extended_meta(run_id)
        if meta is None:
            raise HTTPException(status_code=404, detail=f"unknown run_id: {run_id}")
        return meta

    @router.get("/{run_id}/assets/{asset_path:path}")
    async def get_run_asset(run_id: str, asset_path: str) -> FileResponse:
        """某 run 的图片/二进制资源（SPEC §0 D10）。

        前端 markdown renderer 把 ``![](rel.png)`` / ``file://...`` / 裸文件名 rewrite 到
        ``/api/runs/<id>/assets/<encoded>``，由本端点解码 path 后从 ``<runs_dir>/<run_id>/
        assets/<path>`` 取文件。

        - 未知 run_id → 404
        - 路径越界（``..`` / 绝对路径）→ 404（fail loud，不暴露 fs）
        - 文件不存在 → 404
        """
        await _ensure_attached(manager, run_id)
        candidate = manager.resolve_asset_path(run_id, asset_path)
        if candidate is None:
            raise HTTPException(
                status_code=404,
                detail=f"asset not found: {asset_path}",
            )
        return FileResponse(str(candidate))

    @router.delete("/{run_id}")
    async def delete_run(run_id: str) -> JSONResponse:
        """删 run（SPEC §13 §5.7 D10 / B-5 / M-3）。

        响应：
          - 200 ``{ok:true, run_id, existed_before:true}``
          - 404 ``{ok:false, never_existed:true, run_id}``
          - 409 ``{ok:false, live:true, run_id, pid}`` （attached live / Windows file-locked）
        """
        result = await manager.delete_run(run_id)
        if result.get("never_existed"):
            return JSONResponse(result, status_code=404)
        if result.get("live"):
            return JSONResponse(result, status_code=409)
        if result.get("ok"):
            return JSONResponse(result, status_code=200)
        # 兜底：未识别错误 → 500
        return JSONResponse(result, status_code=500)

    return router


async def _ensure_attached(manager: "RunManager", run_id: str) -> None:
    """懒挂载 helper（SPEC §13.2 I-3）：unknown run_id 先 ensure_attached。

    - 已在内存 → no-op。
    - 0 命中 → 404。
    - 多命中 → 500（fail loud）。
    - PermissionError → 403。
    """
    if manager.get_handle(run_id) is not None:
        return
    try:
        await manager.ensure_attached(run_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


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
