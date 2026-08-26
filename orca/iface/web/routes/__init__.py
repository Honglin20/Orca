"""orca.iface.web.routes —— 懒加载 REST + gate + attach + approval + health 路由（SPEC §3 §5）。

各 route 模块导出 ``build_router(manager) -> APIRouter``（manager 注入，避免全局状态）。
"""

from orca.iface.web.routes.approval import build_router as build_approval_router
from orca.iface.web.routes.attach import build_router as build_attach_router
from orca.iface.web.routes.gate import build_router as build_gate_router
from orca.iface.web.routes.projects import build_router as build_projects_router
from orca.iface.web.routes.run import build_router as build_run_router
from orca.iface.web.routes.runs import build_router as build_runs_router
from orca.iface.web.routes.workflows import build_router as build_workflows_router

__all__ = [
    "build_approval_router",
    "build_attach_router",
    "build_gate_router",
    "build_projects_router",
    "build_run_router",
    "build_runs_router",
    "build_workflows_router",
]
