"""projects.py —— 注册表只读运维路由（SPEC §13.3 P3 Stale projects 折叠区）。

回答「前端列表页怎么看到 path 已失效的注册项？」：
  - ``GET /api/projects/stale`` → ``list[{project_id, path, name, first_seen, last_seen}]``

**只读**：本路由不提供任何写入（注册表写入在 ``register_project`` / ``rebuild_registry``）。
**单向依赖**：依赖 ``orca.runtime``（中立层）+ fastapi；不 import cli。
"""

from __future__ import annotations

from fastapi import APIRouter

from orca.runtime import list_stale_projects


def build_router() -> APIRouter:
    """构造 ``/api/projects`` 路由（无 manager 依赖——只读注册表）。"""
    router = APIRouter(prefix="/api/projects", tags=["projects"])

    @router.get("/stale")
    async def get_stale_projects() -> list[dict]:
        """SPEC §13.3 P3：列注册表中 path 已失效的项目（前端折叠区用）。

        ``stale`` 定义：``entry.path`` 目录不存在 OR 不再含 project marker。
        注册表读失败 → 空 list（fail-soft，不崩列表页）。
        """
        return list_stale_projects()

    return router
