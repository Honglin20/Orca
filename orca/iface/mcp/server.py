"""server.py —— MCP server v4（8 工具 + Result 信封）。

**BREAKING (in-session v5 §6.2, step 5a)**：删 ``get_agent_prompt`` 工具，
``start_workflow`` 删 ``setup_outputs`` 参数。setup phase 全栈已删，旧 MCP 客户端
不再调 ``get_agent_prompt``；``start_workflow`` 不再接收 ``setup_outputs``。YAML 含
``setup:`` 段会被 pydantic ``extra="forbid"`` 拒绝（fail loud）。详见 release note
``docs/releases/2026-07-15-in-session-step5a-setup-removal.md``。

回答「外部 MCP 客户端（Claude Code / opencode / Cursor）怎么把 Orca workflow 当工具调？」：
单 ``RunManager`` + ``FastMCP`` + 8 工具（Discovery 3 + Lifecycle 3 + History 2），
stdio JSON-RPC。所有 tool 秒级返回（§0.1 第三条 HandleId pattern）。

v4 vs 旧 6-tool 设计（2026-07-07 重写）+ in-session v5 §6.2 精简：
  - 删 ``resolve_gate``（execute phase 永不中断，无 needs_decision 状态）
  - 加 ``list_workflows`` / ``describe_workflow`` / ``get_task_history``
  - ``start_workflow`` name-based（catalog 查找）
  - 全部 tool 返回 ``Result`` 信封（``{ok, data?, error?, _hint?}``），error.kind 取
    ``ErrorKind`` 值（ADR §4.1，**无 layer 字段**）
  - in-session v5 §6.2：删 ``get_agent_prompt`` + ``start_workflow`` 删 ``setup_outputs``
    （setup phase 全栈删除）

设计规则（SPEC §0.1 八条铁律 / §1.4 单例 / §1.3 stdin EOF 双行为）：
  - **单进程单 RunManager**：``run_mcp_server`` 构造后立刻 ``_assert_runmanager_singleton``。
  - **HandleId pattern**：tool 不阻塞等 gate / 等事件。start_workflow 复用
    ``manager.start_run``（非阻塞后台 task）。
  - **Result 信封**：所有 tool 返 ``Result``；MCP 序列化为 ``{ok, data?, error?, _hint?}``。
    error.kind 是 ``ErrorKind`` 值（ADR §4.1 决策 1.3/1.4）。
  - **stdio flush 兜底**：``transport.FlushingStdoutWriter`` 留作纵深防御 + 单测 mock。
  - **stdin EOF 双行为**：无 --with-web → run_stdio 返回即 drain + 退出；有 --with-web
    → run_stdio 返回后进 daemon 模式等 idle/signal（§1.3）。

依赖单向：本模块依赖 ``orca.iface.web.run_manager``（RunManager）+
``orca.iface.mcp.{transport, hints, tape_index}`` +
``orca.compile.catalog``（workflow 发现/加载/描述）+
``orca.exec.result``（Result/Error/ErrorKind）+ mcp SDK（FastMCP / stdio_server）。
不含编排/gate 决策逻辑——manager 才是托管入口。
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import logging
from typing import TYPE_CHECKING, Any

from orca.compile import catalog
from orca.exec.error_kinds import ErrorKind
from orca.exec.result import Error, Result
from orca.iface.mcp.hints import (
    after_cancel,
    after_start,
    by_status,
    for_describe_workflow,
    for_get_task_history,
    for_list_workflows,
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


def _result_to_dict(result: Result) -> dict[str, Any]:
    """``Result`` → MCP JSON-RPC 友好的 dict（SPEC §2.4b 信封序列化）。

    序列化为 ``{ok, data?, error?, _hint?}``：
      - ``ok=True`` → ``{ok: True, data: ..., _hint?: ...}``
      - ``ok=False`` → ``{ok: False, error: {kind, message, retryable?}, _hint?}``

    ``error`` 字段集（ADR §4.1 决策 1.3，**无 layer**）：``{kind, message, retryable?}``。
    ``kind`` 是 ``ErrorKind`` 值（str Enum，直接 JSON 序列化）。``raw`` 不进 MCP 信封
    （可能含 backend 敏感数据；诊断走 tape）。
    """
    out: dict[str, Any] = {"ok": result.ok}
    if result.data is not None:
        out["data"] = result.data
    if result.error is not None:
        err: dict[str, Any] = {
            "kind": result.error.kind.value,
            "message": result.error.message,
        }
        if result.error.retryable is not None:
            err["retryable"] = result.error.retryable
        out["error"] = err
    if result._hint is not None:
        out["_hint"] = result._hint
    return out


def _ok(data: Any, hint: str | None = None) -> dict[str, Any]:
    """成功 Result → dict（DRY 快捷）。"""
    return _result_to_dict(Result.ok_(data, hint=hint))


def _err(
    kind: ErrorKind,
    message: str,
    *,
    hint: str | None = None,
    retryable: bool | None = None,
) -> dict[str, Any]:
    """失败 Result → dict（DRY 快捷）。"""
    error = Error(kind=kind, message=message, retryable=retryable)
    return _result_to_dict(Result.err(error, hint=hint))


class OrcaMcpServer:
    """单 RunManager + FastMCP + 8 工具注册（SPEC §1.4 / §2 v4）。

    用法::

        manager = RunManager(max_concurrent=3)
        _assert_runmanager_singleton(manager)
        server = OrcaMcpServer(manager)
        await server.run_stdio()  # 阻塞，stdin EOF 退出

    Tool 实现作为 **bound method**（``self.tool_start_workflow`` 等），既给 FastMCP 注册
    也给单测直接调（绕开 stdio round-trip，SPEC §D3.5 / §D4.4）。所有 tool 返回
    ``dict``（``_result_to_dict`` 序列化的 Result 信封）。
    """

    # ── Discovery 组（3 个）────────────────────────────────────────────────────

    async def tool_list_workflows(self) -> dict[str, Any]:
        """List available workflows in the catalog (read-only, instant).

        Scans project-local ``workflows/`` + user-global ``~/.orca/workflows/``.
        Each entry includes ``inputs_schema`` for picking a workflow + extracting
        inputs in one command. Call ``describe_workflow(name=...)`` next for
        full input metadata.
        """
        workflows = catalog.list_workflows()
        return _ok({"workflows": workflows}, hint=for_list_workflows())

    async def tool_describe_workflow(self, name: str) -> dict[str, Any]:
        """Get detailed metadata for one workflow (without starting it).

        Returns ``{name, description, inputs_schema}``. ``inputs_schema`` is a
        dict ``{key: {type, required, description}}`` for filling ``start_workflow``.

        Decision flow:
          - inputs complete → call start_workflow
          - inputs incomplete → ask user for missing fields, then start_workflow
        """
        wf = catalog.find_workflow_by_name(name)
        if wf is None:
            return _err(
                ErrorKind.BUSINESS_CONFIG,
                f"Workflow '{name}' not found in catalog. "
                "Call list_workflows to see available names.",
                hint="Call list_workflows to discover available workflows.",
            )
        detail = catalog.describe_workflow(wf)
        inputs_complete = _inputs_complete(wf)
        hint = for_describe_workflow(
            inputs_complete=inputs_complete,
            name=name,
        )
        return _ok(detail, hint=hint)

    async def tool_list_agents(
        self, yaml_path: str | None = None
    ) -> dict[str, Any]:
        """List available agents in the agent pool (read-only, instant).

        Scans ``agents/`` directories (workflow-local + cwd). Pass ``yaml_path``
        to also scan that workflow's sibling ``agents/`` dir.

        Returns ``{agents: [{name, description, has_resources}]}``.
        """
        from pathlib import Path

        from orca.compile.agents import (
            AgentNotFound,
            LocalPoolResolver,
            ResolveContext,
        )
        from orca.compile.validator import ConfigurationError

        cwd = Path.cwd()
        workflow_dir = Path(yaml_path).resolve().parent if yaml_path else cwd
        ctx = ResolveContext(workflow_dir=workflow_dir, cwd=cwd)
        resolver = LocalPoolResolver()
        items: list[dict[str, Any]] = []
        for agent_name, is_folder in resolver.discover(context=ctx):
            description = ""
            try:
                handle = resolver.resolve(agent_name, context=ctx)
                description = handle.meta.description
            except (AgentNotFound, ConfigurationError, OSError):
                logger.warning(
                    "list_agents: 解析 agent %r 失败（跳过 description）",
                    agent_name,
                    exc_info=True,
                )
            items.append(
                {
                    "name": agent_name,
                    "description": description,
                    "has_resources": is_folder,
                }
            )
        return _ok({"agents": items})

    # ── Lifecycle 组（3 个）────────────────────────────────────────────────────

    async def tool_start_workflow(
        self,
        name: str,
        inputs: dict | None = None,
        task: str | None = None,
        max_iter: int | None = None,
    ) -> dict[str, Any]:
        """Start an Orca workflow, returns task_id immediately (non-blocking).

        **Before starting**: call ``describe_workflow(name=...)`` to see required
        inputs, then ask the user for any missing fields.

        **After starting**: call ``get_task_status(task_id=...)`` to poll.
        Long-running: do NOT poll more than once per turn. End your turn after polling.

        Args:
            name: workflow name (from list_workflows).
            inputs: workflow inputs.
            task: sugar for ``inputs.task``.
            max_iter: override max_iterations.
        """
        # catalog 反查（DRY：单次扫描同时拿 Workflow + yaml_path）
        found = catalog.find_workflow(name)
        if found is None:
            return _err(
                ErrorKind.BUSINESS_CONFIG,
                f"Workflow '{name}' not found in catalog.",
                hint="Call list_workflows to discover available workflows.",
            )
        _wf, yaml_path = found

        # 启动 run（非阻塞，HandleId pattern）。
        run_id = await self._manager.start_run(
            yaml_path, inputs, task, max_iter
        )
        return _ok(
            {"task_id": run_id, "status": "running"},
            hint=after_start(run_id),
        )

    async def tool_get_task_status(self, task_id: str) -> dict[str, Any]:
        """Query task status. Returns instantly (non-blocking).

        Returns status: running | completed | failed | cancelled | unknown
        (NO ``needs_decision`` — execute phase never interrupts).
          - completed: includes ``output`` (workflow outputs).
          - failed: includes ``error``.
          - running: includes ``progress`` ("3/7") + ``current_node``.

        **Always call this after start_workflow**, until terminal status.
        Running: **end your turn** after polling to avoid polling loops.
        """
        summary = self._manager.run_summary(task_id)
        if summary is None:
            return _ok(
                {"task_id": task_id, "status": "unknown"},
                hint=unknown_task(),
            )
        # v4：gate 字段恒为 None（execute phase 永不中断，MCP 不暴露 resolve_gate）
        # 不原地 mutate（防 RunManager 内部缓存污染）——浅拷贝过滤
        summary = {k: v for k, v in summary.items() if k != "gate"}
        hint = by_status(summary["status"])
        return _ok(summary, hint=hint)

    async def tool_cancel_task(
        self, task_id: str, reason: str | None = None
    ) -> dict[str, Any]:
        """Cancel a run. Terminal runs return ok=False.

        **Always call get_task_status(task_id=...) after this** to confirm
        the run entered the cancelled state.
        """
        ok = await self._manager.cancel_run(task_id, reason)
        return _ok(
            {"ok": ok, "status": "cancelled" if ok else "terminal"},
            hint=after_cancel(ok),
        )

    # ── History 组（2 个）──────────────────────────────────────────────────────

    async def tool_get_task_history(
        self, task_id: str, limit: int = 50
    ) -> dict[str, Any]:
        """Get task event history (tape replay, read-only).

        Reads the full tape for ``task_id`` and returns a chronological summary
        of events (capped at ``limit``, most recent last). Each event entry:
        ``{seq, type, node, summary, session_id?}``.

        Use this for debugging / audit / "what happened so far". For current
        status, use ``get_task_status``.
        """
        from orca.iface.mcp.tape_index import summarize_events

        try:
            events = self._manager.get_run_events(task_id)
        except KeyError:
            return _err(
                ErrorKind.BUSINESS_CONFIG,
                f"Unknown task_id: {task_id}",
                hint=unknown_task(),
            )
        history = summarize_events(events, limit=limit)
        return _ok(
            {"task_id": task_id, "events": history, "count": len(history)},
            hint=for_get_task_history(),
        )

    async def tool_get_agent(
        self, name: str, yaml_path: str | None = None
    ) -> dict[str, Any]:
        """Get one agent's full details: prompt preview + meta + resources.

        ``name`` not found → ``{ok: False, error: {...}}`` (does not raise).
        ``prompt_preview`` truncated to 500 chars. ``resources`` lists folder
        agent's subdirectory contents (excluding agent.md).
        """
        from pathlib import Path

        from orca.compile.agents import (
            AgentNotFound,
            LocalPoolResolver,
            ResolveContext,
        )

        cwd = Path.cwd()
        ctx = ResolveContext(
            workflow_dir=Path(yaml_path).resolve().parent if yaml_path else cwd,
            cwd=cwd,
        )
        resolver = LocalPoolResolver()
        try:
            handle = resolver.resolve(name, context=ctx)
        except AgentNotFound as e:
            return _err(
                ErrorKind.BUSINESS_CONFIG,
                str(e),
                hint="Call list_agents to see available agents.",
            )

        resources: list[str] = []
        if handle.is_folder:
            try:
                for p in sorted(handle.resources_root.iterdir()):
                    if p.name == "agent.md":
                        continue
                    resources.append(p.name + ("/" if p.is_dir() else ""))
            except OSError:
                logger.warning(
                    "get_agent: 列资源目录 %r 失败",
                    handle.resources_root,
                    exc_info=True,
                )

        return _ok(
            {
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
        )

    # ── 构造 + 注册 ────────────────────────────────────────────────────────────

    def __init__(self, manager: RunManager) -> None:
        from mcp.server.fastmcp import FastMCP

        self._manager = manager
        self._mcp = FastMCP("orca")
        self._register_tools()

    def _register_tools(self) -> None:
        """注册 8 工具到 FastMCP（SPEC §2.1 v4 + in-session v5 §6.2 精简）。

        ``FastMCP.add_tool(fn, name, description=...)``：fn 的类型注解自动派生 inputSchema，
        description 来自 tool docstring（含强指令，§2.6）。bound method 直接传——FastMCP
        内部用 ``inspect.signature`` 解析参数注解。``inspect.cleandoc`` 去除 docstring 缩进。
        """
        tools = [
            (self.tool_list_workflows, "list_workflows"),
            (self.tool_describe_workflow, "describe_workflow"),
            (self.tool_list_agents, "list_agents"),
            (self.tool_start_workflow, "start_workflow"),
            (self.tool_get_task_status, "get_task_status"),
            (self.tool_cancel_task, "cancel_task"),
            (self.tool_get_task_history, "get_task_history"),
            (self.tool_get_agent, "get_agent"),
        ]
        for fn, name in tools:
            self._mcp.add_tool(
                fn,
                name=name,
                description=inspect.cleandoc(fn.__doc__ or ""),
            )

    # ── stdio 生命周期 ─────────────────────────────────────────────────────────

    async def run_stdio(self) -> None:
        """阻塞跑 stdio MCP，stdin EOF 退出（SPEC §1.3）。

        mcp SDK 1.27.2 的 ``stdio_server`` 在 stdin EOF 时让 ``stdin_reader`` 自然退出，
        task group 收尾 → ``_mcp_server.run`` 返回 → 本方法返回。
        """
        await self._mcp.run_stdio_async()


# ── helpers ────────────────────────────────────────────────────────────────────


def _inputs_complete(wf: Any) -> bool:
    """粗判 workflow inputs 是否「全填」（给 ``describe_workflow`` hint 用）。

    启动前校验由 ``manager.start_run`` 内部做（``Orchestrator`` 渲染时 fail loud）；
    本函数只给主 session 一个粗略引导，不做精确校验。
    """
    required = [k for k, v in wf.inputs.items() if v.required]
    return len(required) == 0  # 无 required 字段 = inputs 完整


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
    """纯 MCP 模式收尾：drain in-flight tool + cancel 后台 run（SPEC §1.3）。"""
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
    import signal

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
