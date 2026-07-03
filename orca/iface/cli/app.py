"""app.py —— OrcaApp：Textual TUI 主壳（SPEC §3.2 / §6.0 铁律）。

回答「用户在终端怎么跑一个 workflow、看进度、回答 gate？」：编排主流程（Orchestrator）
作为 ``@work`` 协程跑，事件消费协程 ``async for event in sub.events()`` 把事件分发到
DAG/ActiveNode/LogStream widget；gate 触发时 ``push_screen_wait(GateModal)`` 阻塞
编排 worker 但 UI 事件循环继续刷新（Textual 决定性优势，SPEC §1 决策 1）。

**5 条铁律（SPEC §6.0）**：
  1. **壳无业务真相**：所有 widget 状态由事件流注入（``_dispatch_to_widgets``）；
     重连/重启从 tape replay 同样的事件流，渲染必然一致。shell 不存 gate 状态。
  2. **gate 走 phase 6 handler.resolve**：用户答 → ``handler.resolve(gate.id, answer, "cli")``；
     广播输家路径（收到 ``human_decision_resolved`` 且本壳 modal 还在）→ 不 resolve（赢家已 resolve）。
  3. **编排主流程不阻塞 UI**：``_run_pipeline`` 是 ``@work`` worker；gate 时
     ``await push_screen_wait`` 阻塞该 worker，UI 事件循环继续刷新 DAG/日志。
  4. **依赖单向**：本模块只 import ``orca.{run, gates, events, compile, schema}`` +
     textual/stdlib，不被任何模块 import（grep ``from orca.iface`` 应为空）。
  5. **Textual（非 Rich Live）**：gate prompt 在渲染期输入（Rich Live 做不到）。

**Gate 触发架构决策**（任务要求 resolve 的架构决策，记录于此防 drift）：
orchestrator 对 gate 透明（phase 6 §2.3）—— gate 触发在两个独立来源：
  - **tool_permission**：claude 的 PreToolUse hook POST 到 ``/gate``。本 CLI 必须跑一个
    **后台 HTTP server**（FastAPI + uvicorn，``register_gate_routes``），共享同一份
    ``HumanGateHandler`` + ``SessionContextRegistry`` + ``EventBus``。Hook 由 claude spawn，
    是独立短命进程，与 TUI 不在同一线程/loop。
  - **agent_ask**：``ask_user(handler, ...)`` 在 node 执行内调（无需 HTTP）。

**HTTP server 隔离方案**（关键）：uvicorn 跑在**独立 daemon 线程 + 独立 asyncio loop**，
``_GateHttpBridge`` 负责起停。共享对象（``HumanGateHandler`` / ``SessionContextRegistry`` /
``EventBus.tape``）均用 ``threading.Lock`` 保护跨线程安全（见各模块 docstring），
故 TUI 的 loop 与 HTTP server 的 loop 可安全并发访问。这避免「两个 asyncio loop 抢一个
线程」的崩溃，也避免 uvicorn 嵌入 Textual 的兼容性坑（uvicorn ``Server.serve`` 必须在
其 loop 内 await，跨 loop 调用会 raise）。loop 隔离 + 共享对象锁 = 干净的双线程拓扑。

退出语义：编排 worker 跑完 → ``terminal_state`` 存 ``RunState``（tape 派生）→ **TUI 停留**
（不再自动 ``self.exit()``，终态 notify 提示「按 q 退出」）→ 用户按 ``q`` → ``self.exit()`` →
``commands._run_workflow`` 据 ``terminal_state.status`` 决定 exit 0/1。中途 ``q`` 强退 →
``terminal_state`` 仍 None → exit 1（fail loud）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer

from orca.chart._limits import SOCK_PATH_MAX
from orca.events.bus import EventBus
from orca.events.chart_ingestor import chart_ingestor, make_crash_callback
from orca.events.tape import Tape
from orca.gates.context_registry import SessionContextRegistry
from orca.gates.handler import HumanGateHandler
from orca.gates.interrupt import InterruptHandler
from orca.gates.types import InterruptRequest
from orca.iface.cli.screens.chart_browser import ChartBrowser
from orca.iface.cli.screens.gate_modal import GateModal
from orca.iface.cli.screens.interrupt_modal import InterruptModal
from orca.iface.cli.widgets import DagGraph, Header, LogStream, NodeDetail
from orca.iface.cli.widgets.header import HeaderStats
from orca.run.lifecycle import gen_run_id
from orca.run.orchestrator import Orchestrator

if TYPE_CHECKING:
    from orca.schema import RunState, Workflow

logger = logging.getLogger(__name__)

# hook 桥 HTTP server 默认端口（claude 的 PreToolUse hook POST 到此端口）。
# 与 phase 6 ``hook_script.py`` 默认一致；环境变量 ``ORCA_PORT`` 可覆盖。
_DEFAULT_GATE_PORT = 7421

# push_screen_wait 返回的「别壳先答」哨兵前缀（见 GateModal.notify_resolved_externally）。
_BROADCAST_PREFIX = "__orca_broadcast__"


class _GateHttpBridge:
    """hook 桥 HTTP server 的线程 + loop 隔离封装（SPEC §3 §4）。

    为何单独一个类：把「起 uvicorn 线程 / 在该线程的 loop 里 schedule stop」的样板
    集中一处，让 OrcaApp 主体保持纯 Textual 逻辑。共享对象（handler/registry/bus）的
    跨线程安全由它们各自的 ``threading.Lock`` 保证，本类只负责线程 + loop 生命周期。

    生命周期：
      - ``start()``：起 daemon 线程 → 线程内 ``new_event_loop`` → 构建 FastAPI app +
        ``register_gate_routes`` → ``handler.start()``（在线程 loop 里）→ ``uvicorn.Server.serve``。
      - ``stop()``：从 TUI loop 调 → ``call_soon_threadsafe(server.should_exit=True)`` +
        ``handler.stop()``（schedule 到线程 loop）→ ``join(timeout)`` 干净退出。
    """

    def __init__(
        self,
        handler: HumanGateHandler,
        registry: SessionContextRegistry,
        port: int = _DEFAULT_GATE_PORT,
    ) -> None:
        self._handler = handler
        self._registry = registry
        self._port = port
        self._thread: threading.Thread | None = None
        self._loop = None
        self._server = None
        # ``start`` 完成 + uvicorn 监听就绪信号（让调用方知道 hook 桥已可 POST）。
        self._ready = threading.Event()
        self._start_error: BaseException | None = None

    def start(self) -> None:
        """起 daemon 线程跑 uvicorn。失败（端口占用 / 缺依赖）fail loud 记 error。"""
        if self._thread is not None:
            return  # 幂等
        self._ready.clear()
        self._start_error = None
        self._thread = threading.Thread(
            target=self._run_loop, name="orca-gate-http", daemon=True,
        )
        self._thread.start()
        # 等 uvicorn 起来（或失败）；超时 5s 兜底（极端慢机也不应卡死 TUI 启动）。
        if not self._ready.wait(timeout=5.0):
            logger.warning(
                "gate HTTP server 5s 内未就绪（port=%d）；hook 桥可能不可达 "
                "→ 安全语义会让 hook exit 2，workflow 仍能跑（agent_ask gate 不受影响）",
                self._port,
            )
        if self._start_error is not None:
            # uvicorn 未安装 / FastAPI 路由注册失败等：记 error 不崩 TUI（agent_ask 仍可用）。
            logger.error(
                "gate HTTP server 启动失败（port=%d）：%.200s。"
                "tool_permission gate 不可用；agent_ask gate 不受影响",
                self._port, self._start_error,
            )

    def _run_loop(self) -> None:
        """daemon 线程体：建 loop → 起 handler broadcaster → 起 uvicorn → 阻塞 serve。"""
        import asyncio

        loop = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop

            async def _serve() -> None:
                try:
                    # 同步预 bind socket：① 端口占用时**立即** deterministic 失败（不依赖
                    #    uvicorn 内部 startup 的异步时序，避免「ready 后才知道失败」的 race）；
                    #    ② 把已 bind 的 socket 喂给 uvicorn，消除「假就绪」窗口（startup 不再
                    #    需要自己 bind，直接 accept）。
                    import socket as _socket
                    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                    try:
                        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
                        sock.bind(("127.0.0.1", self._port))
                        sock.listen(128)
                    except OSError as e:
                        # 端口占用 / 无权限：fail loud 记 error，set ready 让 start 解除等待。
                        self._start_error = e
                        self._ready.set()
                        sock.close()
                        logger.error(
                            "gate HTTP server bind 失败（port=%d）：%s。"
                            "tool_permission gate 不可用", self._port, e,
                        )
                        return

                    await self._handler.start()
                    # 局部 import：FastAPI/uvicorn 仅在线程 loop 内需要，失败不阻断 TUI。
                    from fastapi import FastAPI
                    import uvicorn
                    from orca.gates.http_endpoint import register_gate_routes

                    app = FastAPI(title="orca-gate")
                    register_gate_routes(app, self._handler, self._registry)
                    config = uvicorn.Config(
                        app, host="127.0.0.1", port=self._port, log_level="warning",
                    )
                    server = uvicorn.Server(config)
                    self._server = server
                    # ready 信号：socket 已 bind + listen，uvicorn 即将 accept → 真正就绪。
                    self._ready.set()
                    # serve(sockets=[sock]) 让 uvicorn 复用我们已 bind 的 socket。
                    await server.serve(sockets=[sock])
                except BaseException as e:  # noqa: BLE001 —— 线程顶层兜底
                    self._start_error = e
                    self._ready.set()
                    logger.exception("gate HTTP server 线程异常")

            loop.run_until_complete(_serve())
        finally:
            # ``loop`` 可能未绑定（new_event_loop 抛异常的极端路径）—— 守卫之，避免 finally
            # 再抛 NameError 掩盖原始异常（reviewer 发现的鲁棒性 bug）。
            if loop is not None:
                try:
                    loop.close()
                except Exception:  # noqa: BLE001
                    pass

    def stop(self) -> None:
        """从 TUI loop 调：让 uvicorn 优雅退出 + stop handler broadcaster + join 线程。

        关键：``handler.stop()`` 是 async（投哨兵 + await task 退出），必须在 HTTP 线程的
        loop 里跑完，否则 ``_broadcaster`` task 会泄漏（loop close 时报「Task was destroyed
        but it is pending」）。故用 ``run_coroutine_threadsafe`` 把「stop uvicorn + stop
        handler」的完整 async 清理 schedule 到线程 loop 并阻塞等结果——干净退出无泄漏。

        幂等 + 无泄漏保证（消除 ``coroutine ... was never awaited``）：
          1. 启动失败路径（端口占用等 ``_start_error`` 非 None，或 ``_server`` 从未赋值）
             短路：没有 uvicorn / handler 要停，直接 join 线程，**不构造任何 coroutine**
             （否则在线程 finally 关 loop 的 TOCTOU 窗口里它会被 GC 当作未 await 泄漏）。
          2. 正常路径只在「loop 未关闭 + loop 仍 alive」时才 schedule 清理 coroutine；
             future.result 超时分支用 ``future.cancel()`` 让目标 loop（Task 的合法 owner）
             取消 Task，而非从本线程 ``coro.close()`` 一个已被 wrap 的 coroutine（会被
             asyncio 当作双重驱动，行为未定义）。RuntimeError 分支 coro 尚未被 wrap，
             ``coro.close()`` 安全且必要。
        """
        if self._thread is None or self._loop is None:
            return

        import asyncio as _asyncio

        loop = self._loop

        async def _graceful_shutdown() -> None:
            if self._server is not None:
                self._server.should_exit = True
                # 等 uvicorn serve() 返回（should_exit 触发它退出 accept 循环）
                # 不直接 await serve_task（无句柄）；should_exit 后 serve 会自行 return。
            try:
                await self._handler.stop()
            except Exception:  # noqa: BLE001 —— stop 失败不阻断线程退出
                logger.exception("HTTP bridge shutdown: handler.stop 异常")

        # 启动失败 / server 从未起来：没有 async 资源要清理。短路，避免在「线程正关 loop」
        # 的竞态窗口里构造注定无法 await 的 coroutine（occupied-port 路径的泄漏根因）。
        startup_failed = self._start_error is not None or self._server is None
        if not startup_failed and not loop.is_closed():
            coro = _graceful_shutdown()
            try:
                future = _asyncio.run_coroutine_threadsafe(coro, loop)
                future.result(timeout=5.0)  # 阻塞等 async 清理完成
            except RuntimeError:
                # loop 在 schedule 后、result 前被线程 finally 关闭：coroutine 可能未被
                # drive → 必须 close，否则 GC 报「coroutine was never awaited」。
                coro.close()
            except Exception:  # noqa: BLE001 —— future 超时 / cancel：兜底记 error
                # 超时分支：coro 已被 wrap 成 Task 在目标 loop 上跑，用 future.cancel 让
                # 合法 owner 取消它（不要从本线程 coro.close —— 双重驱动，行为未定义）。
                future.cancel()
                logger.exception("HTTP bridge graceful shutdown 超时或异常")

        # 无论上面走哪条分支，都 join 线程 + 清状态（幂等：重复 stop 第二次走最顶 return）。
        self._thread.join(timeout=5.0)
        self._thread = None
        self._loop = None
        self._server = None


class OrcaApp(App):
    """Orca CLI 主 TUI（SPEC §3.2 / §6.0）。

    用法（由 ``commands._run_workflow`` 调）::

        app = OrcaApp(wf=wf, inputs={...}, task="...", max_iter=100)
        app.run()                 # 阻塞至 TUI 退出
        state = app.terminal_state  # None = 未到终态 / RunState = 完成

    退出码由调用方据 ``terminal_state.status`` 决定（completed→0 / failed→1）。
    """

    CSS = """
    Screen {
        layout: vertical;
    }
    #main-row {
        height: 1fr;
    }
    /* phase-12 SPEC §3.2：左图窄（max 1/3），右列 NodeDetail 高于 LogStream（3fr/2fr）。 */
    #right-col {
        width: 1fr;
    }
    NodeDetail {
        height: 3fr;
    }
    LogStream {
        height: 2fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "退出"),
        Binding("g", "goto_gate", "跳到 gate"),
        # phase 11 §2.4 / §3.1：Ctrl+G 弹 InterruptModal（中断纠偏）。
        Binding("ctrl+g", "interrupt", "中断/纠偏"),
        # phase 11 §2.4 / §6.1：d 弹 DialogModal（agent 跑完后多轮追问）。
        Binding("d", "dialog", "对话"),
        # phase-12 SPEC §5：a 恢复 auto-follow；c 聚焦 NodeDetail 图表 tab；
        # C 全屏 ChartBrowser；/ LogStream 过滤；Tab focus_next（Textual 默认）。
        Binding("a", "follow_active", "跟随活跃"),
        Binding("c", "focus_charts", "图表 tab"),
        Binding("C", "open_chart_browser", "全屏图表"),
        Binding("slash", "filter_log", "过滤日志"),
    ]

    def __init__(
        self,
        wf: Workflow,
        inputs: dict | None = None,
        task: str | None = None,
        max_iter: int | None = None,
        *,
        gate_port: int | None = None,
        tape_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.wf = wf
        self._inputs = inputs or {}
        self._task = task
        self._max_iter = max_iter

        # 构造 app 自己的 bus + tape（不复用 run_workflow：那个内部构造会吞 bus，
        # TUI 需要在 orchestrator 跑前 subscribe，故此处显式构造）。run_id 由 orchestrator
        # 内部 gen（若调用方没传）；为 header 显示提前 gen 一份（与 orchestrator.run_id 同算法）。
        #
        # phase 11 §8 P3.2 daemon：detached child 经 ``ORCA_BG_RUN_ID`` 拿父进程生成的
        # run_id，复用它而非重新 gen —— 保证 ``~/.orca/runs/<run_id>.json`` metadata、
        # tape 文件名、OrcaApp.run_id 三者一致（确定性，``ps``/``logs``/``wait`` 据此定位）。
        from orca.iface.cli.bg_runner import ENV_BG_RUN_ID

        bg_run_id = os.environ.get(ENV_BG_RUN_ID)
        self.run_id = bg_run_id or gen_run_id(wf.name)
        # tape_path：默认 ``./runs/<run_id>.jsonl``（生产路径）；测试传 tmp_path 避免污染
        # CWD + 文件句柄泄漏。web 壳复用 OrcaApp 时也可注入自己的 run 目录。
        path = tape_path if tape_path is not None else Path("runs") / f"{self.run_id}.jsonl"
        tape = Tape(path, run_id=self.run_id)
        self.bus = EventBus(tape)
        self.gate_handler = HumanGateHandler(self.bus)
        # phase 11 §3：InterruptHandler（与 gate_handler 共享同一 bus/tape）。
        # 经 Orchestrator(interrupt_handler=...) 注入；None = 该 run 无中断支持。
        self.interrupt_handler = InterruptHandler(self.bus)
        # 当前 run 的 Orchestrator 句柄（_run_pipeline 内构造后回填）。
        # action_interrupt 经它调 request_interrupt；run 开始前为 None（无法中断）。
        self._orchestrator: Orchestrator | None = None
        # 当前在跑的 node 名 + session_id（action_interrupt 构造 InterruptRequest 用）。
        self._current_node: str | None = None
        self._current_session_id: str | None = None
        self._node_started_at: float | None = None
        self.session_registry = SessionContextRegistry()
        # phase 11 §5：AgentToolsMcpServer（ask_user 挂载，给被编排的 claude -p 调）。
        # runs_dir 与 tape 同目录（mcp-config 写到 runs/<run_id>/mcp_<session>.json）。
        # lazy-start（orchestrator.run 内，第一个 agent spawn 前）；None 路径 == 无 ask_user。
        from orca.exec.mcp_tools.server import AgentToolsMcpServer

        runs_dir = Path(path).parent if tape_path is not None else Path("runs")
        self.agent_tools_server = AgentToolsMcpServer(
            self.gate_handler, self.session_registry, runs_dir=runs_dir,
        )
        # phase-13 §3.1：per-run chart ingestor task（与 RunManager.start_run 对称）。
        # script 子进程（agent spawn 的 Bash 工具或直接 script node）经 env 拿 sock path
        # → ``orca.chart.render_chart`` 推图到此 ingestor → emit custom(chart) → tape。
        # ``_run_pipeline`` 启动时 create_task；finally cancel + unlink socket。
        # None == ingestor 未起（_run_pipeline 还没跑 / sock path 过长退化）。
        self._chart_ingestor_task: asyncio.Task | None = None
        self._chart_sock_path: Path = runs_dir / f"{self.run_id}.sock"
        self._runs_dir = runs_dir

        # gate HTTP 桥（hook POST 入口）。gate_port=None → 读 ORCA_PORT env / 默认 7421。
        port = gate_port if gate_port is not None else _gate_port_from_env()
        self._http_bridge = _GateHttpBridge(self.gate_handler, self.session_registry, port=port)
        # kickoff 是否已调用（on_mount 不调编排 worker，kickoff 才调；幂等保护）。
        self._kicked_off: bool = False

        # 终态：orchestrator 跑完后存 RunState（commands 据此决定 exit code）。
        self.terminal_state: RunState | None = None
        # 当前在途的 GateModal（None=无 gate 在等；非空=modal 还在屏上）。
        # 这是 UI 交互态（SPEC §4.3「临时 UI 交互态」），不是业务真相——真相在 tape。
        self._active_modal: GateModal | None = None
        # 事件订阅句柄（on_mount 里 subscribe，stop 时 cancel）。
        self._sub = None
        # node 名集合（build DagTree 用）+ parallel 组（name, branches）。
        self._node_names: list[str] = [n.name for n in wf.nodes]
        self._parallel_groups: list[tuple[str, list[str]]] = [
            (g.name, list(g.branches)) for g in wf.parallel
        ]
        # gate 计数（requested 未 resolved 的）：header awaiting 显示。
        self._awaiting_gates: set[str] = set()
        # 已完成 node 数（header done 显示）。
        self._done_nodes: int = 0
        self._total_nodes: int = len(wf.nodes)
        # phase 11 §6.1：最近一个完成的 agent node 名 + 其 output（action_dialog 按 d 弹
        # DialogModal 用）。只有 agent node 的 output 值得追问（script/set 是确定性输出）。
        # node_completed 事件 data 含 {output, elapsed}，此处记 (node, output)。
        # 多个 agent 完成时取最后一个（用户按 d 时最关心的往往是刚跑完的那个）。
        self._last_completed_agent_node: str | None = None
        self._last_completed_agent_output: Any = None
        # agent node 名集合（判定 node_completed 时是否记 agent output）。
        self._agent_node_names: set[str] = {
            n.name for n in wf.nodes if getattr(n, "kind", None) == "agent"
        }
        # phase-12 SPEC §3.1：node 名 → kind（agent/script/set/foreach/wait/terminate）。
        # 静态从 wf.nodes 派生（不读 node_started.data.kind —— foreach 无顶层该事件，
        # run/foreach.py:73）。经 NodeDetail.set_node(name, kind) 透传，驱动 6 kind 派发。
        self._node_kinds: dict[str, str] = {
            n.name: n.kind for n in wf.nodes if n.name
        }
        # phase-12 SPEC §1.4：临时 UI 交互态（不写 tape、不算业务真相，与 _active_modal 同类）。
        # _auto_follow 默认 True（node_started 自动跟随 running 节点）；j/k 或点选 → False（pin）；
        # a → 恢复 True。_selected_node 驱动 NodeDetail 全部内容。
        self._selected_node: str | None = None
        self._auto_follow: bool = True

    # ── Textual 钩子 ─────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-row"):
            yield DagGraph()                       # phase-12：左图拓扑（窄，max 1/3）
            with Vertical(id="right-col"):
                yield NodeDetail()                 # 右上：tab 化详情（3fr）
                yield LogStream()                  # 右下：日志（2fr）
        yield Footer()

    def on_mount(self) -> None:
        """初始化 widget（build DagTree / header / active node）+ 起事件消费 worker
        + kickoff 编排主流程。

        on_mount 在 Textual event loop 已 running 时被调，故此处 spawn ``@work`` worker
        安全（与 ``_consume_events`` 同 pattern）。**不**在 ``commands._run_workflow``
        里调 kickoff：那是 ``app.run()`` **之前**，loop 尚未起，``@work`` decorator
        试图 ``asyncio.create_task`` 时 ``events.get_running_loop()`` 抛 RuntimeError
        （真实 ``orca run`` 撞过的 bug）。

        单测用 ``run_test()`` 时 on_mount 同样会触发；如不希望真起编排（避免 spawn
        claude / uvicorn），可把 ``kickoff`` 替换成 no-op（见 ``test_app._patched_app``）。
        """
        tree = self.query_one(DagGraph)
        # routes 派生：{node_or_group: [target, ...]}（含 $end，build_from_workflow 内忽略）。
        # 顶层 node 的 routes + parallel 组的 routes（组完成后路由）。
        routes: dict[str, list[str]] = {}
        for n in self.wf.nodes:
            if n.name:
                routes[n.name] = [r.to for r in n.routes]
        for g in self.wf.parallel:
            routes[g.name] = [r.to for r in g.routes]
        tree.build_from_workflow(self._node_names, self._parallel_groups, routes)
        header = self.query_one(Header)
        header.update_stats(HeaderStats(
            run_id=self.run_id, workflow_name=self.wf.name, total=self._total_nodes,
        ))
        # NodeDetail 初始选中 entry（auto-follow 默认 True，首个 node_started 会覆盖）。
        active = self.query_one(NodeDetail)
        active.set_node(self.wf.entry, self._node_kinds.get(self.wf.entry))
        self._selected_node = self.wf.entry

        # 订阅事件 + 起事件消费 worker（@work，loop 已 running，安全）。
        self._sub = self.bus.subscribe()
        self._consume_events()
        # kickoff 编排主流程 + HTTP 桥（@work，loop 已 running）。kickoff 幂等，测试可
        # 替换为 no-op 跳过真起编排。
        self.kickoff()

    def kickoff(self) -> None:
        """启动编排主流程 + gate HTTP 桥（on_mount 自动调，单测可替换为 no-op）。

        幂等：重复调直接 return（on_mount 已调时，外部再调 no-op）。
        必须在 Textual event loop running 时调（``@work`` decorator 依赖 loop）。
        """
        if self._kicked_off:
            return  # 幂等
        self._kicked_off = True
        # 起 gate HTTP 桥（hook POST 入口）。失败已 fail loud 记 error，TUI 继续。
        self._http_bridge.start()
        # 起编排 worker（@work，在 app 的 loop 里）
        self._run_pipeline()

    def on_unmount(self) -> None:
        """退出时清理：关 gate HTTP 桥 + 关 bus。"""
        self._http_bridge.stop()
        # bus.close 投递 None 哨兵让 _consume_events 退出；TUI 退出时 orchestrator 也应已完。
        try:
            self.bus.close()
        except Exception:  # noqa: BLE001 —— 关 idempotent，重复关忽略
            pass

    # ── worker 1：编排主流程 ─────────────────────────────────────────────

    @work(name="orca-pipeline")
    async def _run_pipeline(self) -> None:
        """跑 Orchestrator.run()（顺序代码）。终态存 ``terminal_state``。"""
        # handler broadcaster 必须先起（在 TUI loop 里起，与 _consume_events 同 loop）。
        # _http_bridge 线程里有自己的 loop + 自己的 broadcaster（线程隔离）—— 两边都 start
        # 是幂等的；TUI loop 这边的 broadcaster 负责把 resolved 事件 emit 给 TUI 订阅者。
        # phase 11：interrupt_handler 也起 broadcaster（同 loop，resolved 事件经它广播）。
        await self.gate_handler.start()
        await self.interrupt_handler.start()
        # phase-13 §3.1：起 per-run chart ingestor（与 RunManager.start_run 对称）。
        # sock path 长度过长（macOS SOCK_PATH_MAX=90）→ log warning + 不起 ingestor
        # （script 端 render_chart 会 fail loud 提示）。不 raise（避免阻塞整个 run）。
        try:
            resolved_sock = str(self._chart_sock_path.resolve())
            if len(resolved_sock) > SOCK_PATH_MAX:
                logger.warning(
                    "phase-13: chart sock path 过长（%d > %d 字节）：%r；"
                    "TUI 不起 ingestor（script 端 render_chart 会 fail loud）。"
                    "改 tape_path 到短路径（如 /tmp/orca-<run_id>/tape.jsonl）。",
                    len(resolved_sock), SOCK_PATH_MAX, resolved_sock,
                )
            else:
                self._chart_ingestor_task = asyncio.create_task(
                    chart_ingestor(self._chart_sock_path, self.bus, self.run_id),
                    name=f"orca-chart-ingestor-{self.run_id}",
                )
                self._chart_ingestor_task.add_done_callback(
                    make_crash_callback(self._chart_sock_path, self.bus, self.run_id)
                )
        except Exception:  # noqa: BLE001 —— ingestor 启动失败不应阻塞编排
            logger.exception("chart_ingestor 启动失败（不影响编排主流程）")
        try:
            orch = Orchestrator(
                self.wf, self.bus, self._inputs,
                task=self._task, max_iter=self._max_iter, run_id=self.run_id,
                interrupt_handler=self.interrupt_handler,
                agent_tools_server=self.agent_tools_server,
            )
            self._orchestrator = orch
            state = await orch.run()
            self.terminal_state = state
        except Exception:  # noqa: BLE001 —— 编排顶层兜底
            logger.exception("Orchestrator 运行异常")
            # terminal_state 留 None（commands 据 None → exit 1，fail loud）
        finally:
            self._orchestrator = None
            # phase-13 §3.1：先 cancel chart ingestor（防 in-flight chart 写已 close 的 tape）。
            if self._chart_ingestor_task is not None and not self._chart_ingestor_task.done():
                self._chart_ingestor_task.cancel()
                try:
                    await self._chart_ingestor_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    logger.warning("chart_ingestor 异常退出", exc_info=True)
            try:
                self._chart_sock_path.unlink(missing_ok=True)
            except OSError as e:  # noqa: BLE001
                logger.warning("sock unlink 失败 %s: %r", self._chart_sock_path, e)
            # orchestrator.run() 已 close bus；这里只确认 broadcaster 停（无 in-flight gate/interrupt）。
            try:
                await self.gate_handler.stop()
            except Exception:  # noqa: BLE001
                logger.exception("gate_handler.stop 异常")
            try:
                await self.interrupt_handler.stop()
            except Exception:  # noqa: BLE001
                logger.exception("interrupt_handler.stop 异常")
            # 不主动 self.exit()：TUI 终态后停留，让用户看结果 / 错误后再按 q 退出
            # （commands._run_workflow 在 tui.run() 返回后读 terminal_state 决定 exit code）。
            # 终态提示由 _dispatch_to_widgets 的 workflow_completed/failed 分支 notify。
            # 中途 q 强退：terminal_state 仍 None → exit 1（既有逻辑，保持）。

    # ── worker 2：事件消费（SPEC §3.2 _consume_events）──────────────────

    @work(name="orca-events")
    async def _consume_events(self) -> None:
        """订阅 bus 事件流 → 分发到 widget / 触发 GateModal。"""
        if self._sub is None:
            return
        async for event in self._sub.events():
            try:
                self._dispatch_to_widgets(event)
            except Exception:  # noqa: BLE001 —— 单事件 dispatch 错不中断消费
                logger.exception("dispatch event 失败 type=%s seq=%d", event.type, event.seq)

    def _dispatch_to_widgets(self, event) -> None:
        """把单个 event 分发到对应 widget（SPEC §6.0 铁律 1：纯派生）。"""
        etype = event.type
        data = event.data or {}
        node = event.node

        # node 生命周期 → DagGraph 图标 + NodeDetail 切换
        if etype == "node_started":
            graph = self.query_one(DagGraph)
            graph.set_status(node or "", "running")
            if node:
                # phase-12 SPEC §1.4：auto-follow（默认 True）→ _selected_node 跟随 running。
                # pin 后（_auto_follow=False）node_started 不覆盖选中。
                if self._auto_follow:
                    self._selected_node = node
                    self.query_one(NodeDetail).set_node(
                        node, self._node_kinds.get(node),
                    )
                # node_started 也入流式 tab（terminate kind 的主源，SPEC §1.3 表）。
                self.query_one(NodeDetail).append_event_stream(node, etype, data)
                # phase 11 §3：追踪当前 node + 起始时间 + session（action_interrupt 用）。
                import time as _time
                self._current_node = node
                self._node_started_at = _time.monotonic()
                self._current_session_id = event.session_id
        elif etype == "node_completed":
            graph = self.query_one(DagGraph)
            graph.set_status(node or "", "done")
            self._done_nodes += 1
            self._refresh_header()
            if node:
                # phase-12 SPEC §1.3：输出 tab 显 node_completed.data.output（6 kind 通用）。
                self.query_one(NodeDetail).set_output(node, data.get("output"))
            # phase 11 §6.1：记最近完成的 agent node + output（action_dialog 按 d 用）。
            if node and node in self._agent_node_names:
                self._last_completed_agent_node = node
                self._last_completed_agent_output = data.get("output")
        elif etype == "node_failed":
            self.query_one(DagGraph).set_status(node or "", "failed")
        elif etype == "route_taken":
            pass  # 日志里看即可，DAG 图标由 node_* 事件驱动

        # gate 事件（SPEC §4.5 决策 5：双重身份）
        elif etype == "human_decision_requested":
            gate_id = data.get("gate_id", "")
            self._awaiting_gates.add(gate_id)
            self._refresh_header()
            # 当前节点 → blocked 图标（gate 拦在 node 内的 claude 工具调用循环）
            if node:
                self.query_one(DagGraph).set_status(node, "blocked")
            # 推 GateModal 参与竞速（@work 内 push_screen_wait）
            self._push_gate_modal(event)
        elif etype == "human_decision_resolved":
            gate_id = data.get("gate_id", "")
            self._awaiting_gates.discard(gate_id)
            self._refresh_header()
            # node 解除 blocked → 回 running（claude resume 继续跑）
            if node:
                self.query_one(DagGraph).set_status(node, "running")
            # 广播输家：本壳 modal 还在 → notify_resolved_externally 让它 dismiss
            if self._active_modal is not None:
                self._active_modal.notify_resolved_externally(
                    source=str(data.get("resolved_by", "?")),
                    answer=str(data.get("answer", "")),
                )

        # agent 流式 → NodeDetail 流式 tab + LogStream（executor-agnostic：N 事件→N 行）
        elif etype in ("agent_message", "agent_thinking", "agent_tool_call", "agent_tool_result"):
            log = self.query_one(LogStream)
            log.append_event(
                etype, data, node=node,
                session_id=event.session_id, timestamp=event.timestamp,
            )
            if node:
                self.query_one(NodeDetail).append_event_stream(node, etype, data)

        # foreach / wait 流式事件 → NodeDetail 流式 tab（SPEC §1.3 表）
        elif etype in (
            "foreach_started", "foreach_completed",
            "foreach_item_started", "foreach_item_completed",
            "wait_started", "wait_completed",
        ):
            log = self.query_one(LogStream)
            log.append_event(
                etype, data, node=node,
                session_id=event.session_id, timestamp=event.timestamp,
            )
            if node:
                self.query_one(NodeDetail).append_event_stream(node, etype, data)

        # phase-12 SPEC §3.3：custom(kind=chart) → NodeDetail 图表 tab（确定性 fold 落点）。
        elif etype == "custom" and data.get("kind") == "chart":
            payload = data.get("chart")
            if not isinstance(payload, dict):
                # 防御：非 dict 静默跳过 + warning（SPEC §6.4 同语义）。
                logger.warning("custom(chart) payload 非 dict，跳过: %r", type(payload).__name__)
                return
            # node=None → __workflow__ 桶（SPEC §3.3 D2-a；workflow 级图归此，ChartBrowser 顶层）。
            node_key = node if node is not None else "__workflow__"
            self.query_one(NodeDetail).upsert_chart(node_key, payload)

        # workflow 终态
        elif etype == "workflow_started":
            log = self.query_one(LogStream)
            log.append_event(etype, data, node=node, session_id=event.session_id,
                             timestamp=event.timestamp)
        elif etype in ("workflow_completed", "workflow_failed"):
            log = self.query_one(LogStream)
            log.append_event(etype, data, node=node, session_id=event.session_id,
                             timestamp=event.timestamp)
            # TUI 不再自动退出（_run_orchestrator finally 不调 self.exit），终态后停留。
            # 显式 notify 让用户知道已到终态 + 如何离开（timeout=0 持久不消失）。
            if etype == "workflow_failed":
                self.notify(
                    "workflow failed — 见日志详情，按 q 退出",
                    severity="error", timeout=0,
                )
            else:
                self.notify(
                    "workflow completed — 按 q 退出",
                    severity="information", timeout=0,
                )

        # 所有事件都入 LogStream（保证「发生了什么」可见；SPEC §4.3 全部入日志）
        else:
            log = self.query_one(LogStream)
            log.append_event(etype, data, node=node, session_id=event.session_id,
                             timestamp=event.timestamp)

    # ── gate ModalScreen（SPEC §3.2 / §4.5）─────────────────────────────

    @work(name="orca-gate-push")
    async def _push_gate_modal(self, request_event) -> None:
        """收到 ``human_decision_requested`` → 推 GateModal 等用户答 → resolve。

        这是独立的 @work worker：``push_screen_wait`` 阻塞本 worker 但 UI 事件循环
        继续刷新（_dispatch_to_widgets / _consume_events 不受影响，DAG/日志继续滚）。
        SPEC §6.0 铁律 3 + §1 决策 1 的核心验证点。
        """
        from orca.gates.types import HumanGate

        data = request_event.data or {}
        gate = HumanGate(
            id=data.get("gate_id", ""),
            prompt=str(data.get("prompt", "")),
            options=data.get("options"),
            context=data.get("context", {}) or {},
            source=data.get("source", "tool_permission"),  # type: ignore[arg-type]
            run_id=data.get("run_id", self.run_id),
            node=request_event.node,
            session_id=request_event.session_id,
        )
        modal = GateModal(gate)
        self._active_modal = modal
        try:
            result = await self.push_screen_wait(modal)
        finally:
            self._active_modal = None

        # 处理用户答案
        if isinstance(result, str) and result.startswith(_BROADCAST_PREFIX):
            # 别壳先答（广播输家）：赢家已 resolve，本壳不重复 resolve
            logger.info("gate %s 被别壳先答（%s）", gate.id, result)
            return
        # 本壳赢家：调 handler.resolve（SPEC §6.0 铁律 2）
        ok = self.gate_handler.resolve(gate.id, str(result), "cli")
        if not ok:
            # 极端 race：本壳用户答了，但 resolve 返回 False（已被别壳答）—— 记 warning
            logger.warning(
                "gate %s CLI 答案 %r 被 reject（已被别壳答？），fail loud",
                gate.id, result,
            )

    # ── 快捷键 ──────────────────────────────────────────────────────────

    def action_goto_gate(self) -> None:
        """g 键：把焦点切到当前 gate modal（若在屏）；否则提示无 gate。"""
        if self._active_modal is not None:
            # modal 已在最上层，给用户视觉提示
            self.query_one(LogStream).write("(gate 已在屏上，用方向键 + 回车答)")
        else:
            self.query_one(LogStream).write("(当前无 gate 在等)")

    @work(name="orca-interrupt-push")
    async def action_interrupt(self) -> None:
        """Ctrl+G：弹 InterruptModal 等用户答 → 登记 pending + resolve。

        SPEC §3.1 / §2.3。@work worker：``push_screen_wait`` 阻塞本 worker 但 UI 事件循环
        继续刷新（与 _push_gate_modal 同 pattern，SPEC §6.0 铁律 3）。

        流程：
          1. 构造 InterruptRequest（当前 node + 已耗时）。
          2. push InterruptModal → dismiss 返回 ``(action, guidance)``。
          3. ``orchestrator.request_interrupt(ireq)`` 登记 pending（node 边界消费）。
          4. ``interrupt_handler.resolve(ireq.id, action, guidance, "cli")`` 喂答案。
        """
        import time
        import uuid

        orch = self._orchestrator
        if orch is None:
            # run 尚未开始 / 已结束 → 无可中断目标
            self.query_one(LogStream).write("(无可中断的 run：编排未在跑)")
            return

        node = self._current_node or self.wf.entry
        elapsed = (
            time.monotonic() - self._node_started_at
            if self._node_started_at is not None
            else 0.0
        )
        ireq = InterruptRequest(
            id=uuid.uuid4().hex,
            node=node,
            run_id=self.run_id,
            session_id=self._current_session_id,
            elapsed_at_request=elapsed,
            source="cli",
        )
        modal = InterruptModal(ireq)
        action, guidance = await self.push_screen_wait(modal)

        # phase 11 §9 P4：SKIP 时弹 NodeSelectModal 让用户选目标 node。
        # - 用户选具体 node → skip_target = 该 node 名（直接跳，不经 route 求值）。
        # - 用户选「route-default」/ Esc → skip_target = None（走兜底 route / 默认下一 node）。
        # 放在 request_interrupt 之前：把目标随中断请求一起登记，node 边界 _handle_interrupt
        # 一次性消费（action + guidance + skip_target）。
        skip_target: str | None = None
        if action == "skip":
            from orca.iface.cli.screens.node_select_modal import NodeSelectModal

            # 候选 = workflow 全部 node 名 + parallel 组名（排除当前 node，modal 内再滤一次）。
            candidates = [n.name for n in self.wf.nodes]
            candidates.extend(g.name for g in self.wf.parallel)
            select_modal = NodeSelectModal(current_node=node, candidate_nodes=candidates)
            skip_target = await self.push_screen_wait(select_modal)

        # CLI 单壳路径（SPEC §3.1 时序）：用户已在 modal 答完，把 (action, guidance) 随
        # request_interrupt 一起带入。orchestrator 在 node 边界 _handle_interrupt 直接消费它
        # （record_resolved emit requested + 入队 resolved 写 Tape），**不**调 handler.resolve
        # ——resolve 是多壳竞速路径（await-future），CLI 单壳不需要且时序不匹配（review §2.1）。
        orch.request_interrupt(
            ireq, answer=(action, guidance), skip_target=skip_target,
        )

    # ── phase 11 §6：Dialog（agent 跑完后多轮追问）──────────────────────

    def action_dialog(self) -> None:
        """d 键：找最近完成的 agent node → 弹 DialogModal 多轮追问（SPEC §6.1）。

        非阻塞（push_screen 非 wait）：dialog 不影响 DAG 推进（SPEC §6.3：post-completion 模式）。
        workflow 通常已跑完或跑到下游时用户才按 d，故 dialog 自然 post-run。

        无完成的 agent node → 写 hint 到 LogStream 提示（不弹 modal）。
        DialogHandler 懒构造（首次按 d 时建，profile 从 get_profile("claude") 拿，bus 复用 app 的）。
        """
        node = self._last_completed_agent_node
        output = self._last_completed_agent_output
        if node is None:
            # 无完成的 agent node（workflow 全 script / 尚未跑到 agent / agent 失败）
            self.query_one(LogStream).write(
                "(无可追问的 agent node：尚无 agent 完成产出)",
            )
            return

        from orca.exec.context import RunContext
        from orca.gates.dialog import DialogHandler
        from orca.iface.cli.screens.dialog_modal import DialogModal
        from orca.profiles import get_profile

        # DialogHandler 懒构造（profile + bus）。每次按 d 新建一个 handler（dialog 间状态隔离，
        # 不复用——同一次 run 多次 dialog 各自独立）。get_profile("claude") 与 agent spawn 同源。
        handler = DialogHandler(get_profile("claude"), self.bus)
        # 最小 ctx：dialog handler 仅用 run_id（spawn env overlay 路由用）。outputs/inputs 不参与
        # dialog 逻辑（agent_output 已显式传入），故空 dict 足够。
        ctx = RunContext(
            inputs=self._inputs, outputs={}, run_id=self.run_id, task=self._task,
        )
        self.push_screen(DialogModal(handler, node, output, ctx, bus=self.bus))

    # ── phase-12 §1.4 / §5：选中 + auto-follow + 图表键位 ────────────────────

    def _on_node_selected(self, name: str) -> None:
        """DagGraph 选中（j/k / click）回调（SPEC §1.4）。

        pin：``_auto_follow=False``；``_selected_node=name``；NodeDetail 切到该节点。
        后续 ``node_started`` 不再覆盖选中（除非用户按 ``a`` 恢复跟随）。
        """
        self._selected_node = name
        self._auto_follow = False
        self.query_one(NodeDetail).set_node(name, self._node_kinds.get(name))

    def action_follow_active(self) -> None:
        """``a`` 键：恢复 auto-follow（SPEC §1.4 / §5）。

        ``_auto_follow=True``；``_selected_node=当前 running 节点``（无 running 则不变）。
        """
        self._auto_follow = True
        running = self._current_node
        if running is not None:
            self._selected_node = running
            self.query_one(NodeDetail).set_node(
                running, self._node_kinds.get(running),
            )

    def action_focus_charts(self) -> None:
        """``c`` 键：聚焦 NodeDetail + 切图表 tab（SPEC §5）。"""
        nd = self.query_one(NodeDetail)
        self.set_focus(nd)
        nd.action_focus_charts()

    def action_open_chart_browser(self) -> None:
        """``C`` 键：全屏 ChartBrowser（SPEC §4.5 / §5）。Esc/q 退。"""
        self.push_screen(ChartBrowser())

    def action_filter_log(self) -> None:
        """``/`` 键：LogStream 过滤（SPEC §4.4 / §5）。

        简化实现：写提示到 LogStream（输入框 modal 留后续；本 phase 只接键位 + 提示，
        不阻塞 DAG 推进）。聚焦 LogStream 供 j/k 滚动。
        """
        log = self.query_one(LogStream)
        self.set_focus(log)
        log.write("(/ 过滤：输入后回车；当前实现为占位，过滤逻辑留后续)")

    # ── header 刷新（SPEC §4.4）─────────────────────────────────────────

    def _refresh_header(self) -> None:
        """重算 header stats（done / awaiting）。"""
        self.query_one(Header).update_stats(HeaderStats(
            run_id=self.run_id,
            workflow_name=self.wf.name,
            done=self._done_nodes,
            total=self._total_nodes,
            awaiting_gate=len(self._awaiting_gates),
        ))


def _gate_port_from_env() -> int:
    """读 ``ORCA_PORT`` env（hook 桥端口，默认 7421，phase 6 hook_script 一致）。"""
    raw = os.environ.get("ORCA_PORT", str(_DEFAULT_GATE_PORT))
    try:
        port = int(raw)
    except ValueError:
        logger.warning("ORCA_PORT=%r 非法，回退默认 %d", raw, _DEFAULT_GATE_PORT)
        return _DEFAULT_GATE_PORT
    if not (1 <= port <= 65535):
        logger.warning("ORCA_PORT=%d 越界，回退默认 %d", port, _DEFAULT_GATE_PORT)
        return _DEFAULT_GATE_PORT
    return port
