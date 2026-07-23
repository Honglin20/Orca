"""_auth.py —— 多用户鉴权 no-op stub（SPEC §13.1 M-1 / §5.8 / D12）。

**当前 no-op pass-through**：所有端点接受可选 ``Authorization: Bearer <token>`` 头，
当前**忽略**（不校验）。未来启用 token 校验**只改本文件**，业务路由零改（OCP）。

实现方式：FastAPI middleware 全局兜底（``AuthMiddleware``），路由层零 Depends（M-1 决策）。
单测守门（AC19）：``app.user_middleware`` 含 ``AuthMiddleware``。

**为何 middleware 而非 Depends**：DEPENDS 会让每个路由签名多一个 ``auth=Depends(...)`` 参数，
违反 M-1「路由层零 Depends」。middleware 拦在路由前，路由无感。
"""

from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import FastAPI, Request, Response


async def auth_noop_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """no-op auth middleware：读 ``Authorization`` 头但**不校验**（D12 接口预留）。

    未来替换为真实 token 校验：
      1. 启动时读 ``$ORCA_HOME/.orca-token``（仅本用户可读）；
      2. 比对 ``Authorization: Bearer <token>``；
      3. 不符 → ``401 Unauthorized``。

    当前直接 pass——任何请求（含无 Authorization 头）都放行。
    """
    # 故意读取并丢弃，证明契约存在（不 break header 缺失的 client）。
    _ = request.headers.get("Authorization")
    return await call_next(request)


def install_auth_middleware(app: FastAPI) -> None:
    """把 ``auth_noop_middleware`` 装到 FastAPI（SPEC §13.1 M-1 / AC19 守门）。"""
    app.middleware("http")(auth_noop_middleware)
