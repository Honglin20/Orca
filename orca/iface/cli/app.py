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

退出语义：编排 worker 跑完 → ``terminal_state`` 存 ``RunState``（tape 派生）→
``self.exit()`` → ``commands._run_workflow`` 据 ``terminal_state.status`` 决定 exit 0/1。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.gates.context_registry import SessionContextRegistry
from orca.gates.handler import HumanGateHandler
from orca.gates.interrupt import InterruptHandler
from orca.gates.types import InterruptRequest
from orca.iface.cli.screens.gate_modal import GateModal
from orca.iface.cli.screens.interrupt_modal import InterruptModal
from orca.iface.cli.widgets import ActiveNode, DagTree, Header, LogStream
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
    """

    BINDINGS = [
        Binding("q", "quit", "退出"),
        Binding("g", "goto_gate", "跳到 gate"),
        # phase 11 §2.4 / §3.1：Ctrl+G 弹 InterruptModal（中断纠偏）。
        Binding("ctrl+g", "interrupt", "中断/纠偏"),
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
        self.run_id = gen_run_id(wf.name)
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

    # ── Textual 钩子 ─────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-row"):
            yield DagTree()
            with Vertical():
                yield ActiveNode()
                yield LogStream()
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
        tree = self.query_one(DagTree)
        tree.build_from_workflow(self._node_names, self._parallel_groups)
        header = self.query_one(Header)
        header.update_stats(HeaderStats(
            run_id=self.run_id, workflow_name=self.wf.name, total=self._total_nodes,
        ))
        active = self.query_one(ActiveNode)
        active.set_active(self.wf.entry)

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
        try:
            orch = Orchestrator(
                self.wf, self.bus, self._inputs,
                task=self._task, max_iter=self._max_iter, run_id=self.run_id,
                interrupt_handler=self.interrupt_handler,
            )
            self._orchestrator = orch
            state = await orch.run()
            self.terminal_state = state
        except Exception:  # noqa: BLE001 —— 编排顶层兜底
            logger.exception("Orchestrator 运行异常")
            # terminal_state 留 None（commands 据 None → exit 1，fail loud）
        finally:
            self._orchestrator = None
            # orchestrator.run() 已 close bus；这里只确认 broadcaster 停（无 in-flight gate/interrupt）。
            try:
                await self.gate_handler.stop()
            except Exception:  # noqa: BLE001
                logger.exception("gate_handler.stop 异常")
            try:
                await self.interrupt_handler.stop()
            except Exception:  # noqa: BLE001
                logger.exception("interrupt_handler.stop 异常")
            # 触发退出（让 _run_workflow.run() 返回）。若用户已在 q 退出则 no-op。
            try:
                self.exit()
            except Exception:  # noqa: BLE001 —— TUI 已退出时 self.exit 可能 raise
                pass

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

        # node 生命周期 → DagTree 图标
        if etype == "node_started":
            tree = self.query_one(DagTree)
            tree.set_status(node or "", "running")
            if node:
                self.query_one(ActiveNode).set_active(node)
                # phase 11 §3：追踪当前 node + 起始时间 + session（action_interrupt 用）。
                import time as _time
                self._current_node = node
                self._node_started_at = _time.monotonic()
                self._current_session_id = event.session_id
        elif etype == "node_completed":
            tree = self.query_one(DagTree)
            tree.set_status(node or "", "done")
            self._done_nodes += 1
            self._refresh_header()
        elif etype == "node_failed":
            self.query_one(DagTree).set_status(node or "", "failed")
        elif etype == "route_taken":
            pass  # 日志里看即可，DAG 图标由 node_* 事件驱动

        # gate 事件（SPEC §4.5 决策 5：双重身份）
        elif etype == "human_decision_requested":
            gate_id = data.get("gate_id", "")
            self._awaiting_gates.add(gate_id)
            self._refresh_header()
            # 当前节点 → blocked 图标（gate 拦在 node 内的 claude 工具调用循环）
            if node:
                self.query_one(DagTree).set_status(node, "blocked")
            # 推 GateModal 参与竞速（@work 内 push_screen_wait）
            self._push_gate_modal(event)
        elif etype == "human_decision_resolved":
            gate_id = data.get("gate_id", "")
            self._awaiting_gates.discard(gate_id)
            self._refresh_header()
            # node 解除 blocked → 回 running（claude resume 继续跑）
            if node:
                self.query_one(DagTree).set_status(node, "running")
            # 广播输家：本壳 modal 还在 → notify_resolved_externally 让它 dismiss
            if self._active_modal is not None:
                self._active_modal.notify_resolved_externally(
                    source=str(data.get("resolved_by", "?")),
                    answer=str(data.get("answer", "")),
                )

        # agent 流式 → ActiveNode + LogStream
        elif etype in ("agent_message", "agent_thinking", "agent_tool_call", "agent_tool_result"):
            log = self.query_one(LogStream)
            log.append_event(
                etype, data, node=node,
                session_id=event.session_id, timestamp=event.timestamp,
            )

        # workflow 终态
        elif etype == "workflow_started":
            log = self.query_one(LogStream)
            log.append_event(etype, data, node=node, session_id=event.session_id,
                             timestamp=event.timestamp)
        elif etype in ("workflow_completed", "workflow_failed"):
            log = self.query_one(LogStream)
            log.append_event(etype, data, node=node, session_id=event.session_id,
                             timestamp=event.timestamp)

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

        # 登记 pending（orchestrator 在 node 边界消费）+ resolve（喂给 handler，
        # 编排 _handle_interrupt await 的 future 立即解除阻塞）。
        orch.request_interrupt(ireq)
        ok = self.interrupt_handler.resolve(ireq.id, action, guidance, "cli")
        if not ok:
            logger.warning(
                "interrupt %s CLI 答案 (%s) 被 reject（已被别壳答？），fail loud",
                ireq.id, action,
            )

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
    import os

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
