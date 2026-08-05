"""server.py —— FastAPI app factory + lifespan + run_server（SPEC §1.2 §3 §4）。

回答「后端 app 怎么组装？单进程同引擎怎么跑？」：``create_app(manager)`` 构建 FastAPI
（挂懒加载 REST + gate + WS），``run_server(manager, host, port)`` 用 uvicorn 同事件循环跑
（orchestrator 后台 task 与 uvicorn 共享 loop，SPEC §1.2 / §9 决策 1）。

设计规则（SPEC §0.1 铁律 5 / §1.2 / §9 决策）：
  - **lifespan**：startup/shutdown 调 ``manager.shutdown``（清理在跑 run + gate_handler），
    保证无 leaked task / 未关 tape。
  - **路由注册**：runs / run / gate 三个 router + WS 端点（单通道）。
  - **run_server 同事件循环**：``uvicorn.Server.serve()`` 与 manager 后台 task 共享 loop
    （零 IPC，SPEC §1.2）。
  - **依赖单向**：本模块只 import orca.{run,gates,events,schema,compile} + web stack。

依赖单向：本模块依赖 ``orca.iface.web.run_manager`` / ``ws_handler`` / ``routes``（同层）
+ fastapi/uvicorn。不含编排/gate 决策逻辑（纯 host/forward）。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from orca.iface.web.routes import (
    build_approval_router,
    build_attach_router,
    build_gate_router,
    build_projects_router,
    build_run_router,
    build_runs_router,
    build_workflows_router,
)
from orca.iface.web._auth import install_auth_middleware
from orca.iface.web.approval_broker import ApprovalBroker
from orca.iface.web.ws_handler import WebServer

if TYPE_CHECKING:
    from orca.iface.web.run_manager import RunManager

logger = logging.getLogger(__name__)

# phase 9b 前端构建产物目录（phase 9a 仅占位 .gitkeep）。
_STATIC_DIR = Path(__file__).parent / "static"


def create_app(manager: RunManager) -> FastAPI:
    """构建 FastAPI app（SPEC §1.2 §3）。

    - lifespan：shutdown 时 ``approval_broker.shutdown`` + ``manager.shutdown``（清理资源，无 leak）。
    - 路由：``/api/runs``（懒加载）+ ``/api/run`` + ``/gate`` + ``/approval`` + ``/ws``。
    - 静态前端：``/`` 挂 StaticFiles（phase 9b 构建产物；9a 占位）。

    manager 注入（不全局），便于测试隔离。

    **SPEC §3.2 B-12 偏差说明**：SPEC 字面是「lifespan startup 构造 broker」。本实现把 broker
    构造提前到 ``create_app`` 顶层，shutdown 仍在 lifespan finally——功能等价（broker 生命周期仍绑
    app 生命周期；startup 前无请求路径会触达 broker）。这样路由与 WS 注入只需一次（避免 lazy
    ``app.state`` 取值 + 中间代理对象的复杂度，KISS）。reviewer 接受为「实际行为无差」。
    """

    # SPEC §3.2 B-12：进程级 singleton broker，与 app 同生命周期；shutdown 清理 pending。
    approval_broker = ApprovalBroker(manager.registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # startup：manager 无常驻 task（run task 在 start_run 时起），无需额外启动。
        try:
            yield
        finally:
            # shutdown：approval broker 先清 pending（让 hook 早日 TCP 失败走 ask），
            # 再 manager.shutdown 等在跑 run 到终态 + stop 各自 gate_handler（无 leaked task）。
            try:
                await approval_broker.shutdown()
            except Exception:  # noqa: BLE001 — broker shutdown 异常不阻断 manager 收尾
                logger.warning("approval_broker.shutdown 异常", exc_info=True)
            await manager.shutdown()

    app = FastAPI(title="orca-web", lifespan=lifespan)
    app.state.manager = manager
    app.state.approval_broker = approval_broker
    # ``tars close`` 的 ``/api/shutdown`` 端点经此句柄触发 ``should_exit``；run_server /
    # _serve_and_run_inprocess 创建 uvicorn.Server 后覆写为真实句柄。None = 未 wire（端点
    # 取不到时返 500 fail loud，见 attach.py::shutdown）。
    app.state.uvicorn_server = None
    # SPEC §13.1 M-1 / AC19：全局 no-op auth middleware（多用户接口预留，当前不校验）。
    install_auth_middleware(app)

    # 懒加载 REST + gate（多 run 分发）+ attach（X — read-only tail-follow）+ approval + health。
    app.include_router(build_runs_router(manager))
    app.include_router(build_run_router(manager))
    app.include_router(build_gate_router(manager))
    app.include_router(build_approval_router(approval_broker))
    app.include_router(build_attach_router(manager))
    # SPEC §13.3 P3：stale projects 只读端点（无 manager 依赖）。
    app.include_router(build_projects_router())
    # workflow / agent 资源只读浏览（无 manager 依赖；plan idempotent-churning-lampson）。
    app.include_router(build_workflows_router())

    # WS 单通道（按需订阅）。approval_broker 注入让 ws_handler 加第二条 approval pump。
    web_server = WebServer(manager, approval_broker=approval_broker)
    app.state.web_server = web_server
    app.websocket("/ws")(web_server.ws_endpoint)

    # 静态前端（phase 9b 构建产物）+ **SPA fallback**。
    # 前端用 BrowserRouter（客户端路由），``/runs/<id>`` 等深链在后端没有对应文件 —— 必须回退
    # 到 index.html 让客户端路由接管，否则深链/刷新返回 ``{"detail":"Not Found"}`` 404（整个
    # 详情页废）。挂 ``/assets``（vite hashed JS/CSS）+ catch-all GET → index.html。
    # catch-all 注册在所有 API router 之后，故 ``/api/*`` ``/gate`` ``/ws`` 优先匹配不被吞。
    if _STATIC_DIR.exists():
        _assets = _STATIC_DIR / "assets"
        if _assets.exists():
            app.mount("/assets", StaticFiles(directory=str(_assets)), name="assets")

        from fastapi.responses import FileResponse, JSONResponse

        _index = _STATIC_DIR / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        async def _spa_fallback(full_path: str):  # noqa: ARG001
            """非 API/WS/gate 的 GET → 返回 index.html（客户端路由接管）。"""
            if _index.exists():
                return FileResponse(str(_index))
            # 前端未构建：可操作提示而非裸 404。
            return JSONResponse(
                {"detail": "frontend not built — run: cd orca/iface/web/frontend && npm run build"},
                status_code=404,
            )

    return app


async def run_server(
    manager: RunManager,
    host: str = "127.0.0.1",
    port: int = 7428,
) -> None:
    """用 uvicorn 单进程同事件循环跑 server（SPEC §1.2 / §9 决策 1）。

    manager 的后台 run task 与 uvicorn 共享同一 asyncio loop（零 IPC）。
    ``await`` 此函数直到 server 停止（Ctrl-C / lifespan shutdown）。
    """
    import uvicorn

    app = create_app(manager)
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    # 暴露 uvicorn 句柄给 ``/api/shutdown`` 端点（B1：与 _serve_and_run_inprocess 两处都 wire）。
    app.state.uvicorn_server = server
    await server.serve()
