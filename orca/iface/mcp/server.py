"""server.py —— MCP server + 工具注册 + stdio 生命周期（SPEC phase-10 §1 §2）。

回答「外部 MCP 客户端（Claude Code / opencode / Cursor）怎么把 Orca workflow 当工具调？」：
单 ``RunManager`` + ``FastMCP`` + HandleId 工具集，stdio JSON-RPC。所有 tool 秒级返回
（§0.1 第三条 HandleId pattern）。

设计规则（SPEC §0.1 七条铁律 / §1.4 单例 / §1.3 stdin EOF 双行为）：
  - **单进程单 RunManager**：``run_mcp_server`` 构造后立刻 ``_assert_runmanager_singleton``
    断言进程内 ``RunManager`` 实例数 == 1（防止后续 refactor 误造多实例）。
  - **HandleId pattern**：tool 不阻塞等 gate / 等事件（CC 60s 超时）。start_workflow 复用
    ``manager.start_run``（非阻塞后台 task）；get_task_status 读 tape（replay_state +
    pending_gates）；resolve_gate 复用 ``handler.resolve``（同步非阻塞）。
  - **stdio flush 兜底**：``run_stdio`` 用 SDK 默认 stdio；``transport.FlushingStdoutWriter``
    留作纵深防御 + 单测 mock。
  - **source="mcp"**：resolve_gate 第三参数写死（壳标识，§0.1 第五条）。
  - **不依赖客户端能力**：禁用 elicitation / progress notification（§0.1 第六条）。
  - **返回值文本摘要**：get_task_status 返回 dict 无 dag/chart_json（§0.1 第七条）。

D3：补 start_workflow / get_task_status / resolve_gate 三件套（D4 补 cancel_task + 生命周期）。

依赖单向：本模块依赖 ``orca.iface.web.run_manager``（RunManager，同进程共享）+
``orca.iface.mcp.transport`` + ``orca.iface.mcp.hints`` + mcp SDK。**不含**编排/gate 决策
逻辑——manager 才是托管入口。
"""

from __future__ import annotations

import gc
import inspect
import logging
from typing import TYPE_CHECKING, Any

from orca.iface.mcp.hints import after_resolve, after_start, by_status, unknown_task
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

    Tool 实现作为 **bound method**（``self.tool_start_workflow`` 等），既给 FastMCP 注册
    也给单测直接调（绕开 stdio round-trip，SPEC §D3.5 / §D4.4）。
    """

    # ── tool 实现（bound method，FastMCP 注册 + 单测直调共用）──────────────────

    async def tool_start_workflow(
        self,
        yaml_path: str,
        inputs: dict | None = None,
        task: str | None = None,
        max_iter: int | None = None,
    ) -> dict:
        """启动 Orca workflow，立即返回 task_id（不阻塞）。

        **Always call get_task_status(task_id=...) after this returns**, and again
        after each resolve_gate, until status is completed/failed/cancelled.

        Long-running workflows: do NOT poll more than once per turn. End your
        turn after polling and let the user ask for updates.

        参数：
          - yaml_path: workflow YAML 文件路径。
          - inputs: 可选，workflow inputs dict（key/value）。
          - task: 可选，等价 inputs.task（语法糖）。
          - max_iter: 可选，覆盖 max_iterations。

        返回 ``{task_id, status:"running", _hint}``。task_id 即 run_id。
        """
        run_id = await self._manager.start_run(yaml_path, inputs, task, max_iter)
        return {
            "task_id": run_id,
            "status": "running",
            "_hint": after_start(run_id),
        }

    async def tool_get_task_status(self, task_id: str) -> dict:
        """查询 task 当前状态。秒级返回，不阻塞。

        **Always call this after start_workflow and after each resolve_gate**,
        until status is completed/failed/cancelled. Running workflows: end your
        turn after polling to avoid polling loops.

        返回 status: running | needs_decision | completed | failed | cancelled | unknown。
          - needs_decision：含 gate 详情（gate_id / prompt / options / context），
            需调 resolve_gate。
          - completed：含 output（workflow outputs）。
          - failed：含 error。
          - running：含 progress（"3/7"）+ current_node。
        """
        summary = self._manager.run_summary(task_id)
        if summary is None:
            return {"task_id": task_id, "status": "unknown", "_hint": unknown_task()}
        summary["_hint"] = by_status(summary["status"])
        return summary

    async def tool_resolve_gate(
        self, task_id: str, gate_id: str, decision: str
    ) -> dict:
        """对 needs_decision 的 task 提交人的决策。

        **Always call get_task_status(task_id=...) after this returns** to see
        the new state. ok=False means another channel (web/CLI) already resolved
        the gate first.

        decision 通常是 gate.options 之一或自由文本。返回 ok：True=赢家（answer 生效）/
        False=输家（已被别壳答）。
        """
        handle = self._manager.get_handle(task_id)
        if handle is None:
            return {
                "ok": False,
                "status": "unknown",
                "_hint": unknown_task(),
            }
        # source="mcp" 写死（SPEC §0.1 第五条）：壳标识，复用 handler.resolve 竞速入口。
        ok = handle.gate_handler.resolve(gate_id, decision, source="mcp")
        return {
            "ok": ok,
            "status": "running" if ok else "needs_decision",
            "_hint": after_resolve(ok),
        }

    # ── 构造 + 注册 ────────────────────────────────────────────────────────────

    def __init__(self, manager: RunManager) -> None:
        from mcp.server.fastmcp import FastMCP

        self._manager = manager
        self._mcp = FastMCP("orca")
        self._register_tools()

    def _register_tools(self) -> None:
        """注册三件套到 FastMCP（SPEC §2.1）。

        ``FastMCP.add_tool(fn, name, description=...)``：fn 的类型注解自动派生 inputSchema，
        description 来自 tool docstring（含强指令，§2.4）。bound method 直接传——FastMCP
        内部用 ``inspect.signature`` 解析参数注解。``inspect.cleandoc`` 去除 docstring 缩进，
        让客户端看到的 description 不带前导空格。

        D4 在此追加 cancel_task。
        """
        self._mcp.add_tool(
            self.tool_start_workflow,
            name="start_workflow",
            description=inspect.cleandoc(self.tool_start_workflow.__doc__ or ""),
        )
        self._mcp.add_tool(
            self.tool_get_task_status,
            name="get_task_status",
            description=inspect.cleandoc(self.tool_get_task_status.__doc__ or ""),
        )
        self._mcp.add_tool(
            self.tool_resolve_gate,
            name="resolve_gate",
            description=inspect.cleandoc(self.tool_resolve_gate.__doc__ or ""),
        )

    # ── stdio 生命周期 ─────────────────────────────────────────────────────────

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

    D3 仅落 start_workflow / get_task_status / resolve_gate 三件套；D4 补 cancel_task
    + stdin EOF 双行为生命周期（--with-web 守护模式）。
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
