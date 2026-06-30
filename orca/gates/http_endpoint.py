"""http_endpoint.py —— Orca server 的 /gate + /gate/respond 端点（壳共用）。

回答「hook 桥 POST 到哪？壳怎么 HTTP resolve？」：``register_gate_routes(app,
handler, registry)`` 把两个端点注册到提供的 FastAPI/Starlette ``app``：

  - ``POST /gate``：hook 桥入口。读 hook stdin 的 ``session_id`` 经 registry 查
    ``(run_id, node)`` → 构造 ``HumanGate(source=tool_permission)`` →
    ``handler.request`` → 返回 ``{decision, resolved_by}``（阻塞等任一壳 resolve）。
  - ``POST /gate/respond``：壳的 HTTP resolve 路径（Web 壳用）。body
    ``{gate_id, answer, source}`` → ``handler.resolve`` → 返回 ``{ok}``。

设计（SPEC §3.5 §4.3 §6）：
  - **框架中立**：``register_gate_routes(app, ...)`` 接受任意 FastAPI/Starlette app，
    不自己创建 app（phase 9 web server 构建完整 app 后调用本函数挂载 gate 端点）。
  - **session_id 映射**：hook 是独立进程，不知道 Orca run 上下文；端点用 registry
    从 hook stdin 的 claude session_id 反查 ``(run_id, node)``（SPEC §6）。
  - **未注册 session_id**：fallback 到 workflow 级 gate（``node=None``）+ 记 warning，
    不 500（hook 桥安全语义依赖端点返回，500 会让 hook 走 URLError → exit 2，可行但
    不优雅；此处返回 deny + warning，让 hook 桥正常解析）。
  - **gate 端点阻塞等 resolve**：``await handler.request(gate)`` 阻塞至任一壳 resolve
    （gate 无限等，SPEC §2.2 决策 3）。HTTP 框架的连接超时由框架/server 配置管，不在此处。

依赖单向：本模块依赖 ``orca.gates.{types,handler,context_registry}`` + FastAPI/Starlette
（HTTP 框架）；不依赖 run/exec/iface（gates 铁律）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from orca.gates.context_registry import SessionContextRegistry
from orca.gates.handler import HumanGateHandler
from orca.gates.types import HumanGate

if TYPE_CHECKING:
    # FastAPI 的 APIRouter 路由签名装饰用（运行时由 register_gate_routes 挂到 app）。
    # 用 TYPE_CHECKING guard 保证模块导入时不强制依赖 FastAPI（但 register_gate_routes
    # 实际调用处需要 FastAPI 已安装）。
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def resolve_session_context(
    registry: SessionContextRegistry, payload: dict[str, Any]
) -> tuple[str, str | None]:
    """从 hook payload 的 session_id 反查 ``(run_id, node)``（SPEC §6）。

    未注册 / 缺 session_id → fallback ``(unknown, None)``（workflow 级 gate）+ 记 warning。
    phase-6 单 run 与 phase-9a 多 run 分发共用此函数（DRY）。
    """
    session_id = payload.get("session_id")
    if session_id is None:
        return "unknown", None
    ctx = registry.lookup(session_id)
    if ctx is not None:
        return ctx.run_id, ctx.node
    # 未注册 session_id：hook 来了但 orchestrator 还没 register（race），或 hook 桥配置错。
    # fallback workflow 级 gate + 记 warning（fail loud）。
    logger.warning(
        "hook 桥收到未注册的 session_id=%s，fallback 到 workflow 级 gate", session_id,
    )
    return "unknown", None


def build_gate_from_hook_payload(
    payload: dict[str, Any], run_id: str, node: str | None,
) -> HumanGate:
    """从 hook POST payload 构造 ``HumanGate(source=tool_permission)``。

    phase-6 单 run 与 phase-9a 多 run 分发共用此构造（DRY：HumanGate context 字段集
    单一来源，避免两处分头维护 drift）。
    """
    session_id = payload.get("session_id")
    tool_name = payload.get("tool_name", "<unknown>")
    tool_input = payload.get("tool_input", {})
    tool_use_id = payload.get("tool_use_id")
    return HumanGate(
        id=uuid4().hex,
        prompt=f"批准 {tool_name} 调用？",
        options=["allow", "deny"],  # hook 语义只有允许/拒绝
        context={
            "tool": tool_name,
            "tool_input": tool_input,
            "tool_use_id": tool_use_id,
            "session_id": session_id,
        },
        source="tool_permission",
        run_id=run_id,
        node=node,
        # session_id 透传到 event 顶层（phase 3 §3.3 身份模型）——壳 reducer 据此
        # 关联 gate 事件到具体 claude 会话。从 hook stdin 来的 claude session_id。
        session_id=session_id,
    )


def register_gate_routes(
    app: FastAPI,
    handler: HumanGateHandler,
    registry: SessionContextRegistry,
) -> None:
    """把 POST /gate + POST /gate/respond 注册到 ``app``。

    Args:
        app: FastAPI（或兼容的 Starlette）app 实例。phase 9 web server 构建后传入。
        handler: HumanGateHandler（共享给 request/resolve）。
        registry: SessionContextRegistry（hook stdin session_id → run_id/node 反查）。

    注意：调用方需保证 ``handler.start()`` 已调用（broadcaster 在跑），否则 resolved
    事件不会广播。本函数不替你管生命周期（SRP：只挂路由）。
    """
    # 局部 import：FastAPI 仅在调用 register_gate_routes 时需要（不在模块顶层 import
    # 让 types/handler/registry 等纯逻辑模块保持零 FastAPI 依赖）。
    from fastapi import HTTPException

    @app.post("/gate")
    async def gate_endpoint(payload: dict[str, Any]) -> dict[str, str]:
        """hook → POST /gate。构造 HumanGate → handler.request → 返回决策。

        阻塞至任一壳 resolve（gate 无限等）。返回 ``{decision, resolved_by}``。
        hook 桥根据 decision exit 0/2。
        """
        run_id, node = resolve_session_context(registry, payload)
        gate = build_gate_from_hook_payload(payload, run_id, node)

        try:
            answer, resolved_by = await handler.request(gate)
        except Exception:
            # request 异常（如 bus 已 close）→ 记 error，返回 deny（hook 桥走 exit 2）
            logger.exception("gate_endpoint handler.request 异常（gate=%s）", gate.id)
            raise HTTPException(status_code=500, detail="gate request failed")

        # answer 可能是 "allow"/"deny"（hook 桥固定选项），也可能是壳自定义字符串。
        # 规范化：仅 "allow" 才放行，其它一律 deny（hook 桥安全优先）。
        decision = "allow" if answer == "allow" else "deny"
        return {"decision": decision, "resolved_by": resolved_by, "gate_id": gate.id}

    @app.post("/gate/respond")
    async def gate_respond_endpoint(payload: dict[str, Any]) -> dict[str, bool | str]:
        """壳 → POST /gate/respond。body ``{gate_id, answer, source}`` → handler.resolve。

        返回 ``{ok, gate_id}``。``ok=False`` 表示晚到（已被别的壳答了，fail loud）。
        """
        gate_id = payload.get("gate_id")
        answer = payload.get("answer")
        source = payload.get("source", "web")

        if not gate_id or answer is None:
            raise HTTPException(
                status_code=400, detail="missing gate_id or answer"
            )

        ok = handler.resolve(gate_id, str(answer), str(source))
        return {"ok": ok, "gate_id": gate_id}
