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
    build_attach_router,
    build_gate_router,
    build_run_router,
    build_runs_router,
)
from orca.iface.web.ws_handler import WebServer

if TYPE_CHECKING:
    from orca.iface.web.run_manager import RunManager

logger = logging.getLogger(__name__)

# phase 9b 前端构建产物目录（phase 9a 仅占位 .gitkeep）。
_STATIC_DIR = Path(__file__).parent / "static"


def create_app(manager: RunManager) -> FastAPI:
    """构建 FastAPI app（SPEC §1.2 §3）。

    - lifespan：shutdown 时 ``manager.shutdown``（清理资源，无 leak）。
    - 路由：``/api/runs``（懒加载）+ ``/api/run`` + ``/gate`` + ``/gate/respond`` + ``/ws``。
    - 静态前端：``/`` 挂 StaticFiles（phase 9b 构建产物；9a 占位）。

    manager 注入（不全局），便于测试隔离。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # startup：manager 无常驻 task（run task 在 start_run 时起），无需额外启动。
        try:
            yield
        finally:
            # shutdown：等在跑 run 到终态 + stop 各自 gate_handler（无 leaked task）。
            await manager.shutdown()

    app = FastAPI(title="orca-web", lifespan=lifespan)
    app.state.manager = manager

    # 懒加载 REST + gate（多 run 分发）+ attach（X — read-only tail-follow）+ health。
    app.include_router(build_runs_router(manager))
    app.include_router(build_run_router(manager))
    app.include_router(build_gate_router(manager))
    app.include_router(build_attach_router(manager))

    # WS 单通道（按需订阅）。
    web_server = WebServer(manager)
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
    await server.serve()
