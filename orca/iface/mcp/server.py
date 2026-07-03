"""server.py —— MCP server 骨架 + 工具注册 + stdio 生命周期（SPEC phase-10 §1 §2）。

回答「外部 MCP 客户端（Claude Code / opencode / Cursor）怎么把 Orca workflow 当工具调？」：
单 ``RunManager`` + ``FastMCP`` + HandleId 三件套工具（start_workflow / get_task_status /
resolve_gate）+ cancel_task，stdio JSON-RPC。所有 tool 秒级返回（§0.1 第三条 HandleId pattern）。

设计规则（SPEC §0.1 七条铁律 / §1.4 单例 / §1.3 stdin EOF 双行为）：
  - **单进程单 RunManager**：``run_mcp_server`` 构造后立刻 ``_assert_runmanager_singleton``
    断言进程内 ``RunManager`` 实例数 == 1（防止后续 refactor 误造多实例）。
  - **HandleId pattern**：tool 不阻塞等 gate / 等事件（CC 60s 超时）。start_workflow 复用
    ``manager.start_run``（非阻塞后台 task）；get_task_status 读 tape（replay_state +
    pending_gates）；resolve_gate 复用 ``handler.resolve``（同步非阻塞）。
  - **stdio flush 兜底**：``run_stdio`` 用 SDK 默认 stdio（SDK 1.27.2 已每行 flush）；
    ``transport.FlushingStdoutWriter`` 留作纵深防御 + 单测 mock。
  - **source="mcp"**：resolve_gate 第三参数写死（壳标识，§0.1 第五条）。
  - **不依赖客户端能力**：禁用 elicitation / progress notification（§0.1 第六条）。
  - **返回值文本摘要**：get_task_status 返回 dict 无 dag/chart_json（§0.1 第七条）。
  - **stdin EOF 双行为**：无 --with-web → run_stdio 返回即 drain + 退出；有 --with-web
    → run_stdio 返回后进 daemon 模式等 idle/signal（§1.3）。

依赖单向：本模块依赖 ``orca.iface.web.run_manager``（RunManager，同进程共享）+
``orca.gates``（HumanGate 类型）+ ``orca.iface.mcp.transport`` + ``orca.iface.mcp.hints`` +
mcp SDK（FastMCP / stdio_server）。**不含**编排/gate 决策逻辑——manager 才是托管入口。
"""

from __future__ import annotations

import asyncio
import gc
import logging
import signal
from typing import TYPE_CHECKING, Any

