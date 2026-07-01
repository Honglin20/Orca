"""server.py —— MCP server 骨架 + tool 注册入口 + stdio 生命周期（SPEC phase-10 §1 §2）。

回答「外部 MCP 客户端（Claude Code / opencode / Cursor）怎么把 Orca workflow 当工具调？」：
单 ``RunManager`` + ``FastMCP`` + HandleId 工具集，stdio JSON-RPC。所有 tool 秒级返回
（§0.1 第三条 HandleId pattern）。

设计规则（SPEC §0.1 七条铁律 / §1.4 单例 / §1.3 stdin EOF 双行为）：
  - **单进程单 RunManager**：``run_mcp_server`` 构造后立刻 ``_assert_runmanager_singleton``
    断言进程内 ``RunManager`` 实例数 == 1（防止后续 refactor 误造多实例）。
  - **HandleId pattern**：tool 不阻塞等 gate / 等事件（CC 60s 超时）。
  - **stdio flush 兜底**：``run_stdio`` 用 SDK 默认 stdio；``transport.FlushingStdoutWriter``
    留作纵深防御 + 单测 mock。
  - **不依赖客户端能力**：禁用 elicitation / progress notification（§0.1 第六条）。

D2 仅落骨架（构造 + 单例 assert + run_stdio + run_mcp_server 入口）。tool 实现（start_workflow /
get_task_status / resolve_gate / cancel_task）+ stdin EOF 双行为生命周期在 D3/D4 增量补。

依赖单向：本模块依赖 ``orca.iface.web.run_manager``（RunManager，同进程共享）+
``orca.iface.mcp.transport`` + mcp SDK（FastMCP）。**不含**编排/gate 决策逻辑——manager 才是托管入口。
"""

from __future__ import annotations

import gc
import logging
from typing import TYPE_CHECKING, Any

from orca.iface.mcp.transport import FlushingStdoutWriter  # noqa: F401 — 纵深防御，单测/SPEC §4.4 引用

if TYPE_CHECKING:
    from orca.iface.web.run_manager import RunManager

logger = logging.getLogger(__name__)


def _assert_runmanager_singleton(manager: RunManager) -> None:
    """断言进程内 RunManager 实例数 == 1（SPEC §1.4 / §0.1 第一条）。

    用 ``gc.get_objects()`` 数存活实例。重复构造 → raise ``RuntimeError`` + log error
    （fail loud，§12）。单测覆盖：构造第二个 RunManager 应被检测。

    为何用 gc 而非模块 flag：RunManager 是 D1 已稳定的类，不应为 MCP 层增加类变量
    （OCP——新能力靠新增模块/策略，不改核心路径）。gc 计数只在启动时跑一次，性能可接受。
    """
    # 延迟 import：RunManager 仅此处用（避免模块顶层环依赖；TYPE_CHECKING 不够——
    # isinstance 需 runtime 类对象）。
    from orca.iface.web.run_manager import RunManager as _RunManager

    instances = [o for o in gc.get_objects() if isinstance(o, _RunManager)]
    if len(instances) != 1:
        logger.error(
            "RunManager singleton 违反：进程内实例数=%d（应为 1）。"
            "多壳必须在同进程共享同一 RunManager 实例（SPEC §0.1 第一条）。",
            len(instances),
        )
        raise RuntimeError(
            f"RunManager singleton violated: {len(instances)} instances "
            f"(expected 1). Multiple shells must share one RunManager in-process."
        )


class OrcaMcpServer:
    """单 RunManager + FastMCP + tool 注册（SPEC §1.4 / §2）。

    D2 仅占位 _register_tools（D3/D4 增量补 tool 实现）。``run_stdio`` 委托 FastMCP。
    """

    def __init__(self, manager: RunManager) -> None:
        from mcp.server.fastmcp import FastMCP

        self._manager = manager
        self._mcp = FastMCP("orca")
        self._register_tools()

    def _register_tools(self) -> None:
        """注册 MCP 工具到 FastMCP（SPEC §2.1）。

        D2 占位；D3 补 start_workflow / get_task_status / resolve_gate，D4 补 cancel_task。
        """
        # 占位：D3 在此 add_tool 三个工具，D4 加 cancel_task。

    async def run_stdio(self) -> None:
        """阻塞跑 stdio MCP，stdin EOF 退出（SPEC §1.3）。

        mcp SDK 1.27.2 的 ``stdio_server`` 在 stdin EOF 时让 ``stdin_reader`` 自然退出，
        task group 收尾 → ``_mcp_server.run`` 返回 → 本方法返回。
        """
        await self._mcp.run_stdio_async()


# ── 入口 ──────────────────────────────────────────────────────────────────────


async def run_mcp_server(
    *,
    with_web: bool = False,
    web_port: int = 7428,
    max_concurrent: int = 3,
    idle_timeout: int = 30,
    runs_dir: str | None = None,
) -> None:
    """启动 MCP server（SPEC §1.4 / §5.5）。

    D2 仅落骨架：构造 RunManager + 单例 assert + OrcaMcpServer + run_stdio。
    D4 补 --with-web 同进程 Web + stdin EOF 双行为生命周期。
    """
    from pathlib import Path

    from orca.iface.web.run_manager import RunManager

    kwargs: dict[str, Any] = {"max_concurrent": max_concurrent}
    if runs_dir is not None:
        kwargs["runs_dir"] = Path(runs_dir)
    manager = RunManager(**kwargs)
    _assert_runmanager_singleton(manager)

    server = OrcaMcpServer(manager)
    await server.run_stdio()
