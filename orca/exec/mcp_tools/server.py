"""server.py —— AgentToolsMcpServer：内嵌 SSE MCP server，暴露 ask_user 给被编排的 claude -p。

回答「被 orca spawn 的 claude agent 怎么主动问用户（如要一个数据库连接串）？」：
Orca 进程内嵌一个 socket MCP server（监听 loopback port），注册 ``ask_user`` 工具。
claude -p 经 ``--mcp-config <json>`` 连上 → 调 ``ask_user`` → 触发 ``HumanGate(source=
agent_ask)`` → 等任一壳 resolve → 返回 answer。

**SSE spike 前置已验证**（2026-07-02）：``mcp.server.fastmcp`` 的 SSE server 可被 in-memory
MCP ``ClientSession`` 调通（echo 工具 round-trip），且**真实 claude -p ``--mcp-config``**
能连上 SSE server 并调通工具（需 ``--allowed-tools mcp__<server>__<tool>`` 授权）。详见
release note。

与 phase 10 ``OrcaMcpServer`` 的边界（SPEC §5.3）：
  - phase 10 ``OrcaMcpServer``（stdio）：给**外部** CC 主对话用，暴露 start_workflow 等。
  - 本类 ``AgentToolsMcpServer``（socket SSE）：给 orca **内部 spawn** 的 claude 用，
    暴露 ask_user 等。

路由设计（SPEC §5.3 / §5.5，决策 D4）：**确定性 tool-params 路由**——``ask_user`` 工具入参
强制带 ``orca_run_id`` / ``orca_node``（路由参）。缺失 → raise RuntimeError（fail loud）。
这比依赖 MCP session 反查可靠（claude -p 不主动报 MCP session）。

> **SPEC 偏离（SPEC §11 记录）**：SPEC §5.3 写的是 ``_orca_run_id`` / ``_orca_node``（下划线
> 前缀 hidden params），但 ``mcp.server.fastmcp`` 拒绝以下划线开头的参数
> （``InvalidSignature: Parameter ... cannot start with '_'``，FastMCP 把它当私有/内部）。
> 故改为 ``orca_run_id`` / ``orca_node``（无下划线前缀）。语义不变（确定性路由参），仅命名。
> render_prompt 的 instruct 文本同步用 ``orca_run_id`` / ``orca_node``。Rule 7：选可跑的命名。

生命周期：
  - ``start()``：lazy，找空闲 loopback port，启动 SSE server（uvicorn）后台 task。返回 port。
  - ``stop()``：幂等，cancel server task。
  - ``write_config(session_id, run_id, node)``：写 ``runs/<run_id>/mcp_<session>.json``，
    给 claude -p 的 ``--mcp-config`` flag 用。

依赖单向：本模块依赖 ``mcp.server.fastmcp`` + ``orca.gates``（ask_user / HumanGateHandler /
SessionContextRegistry）。**不依赖** ``iface/`` / ``run/`` / ``compile/``。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import socket
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

from orca.gates import ask_user as gates_ask_user

if TYPE_CHECKING:
    from orca.gates.context_registry import SessionContextRegistry
    from orca.gates.handler import HumanGateHandler

logger = logging.getLogger(__name__)

# MCP server 名（claude 看到的 tool 前缀：``mcp__<name>__<tool>``）。固定，与 write_config
# 的 ``mcpServers`` key 一致——claude ``--allowed-tools mcp__orca-agent-tools__ask_user`` 授权。
_SERVER_NAME = "orca-agent-tools"
# 默认 runs 目录（与 RunManager 默认一致；构造时可覆盖，测试用 tmp_path）。
_DEFAULT_RUNS_DIR = "runs"


class AgentToolsMcpServer:
    """内嵌 socket SSE MCP server（SPEC §5.3），暴露 ``ask_user`` 给被编排的 claude -p。

    用法（orchestrator 视角）::

        server = AgentToolsMcpServer(handler, registry)
        port = await server.start()           # lazy，第一个 agent spawn 前调
        try:
            ...                               # ClaudeExecutor spawn 时 write_config + register
        finally:
            await server.stop()               # 幂等
    """

    def __init__(
        self,
        handler: HumanGateHandler,
        registry: SessionContextRegistry,
        *,
        runs_dir: str | Path = _DEFAULT_RUNS_DIR,
    ) -> None:
        self._handler = handler
        self._registry = registry
        self._runs_dir = Path(runs_dir)
        self._mcp = FastMCP(_SERVER_NAME, host="127.0.0.1")
        self._server_task: asyncio.Task[None] | None = None
        self._port: int | None = None
        self._register_tools()

    # ── 公开 API ─────────────────────────────────────────────────────────────

    async def start(self) -> int:
        """启动 SSE MCP server（lazy），返回绑定的 loopback port。

        幂等：重复调返回已绑定的 port（不重启）。

        **TOCTOU 说明**：``_find_free_loopback_port`` 选 port → uvicorn re-bind 之间有窗口
        期被抢。loopback 上概率极低（spike 多轮未命中）。uvicorn bind 失败会 ``sys.exit(1)``
        （SystemExit），无法在 task 内干净捕获——故 ``start()`` 不做同步 bind 探测，依赖：
          (a) loopback 低概率（实测可接受）；
          (b) 万一命中，claude spawn 后连不上 SSE → exit != 0 → node_failed（错误在 spawn
              时可见，非静默挂起）；
          (c) orchestrator 的 ``_start_agent_tools`` 包了 try/except，任何 start 异常 →
              workflow_failed（fail loud，SPEC §11.4 / 铁律 12）。
        """
        if self._server_task is not None and not self._server_task.done():
            assert self._port is not None
            return self._port
        # 若上次 task 已 done（含异常退出），清掉重起。
        self._server_task = None
        self._port = None

        port = _find_free_loopback_port()
        # FastMCP settings 在构造时固化 host/port；这里用 ``_mcp.settings`` 直接改 port
        # （pydantic BaseSettings 实例字段可写）。host 已是 127.0.0.1（构造时设）。
        self._mcp.settings.port = port
        self._server_task = asyncio.create_task(
            self._mcp.run_sse_async(), name="orca-agent-tools-mcp-sse"
        )
        self._port = port
        logger.info("AgentToolsMcpServer SSE 起：http://127.0.0.1:%d/sse", port)
        return port

    async def stop(self) -> None:
        """关闭 SSE server。幂等（重复调 / 未 start 调都不报错）。"""
        task = self._server_task
        self._server_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — 退出路径兜底，不阻断 finally
            logger.warning("AgentToolsMcpServer stop 异常（已吞）", exc_info=True)
        self._port = None

    def write_config(self, session_id: str, run_id: str, node: str) -> Path:
        """写 ``--mcp-config`` JSON（SSE transport）到 ``runs/<run_id>/mcp_<session>.json``。

        claude -p 经此文件连本 server。spawn 完后可删（本阶段保留，便于调试 + claude 进程
        生命周期内可能重读）。
        """
        if self._port is None:
            # 编程错误：write_config 在 start() 之前调。fail loud。
            raise RuntimeError(
                "AgentToolsMcpServer.write_config 在 start() 之前调（port 未绑定）"
            )
        config = {
            "mcpServers": {
                _SERVER_NAME: {
                    "type": "sse",
                    "url": f"http://127.0.0.1:{self._port}/sse",
                }
            }
        }
        out_dir = self._runs_dir / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"mcp_{session_id}.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    @property
    def port(self) -> int | None:
        """已绑定的 loopback port（未 start 返回 None）。"""
        return self._port

    @property
    def server_name(self) -> str:
        """MCP server 名（claude tool 前缀用：``mcp__<server_name>__ask_user``）。"""
        return _SERVER_NAME

    @property
    def registry(self) -> SessionContextRegistry:
        """底层 SessionContextRegistry（orchestrator 在 node 边界 unregister 清理用）。"""
        return self._registry

    def register_session(self, *, session_id: str, run_id: str, node: str) -> None:
        """登记 session_id → (run_id, node) 路由（phase 11 §5.5 register debt）。

        ClaudeExecutor spawn claude 前（写 mcp-config 之后）调此方法。HumanGateHandler 的
        gate 答案回流依赖此映射（hook 桥 / ask_user 路由）。last-writer-wins（claude 重连）。

        本方法是 ``registry.register`` 的薄封装（AgentToolsMcpServer 作为 ask_user 闭环的
        单一 facade，ClaudeExecutor 不直接接触 registry，降低耦合）。
        """
        self._registry.register(session_id, run_id, node)

    def unregister_session(self, session_id: str) -> None:
        """清理 session 路由（orchestrator 在 node 边界 / 完成时调，幂等）。"""
        self._registry.unregister(session_id)

    def unregister_run(self, run_id: str) -> int:
        """清理某 run 的全部 session 路由（workflow 结束时调，防内存泄漏，SPEC §6）。

        session_id 由 ClaudeExecutor 内部 uuid 生成，orchestrator 不持有；故按 run_id 批清。
        返回清理条目数（可观测）。委托 ``registry.unregister_run``。
        """
        return self._registry.unregister_run(run_id)

    # ── 工具注册 ─────────────────────────────────────────────────────────────

    def _register_tools(self) -> None:
        """注册 ``ask_user`` 工具到 FastMCP（确定性 tool-params 路由，SPEC §5.3）。

        ``ask_user`` 入参强制带 ``orca_run_id`` / ``orca_node``（路由参）。
        ``render_prompt`` instruct claude 调用时必填。缺失 → raise RuntimeError（fail loud）。
        路由参校验通过后，调 ``orca.gates.ask_user`` 触发 HumanGate(source=agent_ask)。
        """

        async def ask_user(  # noqa: RUF029 — MCP 工具签名必须 async（FastMCP 要求）
            prompt: str,
            options: list[str] | None = None,
            orca_run_id: str = "",
            orca_node: str = "",
        ) -> str:
            """Ask the user a question. Blocks until the user answers.

            Args:
                prompt: Question for the user.
                options: Fixed choices, or None for free text.
                orca_run_id: MUST be filled — the current Orca run id (routing).
                orca_node: MUST be filled — the current Orca node name (routing).

            Returns:
                The user's answer (an option text if options given, else free text).
            """
            if not orca_run_id or not orca_node:
                # fail loud：claude 没带路由参（prompt instruction 没生效 / 被绕过）。
                raise RuntimeError(
                    "ask_user missing routing params: orca_run_id and orca_node "
                    "are required (both must be non-empty)."
                )
            # session_id = run_id:node（与 HumanGate.session_id 透传约定一致：壳按 session
            # 分组关联到 claude 会话；这里确定性派生，不依赖 MCP session 反查）。
            session_id = f"{orca_run_id}:{orca_node}"
            return await gates_ask_user(
                handler=self._handler,
                prompt=prompt,
                options=options,
                run_id=orca_run_id,
                node=orca_node,
                session_id=session_id,
            )

        self._mcp.add_tool(
            ask_user,
            name="ask_user",
            description=inspect.cleandoc(ask_user.__doc__ or ""),
        )


# ── helpers ──────────────────────────────────────────────────────────────────


def _find_free_loopback_port() -> int:
    """找一个空闲的 loopback TCP port（bind 0 让 OS 分配，立即释放给后续 uvicorn 用）。

    存在 TOCTOU race（释放后到 uvicorn bind 前可能被别的进程抢），但 loopback 上概率极低，
    且 uvicorn bind 失败会抛——上层 start() 的调用方会看到。可接受（spike 已验证可行）。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()