from orca.iface.mcp.hints import (
    after_cancel,
    after_resolve,
    after_start,
    by_status,
    unknown_task,
)
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

    用法::

        manager = RunManager(max_concurrent=3)
        _assert_runmanager_singleton(manager)
        server = OrcaMcpServer(manager)
        await server.run_stdio()  # 阻塞，stdin EOF 退出

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

    async def tool_cancel_task(self, task_id: str, reason: str | None = None) -> dict:
        """取消 run。已终态的 run 调它返回 ok=False。

        **Always call get_task_status(task_id=...) after this returns** to confirm
        the run entered the cancelled state.
        """
        ok = await self._manager.cancel_run(task_id, reason)
        return {
            "ok": ok,
            "status": "cancelled" if ok else "terminal",
            "_hint": after_cancel(ok),
        }

    # ── phase-14：agent 池查询（纯读，list_agents / get_agent）─────────────────

    def _agent_resolve_context(self, yaml_path: str | None):
        """构造 agent 解析上下文（LocalPoolResolver 用）。

        yaml_path 给定 → workflow_dir = 其父目录（扫 workflow 同目录 agents/）；否则 workflow_dir = cwd。
        两者都叠加 cwd/agents/（跨 workflow 复用），first-wins（workflow 同目录优先）。
        """
        from pathlib import Path

        from orca.compile.agents import ResolveContext

        cwd = Path.cwd()
        workflow_dir = Path(yaml_path).resolve().parent if yaml_path else cwd
        return ResolveContext(workflow_dir=workflow_dir, cwd=cwd)

    async def tool_list_agents(self, yaml_path: str | None = None) -> dict:
        """List available agents in the agent pool (read-only, instant).

        Scans ``agents/`` directories (workflow-local + cwd). Pass ``yaml_path`` to
        also scan that workflow's sibling ``agents/`` dir. Use get_agent(name=...) for
        full details; use start_workflow to run a workflow that references an agent.

        参数：
          - yaml_path: 可选，workflow YAML 路径（额外扫其同目录 agents/）。

        返回 ``{agents: [{name, description, has_resources}]}``。has_resources=True 表示
        文件夹形态 agent（含 scripts/refs 子目录，运行时可经 ``$ORCA_AGENT_RESOURCES`` 访问）。
        """
        from orca.compile.agents import LocalPoolResolver

        ctx = self._agent_resolve_context(yaml_path)
        resolver = LocalPoolResolver()
        items: list[dict[str, Any]] = []
        for name, is_folder in resolver.discover(context=ctx):
            # 取 description（resolve 一次拿 frontmatter meta；agent 数少，开销可接受）
            description = ""
            try:
                handle = resolver.resolve(name, context=ctx)
                description = handle.meta.description
            except Exception:  # noqa: BLE001 — 列表不应因单个 agent 解析失败而中断
                logger.warning("list_agents: 解析 agent %r 失败（跳过 description）", name, exc_info=True)
            items.append(
                {"name": name, "description": description, "has_resources": is_folder}
            )
        return {"agents": items}

    async def tool_get_agent(self, name: str, yaml_path: str | None = None) -> dict:
        """Get one agent's details: prompt preview + frontmatter meta + resources.

        agent 不存在 → 返回 ``{name, error}``（不 raise，MCP 友好）。prompt_preview 截断
        前 500 字。resources 仅文件夹形态 agent 列出（agent.md 除外）。

        参数：
          - name: agent 名（pool 内的 <name>）。
          - yaml_path: 可选，workflow YAML 路径（额外扫其同目录 agents/）。
        """
        from pathlib import Path

        from orca.compile.agents import AgentNotFound, LocalPoolResolver

        ctx = self._agent_resolve_context(yaml_path)
        resolver = LocalPoolResolver()
        try:
            handle = resolver.resolve(name, context=ctx)
        except AgentNotFound as e:
            return {"name": name, "error": str(e)}

        resources: list[str] = []
        if handle.is_folder:
            try:
                for p in sorted(handle.resources_root.iterdir()):
                    if p.name == "agent.md":
                        continue
                    resources.append(p.name + ("/" if p.is_dir() else ""))
            except OSError:
                logger.warning("get_agent: 列资源目录 %r 失败", handle.resources_root, exc_info=True)

        return {
            "name": name,
            "description": handle.meta.description,
            "model": handle.meta.model,
            "tools": handle.meta.tools,
            "executor": handle.meta.executor,
            "has_resources": handle.is_folder,
            "resources": resources,
            "prompt_preview": handle.prompt[:500],
            "source": handle.source,
        }

    # ── 构造 + 注册 ────────────────────────────────────────────────────────────

    def __init__(self, manager: RunManager) -> None:
        from mcp.server.fastmcp import FastMCP

        self._manager = manager
        self._mcp = FastMCP("orca")
        self._register_tools()

    def _register_tools(self) -> None:
        """注册四件套到 FastMCP（SPEC §2.1）。

        ``FastMCP.add_tool(fn, name, description=...)``：fn 的类型注解自动派生 inputSchema，
        description 来自 tool docstring（含强指令，§2.4）。bound method 直接传——FastMCP
        内部用 ``inspect.signature`` 解析参数注解。``inspect.cleandoc`` 去除 docstring 缩进，
        让客户端看到的 description 不带前导空格。
        """
        import inspect

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
        self._mcp.add_tool(
            self.tool_cancel_task,
            name="cancel_task",
            description=inspect.cleandoc(self.tool_cancel_task.__doc__ or ""),
        )
        self._mcp.add_tool(
            self.tool_list_agents,
            name="list_agents",
            description=inspect.cleandoc(self.tool_list_agents.__doc__ or ""),
        )
        self._mcp.add_tool(
            self.tool_get_agent,
            name="get_agent",
            description=inspect.cleandoc(self.tool_get_agent.__doc__ or ""),
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

    入口（``orca mcp`` 命令调）。流程：
      1. 构造唯一 RunManager（``runs_dir`` 可覆盖，测试用 tmp_path）。
      2. ``_assert_runmanager_singleton`` 断言进程内实例数 == 1。
      3. 构造 OrcaMcpServer。
      4. 可选挂 Web（``--with-web``）：同进程 asyncio task 跑 ``run_server``，共享 manager。
      5. ``run_stdio`` 阻塞跑（stdin EOF 返回）。
      6. 生命周期分支（§1.3）：
         - 无 --with-web：drain in-flight tool call（最大 5s）+ cancel 后台 run + 退出。
         - 有 --with-web：进 daemon 模式，等 idle_timeout 分钟无活跃 run OR SIGINT/SIGTERM。
    """
    from pathlib import Path

    from orca.iface.web.run_manager import RunManager

    kwargs: dict[str, Any] = {"max_concurrent": max_concurrent}
    if runs_dir is not None:
        kwargs["runs_dir"] = Path(runs_dir)
    manager = RunManager(**kwargs)
    _assert_runmanager_singleton(manager)

    server = OrcaMcpServer(manager)

    web_task: asyncio.Task | None = None
    if with_web:
        web_task = asyncio.create_task(
            _run_web_in_process(manager, port=web_port),
            name="orca-mcp-web-sidecar",
        )
        logger.info("MCP --with-web: Web UI 同进程挂载于 http://127.0.0.1:%d", web_port)

    try:
        await server.run_stdio()
    finally:
        # stdin EOF 已到。SPEC §1.3 双行为分支。
        if not with_web:
            await _drain_and_cancel(manager, web_task)
        else:
            await _wait_for_idle_or_signal(manager, idle_timeout, web_task)


async def _run_web_in_process(manager: RunManager, *, port: int) -> None:
    """同进程 asyncio task 跑 Web server（SPEC §1.4 / §0.1 第一条）。

    共享同一 manager 实例（**禁止** subprocess 起 Web——会丢失 in-memory run owner）。
    用 asyncio task（非 thread）：``run_server`` 本就是 async，与 MCP 同 loop 跑最干净，
    零线程间同步。
    """
    from orca.iface.web import run_server

    await run_server(manager, host="127.0.0.1", port=port)


async def _drain_and_cancel(
    manager: RunManager, web_task: asyncio.Task | None
) -> None:
    """纯 MCP 模式收尾：drain in-flight tool + cancel 后台 run（SPEC §1.3）。

    - 等 in-flight tool call（最大 5s）：复用 ``manager.shutdown`` 的 timeout 机制。
    - cancel 所有未完成 run task（gate-await 的 / orchestrator 跑的）。
    - 关 Web task（若误进——shouldn't happen in this branch）。
    """
    if web_task is not None and not web_task.done():
        web_task.cancel()
        try:
            await web_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    try:
        await manager.shutdown(timeout=5.0)
    except Exception:  # noqa: BLE001 — shutdown 不应崩退出路径
        logger.warning("manager.shutdown 异常（退出路径兜底吞）", exc_info=True)


async def _wait_for_idle_or_signal(
    manager: RunManager,
    idle_timeout_minutes: int,
    web_task: asyncio.Task | None,
) -> None:
    """daemon 模式：等无活跃 run 持续 N 分钟 OR SIGINT/SIGTERM（SPEC §1.3）。

    退出条件（任一满足）：
      - 连续 ``idle_timeout_minutes`` 分钟无 status in {queued, running} 的 run。
      - 收到 SIGINT / SIGTERM。

    退出时收尾：cancel 后台 run + 关 Web task。
    """
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _on_signal(signum: int) -> None:
        logger.info("MCP daemon 收到信号 %d，开始退出", signum)
        stop_event.set()

    # 信号处理（Unix；Windows NotImplementedError 退化到默认 KeyboardInterrupt）。
    installed_sigs: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
            installed_sigs.append(sig)
        except (NotImplementedError, RuntimeError):
            pass

    idle_seconds = max(0, idle_timeout_minutes) * 60
    idle_since: float | None = None

    try:
        while not stop_event.is_set():
            await asyncio.sleep(60)
            active = sum(
                1
                for h in manager._runs.values()
                if h.status in ("queued", "running")
            )
            if active == 0:
                if idle_since is None:
                    idle_since = loop.time()
                elif loop.time() - idle_since >= idle_seconds:
                    logger.info(
                        "MCP daemon 无活跃 run 持续 %d 分钟，退出", idle_timeout_minutes
                    )
                    return
            else:
                idle_since = None
    finally:
        for sig in installed_sigs:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        await _drain_and_cancel(manager, web_task)
