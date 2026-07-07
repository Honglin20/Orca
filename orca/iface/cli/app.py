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
from orca.iface.cli.widgets import AgentsList, AgentHistory, Header, LogStream, NodeDetail
# spec v2 §3 + §11.5 #6：dispatch 走 ``EVENT_LEVEL.get()`` 三态派发（合法 level /
# None-as-explicit-skip / 表未登记）。Step 4 LogStream widget 已内置此表，本模块
# dispatch 显式复用同一份常量（避免双真相源）—— LEVEL_INFO 兜底 + LEVEL_DEBUG toggle。
from orca.iface.cli.widgets.log_stream import (
    EVENTS_NOT_IN_LOG_STREAM,
    EVENT_LEVEL,
    LEVEL_DEBUG,
    LEVEL_INFO,
)
from orca.iface.cli.widgets.header import HeaderStats, NodeUsageStats
from orca.run import projections
from orca.run.lifecycle import gen_run_id
from orca.run.orchestrator import Orchestrator

if TYPE_CHECKING:
    from orca.schema import Event, RunState, Workflow

logger = logging.getLogger(__name__)

# hook 桥 HTTP server 默认端口（claude 的 PreToolUse hook POST 到此端口）。
# 与 phase 6 ``hook_script.py`` 默认一致；环境变量 ``ORCA_PORT`` 可覆盖。
_DEFAULT_GATE_PORT = 7421

# push_screen_wait 返回的「别壳先答」哨兵前缀（见 GateModal.notify_resolved_externally）。
_BROADCAST_PREFIX = "__orca_broadcast__"

# spec v2 §9 R3 + §3：``_node_events`` 桶 FIFO 上限（防长跑 workflow 内存爆）。
# 桶是 reducer fold 派生（重放必产相同列表），丢最旧不丢真相（tape 是真相源）。
# AgentHistory 头部行显示 ``⚠ truncated`` 标记（见 ``_TRUNCATED_THRESHOLD``）。
_NODE_EVENTS_CAP = 1000


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
    /* spec v2 §2.1：三块布局（左 30% AgentsList / 右上 70% AgentHistory / 右下 30% LogStream）。
       NodeDetail 保留实例（display:none，chart 路径唯一入口，spec §6.3）。 */
    #main-row {
        height: 1fr;
    }
    AgentsList {
        width: 30%;
        min-width: 20;
        max-width: 40;
        height: 1fr;  /* 填满 #main-row 横向容器的高度（否则 auto 只够内容行，多 node 截断）*/
        border: round $primary;
        padding: 0 1;
        background: $surface;
    }
    #right-pane {
        width: 1fr;
        height: 1fr;
    }
    AgentHistory {
        height: 7fr;
        border: round $success;
        padding: 0 1;
        background: $surface;
    }
    LogStream {
        height: 3fr;
        border: round $warning;
        padding: 0 1;
        background: $surface;
    }
    /* spec v1.1 §7.2 / v2 §6.3：NodeDetail 保留实例（display:none，chart 路径）。
       **必须**用 ``display: none`` 而非 ``height:0 + offset``：``offset`` 只是视觉位移
       （≈ CSS position:relative），widget **仍占布局宽**——在 ``#main-row``（Horizontal）
       里与 ``#right-pane`` 抢横向空间，把右侧栏挤到 width≈1（AgentHistory/LogStream 全黑）。
       ``display: none`` 才真移出布局流；widget 仍 mounted + queryable（既有 ``c`` 键 /
       ChartBrowser / ``nd.active_tab`` 断言照常，spec §6.3）。 */
    NodeDetail {
        display: none;
        height: 0;
        min-height: 0;
        border: none;
        padding: 0;
        margin: 0;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "退出"),
        Binding("g", "goto_gate", "跳到 gate"),
        # phase 11 §2.4 / §3.1：Ctrl+G 弹 InterruptModal（中断纠偏）。
        Binding("ctrl+g", "interrupt", "中断/纠偏"),
        # phase 11 §2.4 / §6.1：d 弹 DialogModal（agent 跑完后多轮追问）。
        Binding("d", "dialog", "对话"),
        # spec v2 §1.2：a 恢复 auto-follow；C 全屏 ChartBrowser 看图表。
        # （小写 c 曾绑图表 tab——App 级 + NodeDetail widget 级两处都已移除：NodeDetail 是
        #   display:none 不可见，c 从未触发（死键）。图表统一走 C。``action_focus_charts``
        #   方法保留：phase-12 e2e 直接调它断言 nd.active_tab 内部 state，不依赖键位。）
        Binding("a", "follow_active", "跟随活跃"),
        Binding("C", "open_chart_browser", "全屏图表"),
        # spec v2 §1.2：thinking 默认显示（dim indigo，见 AgentHistory TYPE-LABEL）；
        # t 键提示用户用 Enter 展开折叠详情（不再 toggle 显隐）。
        Binding("t", "toggle_thinking", "切换 thinking"),
        # spec v2 §2.2 + §8 #2：j/k 切 agent（用户原话「切换 agent 查看历史记录」）。
        # 关键：j/k 必须在 App 级 BINDINGS 上提——AgentsList/AgentHistory widget 都是
        # ``Static``（``can_focus=False`` 默认），widget BINDINGS 在无焦点时不触发；
        # 同时 AgentHistory 内嵌的 RichLog（``agent-history-log``）拿到默认焦点后会吞
        # ``j``/``k``/``L`` 字符。App 级 BINDINGS 优先级最高，覆盖 RichLog 字符吞咽行为。
        # widget 内的 action_select_next/prev 仍是单测通道（不破坏既有接口）。
        Binding("j", "agents_next", "下一 agent"),
        Binding("k", "agents_prev", "上一 agent"),
        # spec v2 §2.3 + reviewer P0-6：Enter 切换折叠详情。同样需 App 级绑定覆盖
        # RichLog 字符吞咽（RichLog 默认 focus 时 Enter 也被吃）。
        Binding("enter", "history_toggle_expand", "展开/收起"),
        # review remediation（commit 5562e5e 回归修复）：down/up 箭头选中 AgentHistory
        # 上一条/下一条 entry，让 Enter 能展开非末条 entry。
        #
        # Rationale（为何不与 j/k 冲突）：
        # - j/k 已 hoist 到 App 级做**跨 agent 切换**（AgentsList.action_select_next/prev，
        #   spec v2 §2.2），驱动 AgentHistory.set_node 全量重渲（切到不同 agent 的 events）。
        # - down/up 做**同 agent 内 entry 导航**（AgentHistory.action_cursor_down/up，
        #   spec v2 §2.3），选中某条 entry 后 Enter 展开其折叠详情。
        # - 两套键位职责正交：j/k 横向切 agent，down/up 纵向切 entry。与 Conductor /
        #   VS Code 的「文件树 j/k + 编辑器内 down/up」模式一致（用户直觉迁移零成本）。
        # - 不复用 j/k 的另一原因：j/k 已被 AgentsList 占用，再用 j/k 做 entry 导航会
        #   让 action_select_next 与 action_cursor_down 互相覆盖（一个键绑两个 action）。
        #
        # ``priority=True`` 关键：RichLog（AgentHistory 内嵌的 agent-history-log）继承
        # ScrollableContainer，默认绑 down/up 做滚屏。无 priority 时 widget 级 BINDINGS
        # 优先于 App 级，down/up 被 RichLog 吞做滚屏，action_history_cursor_* 永不触发
        # （commit 5562e5e 回归的根因之一）。priority=True 让 App 级命中优先，RichLog
        # 不再吞 down/up；用户仍可用 PageDown/PageUp 滚屏（未被 App 级占用）。
        Binding("down", "history_cursor_down", "下一条 entry", priority=True),
        Binding("up", "history_cursor_up", "上一条 entry", priority=True),
        # spec v2 §2.4：L 键切 debug 显示。同上，App 级覆盖 RichLog。
        Binding("L", "log_toggle_debug", "切 debug 日志"),
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
        # phase-15 render layer §6.1：node 名 → executor（"claude" / "opencode" / ...）。
        # normalize_tool 查 ``(executor, tool) → kind`` 表用。仅 agent node 有 executor；
        # script/set/foreach/wait/terminate 不产 agent_tool_call，executor 占位无影响。
        self._node_executors: dict[str, str] = {
            n.name: getattr(n, "executor", "claude") or "claude"
            for n in wf.nodes if n.name
        }
        # phase-12 SPEC §1.4：临时 UI 交互态（不写 tape、不算业务真相，与 _active_modal 同类）。
        # _auto_follow 默认 True（node_started 自动跟随 running 节点）；j/k 或点选 → False（pin）；
        # a → 恢复 True。_selected_node 驱动 NodeDetail 全部内容。
        self._selected_node: str | None = None
        self._auto_follow: bool = True

        # spec v1.1 §4.4.1：reducer 派生 fold（重放必产相同值，与 _selected_node 严格区分）。
        # ADR §4.3.1（接口收敛 v2）：node fold 单一算法源在 ``orca.run.projections``，
        # TUI 维护一份全局 ``_all_events`` 列表后 batch 调 projections 派生 status /
        # usage / iter / session_ids（消除 RunState fold 与 TUI fold 两份独立派生）。
        # 旧字段（``_node_session_ids`` / ``_per_node_last_usage_seq``）删除——projections
        # 内置同算法（DRY），TUI 不再持有副本。
        #
        # ``_all_events``：全局事件流（projections 输入）。无界（TUI 是交互式工具，典型
        # workflow ≤ 数千 events；超大规模 workflow 走 background 模式不经 TUI）。
        # ``_node_events``（per-node 桶）与此正交：后者服务于 AgentHistory 切换渲染，
        # 有 FIFO 上限；前者服务于 projections，无上限（保 status 派生完整）。
        self._all_events: list[Event] = []
        # spec v2 §3：_node_events 分桶（agent_history 切换用）。
        # reducer fold：按 event.node 累积 events，切换 agent 时从这里取（纯前端切换，
        # 不读 tape）。FIFO 上限保护（_NODE_EVENTS_CAP）：超 1000 events/node 丢最旧
        # （spec §9 R3 + AgentHistory 头部行 truncated 标记）；tape 是真相源，丢的不丢真相。
        self._node_events: dict[str, list[Event]] = {}
        # spec v1.1 §6.2：per-node usage（agent_usage 收敛到 Header footer）。
        # 缓存自 ``projections.node_usage``（HeaderStats 的 ``per_node_usage`` 字段消费）。
        # 真正算法源在 ``projections`` —— 本字段是 UI 渲染缓存（DRY §4.3.1）。
        self._per_node_usage: dict[str, NodeUsageStats] = {}
        # spec v1.1 §4.4：node 耗时（node_started.timestamp → node_completed.elapsed）。
        # running 时 live timer 走 wall clock（spec §4.4 acceptance：「live timer 不进 tape」，
        # 即 UI 交互态，重放不重建）。v1 未实现 live timer（v2 路线）；node_completed 后 elapsed
        # 静态，从 data.elapsed 直接读，不依赖此字段。

    # ── Textual 钩子 ─────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        """spec v2 §2.1 三块布局：Header / [AgentsList | (AgentHistory / LogStream)] / NodeDetail / Footer。

        NodeDetail 保留实例（spec §6.3，display:none 移出可视区，仅 chart 路径消费）。
        """
        yield Header()
        with Horizontal(id="main-row"):
            yield AgentsList()                       # v2 左 30%（spec §2.2）
            with Vertical(id="right-pane"):          # v2 右侧 70%（spec §2.1）
                yield AgentHistory()                 # v2 右上 7fr（spec §2.3）
                yield LogStream()                    # v2 右下 3fr（spec §2.4 Conductor Log View）
            # spec §6.3：NodeDetail 保留实例（display:none，仅 chart 路径）。
            yield NodeDetail()
        yield Footer()

    def on_mount(self) -> None:
        """初始化 widget（AgentsList.build / Header / AgentHistory executor + 起事件消费 worker
        + kickoff 编排主流程。

        on_mount 在 Textual event loop 已 running 时被调，故此处 spawn ``@work`` worker
        安全（与 ``_consume_events`` 同 pattern）。**不**在 ``commands._run_workflow``
        里调 kickoff：那是 ``app.run()`` **之前**，loop 尚未起，``@work`` decorator
        试图 ``asyncio.create_task`` 时 ``events.get_running_loop()`` 抛 RuntimeError
        （真实 ``orca run`` 撞过的 bug）。

        单测用 ``run_test()`` 时 on_mount 同样会触发；如不希望真起编排（避免 spawn
        claude / uvicorn），可把 ``kickoff`` 替换成 no-op（见 ``test_app._patched_app``）。
        """
        # spec v2 §2.2：AgentsList.build（拓扑序纵向）。
        self.query_one(AgentsList).build(self._node_names)
        # Header 初始化（既有逻辑保留）。
        header = self.query_one(Header)
        header.update_stats(HeaderStats(
            run_id=self.run_id, workflow_name=self.wf.name, total=self._total_nodes,
        ))
        # spec v2 §2.3：AgentHistory 初始 executor（normalize_tool 查表用）。
        # entry node 的 executor 默认对齐（switch agent 时由 dispatch 重新 set）。
        self.query_one(AgentHistory).set_executor(
            self._node_executors.get(self.wf.entry or "", "claude"),
        )
        # 默认选中 entry node（与 v1.1.1 一致；auto-follow=True 仍会随 running 切换）。
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
        """把单个 event 分发到 v2 三块 widget（spec v2 §3 + §11.5 接口审计 7 条）。

        数据流（reducer fold，重放必产相同状态）：
          1. ``_all_events`` 全局累积 + ``_node_events[event.node]`` 分桶（agent_history
             切换用，spec §3）；分桶 FIFO 上限 ``_NODE_EVENTS_CAP``（1000 events/node，
             spec §9 R3）。
          2. ADR §4.3.1 DRY fold：``projections.node_status / node_usage /
             node_session_ids`` 是单一派生算法源，TUI 不再持有独立 fold 副本
             （删 ``_node_session_ids`` / ``_per_node_last_usage_seq``）。
          3. ``AgentsList.update_node()`` —— 节点状态投影（status / elapsed / tokens / iter），
             status 经 projections 派生（P4：blocked 不再字面量）。
          4. ``AgentHistory.append_event()`` —— **仅 selected_node** 的事件追加
             （reviewer P1-12：auto-follow 时 node_started 触发 _selected_node 更新
             + AgentHistory.set_node 全量重渲，否则用户看不到后续 agent）。
          5. ``LogStream.append_event()`` —— 按 ``EVENT_LEVEL`` 表派发高层事件
             （spec §11.5 #6 + reviewer P0-3：``dict.get()`` 三态，无 try/except KeyError 死代码）。
          6. ``NodeDetail.upsert_chart()`` —— 仅 chart 路径（spec §6.3 保留实例）。

        接口统一性铁律（spec §11.5 + 用户底线）：本方法是节点状态/错误/事件类型的**唯一**
        消费入口；widget 不重新分类、不重新命名；canonical Event schema 不动。
        """
        etype = event.type
        data = event.data or {}
        node = event.node

        # ── 1. 全局 events 累积（projections 输入）+ _node_events 分桶（agent_history）──
        # 全局 list 无界（保 status 派生完整）；per-node 桶 FIFO 上限（spec §9 R3）。
        # 两者正交：_all_events 喂 projections（派生 status/usage/iter）；_node_events 喂
        # AgentHistory 切换（渲染用，可丢最旧）。
        self._all_events.append(event)
        if node:
            bucket = self._node_events.setdefault(node, [])
            bucket.append(event)
            if len(bucket) > _NODE_EVENTS_CAP:
                bucket.pop(0)

        # ── 2. projections-driven fold + AgentsList 投影（ADR §4.3.1 DRY + P4）──
        # status / iter / tokens 全部经 projections 派生（消除 TUI 与 RunState 两份独立 fold）。
        if etype == "node_started" and node:
            # iter 派生（projections.node_session_ids）：retry 时新 session_id 触发 +1。
            sid = event.session_id or ""
            sessions = projections.node_session_ids(self._all_events).get(node, [])
            if sid and sid in sessions:
                iter_n = sessions.index(sid) + 1
            else:
                # 空 session_id / 未登记（防御乱序）：取 list 长度 + 1（与既有逻辑对齐）。
                iter_n = len(sessions) + 1
            # status 派生（projections.node_status）：blocked 也由它派生（P4）。
            status = projections.node_status(self._all_events).get(node, "running")
            self.query_one(AgentsList).update_node(node, status=status, iter_n=iter_n)
            # reviewer P1-12 + spec §11.5 #7：auto-follow 必须**在 dispatch 内**——
            # _auto_follow=True 时 node_started 触发 _selected_node 更新 + AgentHistory
            # 全量重渲（从 _node_events 桶取），否则用户看不到后续 agent 输出。
            if self._auto_follow:
                self._selected_node = node
                # phase-16 auto-follow sync：同步 AgentsList 可见光标（▸ marker）到
                # 跟随节点，避免「app 选 X / 列表还显 Y」不一致——用户从 STALE 的 Y
                # 出发按 j/k 会跳到错误 agent（real-execution 发现的 bug）。
                # 用 set_selected_silent 不触发 _on_node_selected（否则会把 _auto_follow
                # 改回 False，造成 auto-follow 自我取消）。
                try:
                    self.query_one(AgentsList).set_selected_silent(node)
                except Exception:  # noqa: BLE001 —— AgentsList 未挂载（headless）
                    pass
                try:
                    self.query_one(AgentHistory).set_executor(
                        self._node_executors.get(node, "claude"),
                    )
                    self.query_one(AgentHistory).set_node(
                        node, self._node_events.get(node, []),
                    )
                except Exception:  # noqa: BLE001 —— AgentHistory 未挂载（headless）
                    pass
            # phase 11 §3：追踪当前 node + session（action_interrupt 用，既有逻辑保留）。
            import time as _time
            self._current_node = node
            self._node_started_at = _time.monotonic()
            self._current_session_id = event.session_id
            self._refresh_header()

        elif etype == "node_completed" and node:
            elapsed = data.get("elapsed")
            status = projections.node_status(self._all_events).get(node, "done")
            self.query_one(AgentsList).update_node(
                node, status=status,
                elapsed=float(elapsed) if elapsed is not None else None,
            )
            self._done_nodes += 1
            self._refresh_header()
            # phase 11 §6.1：记最近完成的 agent node + output（action_dialog 按 d 用）。
            if node in self._agent_node_names:
                self._last_completed_agent_node = node
                self._last_completed_agent_output = data.get("output")

        elif etype == "node_failed" and node:
            msg = str(data.get("message", data.get("error_type", "")))
            status = projections.node_status(self._all_events).get(node, "failed")
            self.query_one(AgentsList).update_node(node, status=status, error_msg=msg)

        elif etype == "node_skipped" and node:
            # spec v2 §2.2：AgentsList 不单独处理 skip（status 保持上一态），
            # LogStream 会按 LEVEL_WARN 显示一行。
            pass

        # gate / interrupt 事件（ADR §4.3：blocked 派生走 projections，P4 合规）
        elif etype == "human_decision_requested":
            gate_id = data.get("gate_id", "")
            self._awaiting_gates.add(gate_id)
            self._refresh_header()
            if node:
                # status 从 projections 派生（apply_event overlay running→blocked）。
                # 不再字面量 "blocked"——P4 守门（test_status_literal.py AST 检查）。
                status = projections.node_status(self._all_events).get(node, "running")
                self.query_one(AgentsList).update_node(node, status=status)
            # 推 GateModal 参与竞速（@work 内 push_screen_wait）。
            self._push_gate_modal(event)

        elif etype == "human_decision_resolved":
            gate_id = data.get("gate_id", "")
            self._awaiting_gates.discard(gate_id)
            self._refresh_header()
            if node:
                # status 从 projections 派生（apply_event overlay blocked→running）。
                status = projections.node_status(self._all_events).get(node, "running")
                self.query_one(AgentsList).update_node(node, status=status)
            # 广播输家：本壳 modal 还在 → notify_resolved_externally 让它 dismiss。
            if self._active_modal is not None:
                self._active_modal.notify_resolved_externally(
                    source=str(data.get("resolved_by", "?")),
                    answer=str(data.get("answer", "")),
                )

        elif etype in ("interrupt_requested", "interrupt_resolved") and node:
            # phase 11 §3：interrupt 事件也经 projections 派生 blocked（与 gate 同源，
            # ADR §4.3 派生规则覆盖 gate + interrupt 两类）。
            status = projections.node_status(self._all_events).get(node, "running")
            self.query_one(AgentsList).update_node(node, status=status)

        # agent_usage 收敛（projections.node_usage 单一算法源，ADR §4.3.1 DRY）
        if etype == "agent_usage" and node:
            usage_map = projections.node_usage(self._all_events)
            usage = usage_map.get(node)
            if usage is not None:
                tokens = usage.input_tokens + usage.output_tokens
                # _per_node_usage 是 HeaderStats 渲染缓存（NodeUsageStats 转 UsageSummary）。
                self._per_node_usage[node] = NodeUsageStats(
                    name=node, tokens=tokens, cost_usd=usage.cost_usd,
                )
                # spec v2 §11.5 + reviewer P1-12 fix：投影到 AgentsList 行（tokens 字段）。
                self.query_one(AgentsList).update_node(node, tokens=tokens)
            self._refresh_header()

        # ── 3. AgentHistory 分派（仅 selected_node 的事件，spec v2 §3）──────
        # 调用方负责过滤 node == _selected_node（避免双重渲染：auto-follow 路径已 set_node
        # 全量重渲，append_event 走增量追加仅对当前选中节点）。
        # phase-16 double-render fix：当本 event 是 node_started 且 auto-follow 刚用
        # set_node 全量重渲（bucket 已含该 node_started），必须**跳过** append_event——
        # 否则 node_started 被加两次（set_node 的 fold 加 1 + append_event 再加 1），
        # 产生重复 seq entry，破坏 §5.6 重放一致性 + 卡死 cursor advance（duplicate seq）。
        if (node and node == self._selected_node
                and not (etype == "node_started" and self._auto_follow)):
            try:
                self.query_one(AgentHistory).append_event(event)
            except Exception as e:  # noqa: BLE001
                logger.debug("AgentHistory append skipped: %r", e)

        # ── 4. LogStream 分派（按 EVENT_LEVEL 三态，spec v2 §11.5 #6）──────
        # reviewer P0-3：dict.get() 三态语义，无 try/except KeyError 死代码。
        #   - 合法 level：表内已登记的 EventType（30 个），返回对应 level 字符串。
        #   - None (explicit skip)：EVENTS_NOT_IN_LOG_STREAM 白名单 7 个
        #     （agent_* / prompt_rendered / custom），归 AgentHistory / Header / chart 路径。
        #   - 未登记 type：LEVEL_INFO 兜底（fail visible，spec §11.5 #6）。
        level = EVENT_LEVEL.get(etype)
        if level is None and etype not in EVENTS_NOT_IN_LOG_STREAM:
            # 未登记 EventType（schema 加新 type 漏登记）→ fail visible，发 info 行不静默吞。
            level = LEVEL_INFO
        # debug toggle 真相源**唯一**在 LogStream widget（L 键 binding + _debug_buffer）。
        # phase-16 §5.1：dispatch 把所有 level != None 的事件都传给 LogStream，由 widget
        # 决定写/缓冲（show_debug=False 时 debug 事件进 _debug_buffer，L 键 toggle ON 回放）。
        # 这避免「dispatch 层 mirror show_debug + widget 层真相」双真相源（spec §11.5 #6），
        # 同时让用户在 run 结束后按 L 能看到历史 debug 事件（SPEC §5.1 行 L「前无后有」）。
        if level is not None:
            try:
                self.query_one(LogStream).append_event(
                    etype, data, node=node, session_id=event.session_id,
                    timestamp=event.timestamp,
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("LogStream append skipped: %r", e)

        # ── 5. NodeDetail chart 路径（保留实例但 display:none，仅 chart 消费）──
        # spec §6.3：NodeDetail 不再走 stream/output tab 路径（v1.1.1 双写已删）；
        # 仅 ``custom(kind=chart)`` 走 ChartPanel（``c`` 键聚焦 / ``C`` 键 ChartBrowser 全屏）。
        if etype == "custom" and data.get("kind") == "chart":
            payload = data.get("chart")
            if not isinstance(payload, dict):
                logger.warning(
                    "custom(chart) payload 非 dict，跳过: %r", type(payload).__name__,
                )
                return  # 早返回，避免下面 workflow 终态分支误中
            node_key = node if node is not None else "__workflow__"
            try:
                self.query_one(NodeDetail).upsert_chart(node_key, payload)
            except Exception:  # noqa: BLE001 —— NodeDetail 未挂载
                pass

        # ── 6. workflow 终态 notify（既有逻辑保留）──────────────────────────
        if etype == "workflow_failed":
            self.notify(
                "workflow failed — 见 Log Stream 详情，按 q 退出",
                severity="error", timeout=0,
            )
        elif etype == "workflow_completed":
            self.notify(
                "workflow completed — 按 q 退出",
                severity="information", timeout=0,
            )

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

    def _on_node_selected(self, name: str | None) -> None:
        """AgentsList.select 回调（spec v2 §3 切换语义）。

        pin：``_auto_follow=False``；``_selected_node=name``；
        AgentHistory 从 ``_node_events`` 桶全量重渲（纯前端切换，不读 tape）。

        v2 spec §1.2 + §2.3：单 agent 视图（j/k 切换）。
        NodeDetail 保留实例但仅 chart 路径活跃（spec §6.3）：仍调 ``set_node`` 同步
        ChartPanel 的节点过滤（``c`` 键 / ``C`` 键 ChartBrowser 仍依赖此过滤），
        但 stream/output tab 不再有数据流入（spec §4.4 双写已删）。
        """
        if not name:
            return
        self._selected_node = name
        self._auto_follow = False
        events = self._node_events.get(name, [])
        try:
            # 切换 agent 时也同步 executor（normalize_tool 查表用）。
            self.query_one(AgentHistory).set_executor(
                self._node_executors.get(name, "claude"),
            )
            self.query_one(AgentHistory).set_node(name, events)
        except Exception as e:  # noqa: BLE001 —— AgentHistory 未挂载（headless）
            logger.debug("AgentHistory set_node skipped: %r", e)
        try:
            # spec §6.3：NodeDetail 仅 chart 路径活跃，但 chart 过滤仍按 selected_node。
            kind = self._node_kinds.get(name)
            self.query_one(NodeDetail).set_node(name, kind=kind)
        except Exception as e:  # noqa: BLE001 —— NodeDetail 未挂载（headless）
            logger.debug("NodeDetail set_node skipped: %r", e)

    def action_follow_active(self) -> None:
        """``a`` 键：恢复 auto-follow（SPEC §1.4 / §5）。

        ``_auto_follow=True``；``_selected_node=当前 running 节点``（无 running 则不变）。
        AgentHistory 从 ``_node_events`` 桶全量重渲（与 ``_on_node_selected`` 同语义）。
        phase-16：同步 AgentsList 光标（与 auto-follow sync 同语义，避免 a 后列表不一致）。
        """
        self._auto_follow = True
        running = self._current_node
        if running is not None:
            self._selected_node = running
            events = self._node_events.get(running, [])
            try:
                self.query_one(AgentsList).set_selected_silent(running)
            except Exception:  # noqa: BLE001
                pass
            try:
                self.query_one(AgentHistory).set_executor(
                    self._node_executors.get(running, "claude"),
                )
                self.query_one(AgentHistory).set_node(running, events)
            except Exception:  # noqa: BLE001
                pass

    def action_focus_charts(self) -> None:
        """``c`` 键：聚焦 NodeDetail + 切图表 tab（SPEC §5）。

        spec §6.3：NodeDetail display:none 但仍 mount；c 键切换内部 tab state（既有
        e2e_phase12 测试断言 nd.active_tab == "charts"）。视觉上 NodeDetail 不可见，
        但内部 state 切换仍可观测（ChartBrowser 全屏走 C）。
        """
        try:
            nd = self.query_one(NodeDetail)
            nd.action_focus_charts()
        except Exception:  # noqa: BLE001 —— NodeDetail 未挂载
            pass

    def action_open_chart_browser(self) -> None:
        """``C`` 键：全屏 ChartBrowser（SPEC §4.5 / §5）。Esc/q 退。"""
        self.push_screen(ChartBrowser())

    def action_toggle_thinking(self) -> None:
        """``t`` 键：v2 thinking 默认显示（dim indigo，见 AgentHistory TYPE-LABEL）。

        spec v2 §1.2：thinking 不再 toggle 显隐，按 ``t`` 提示用户用 Enter 展开折叠详情
        （Enter 是 AgentHistory 内 toggle_expand 的 binding）。
        """
        self.notify(
            "AgentHistory: thinking 默认 dim 显示，按 Enter 展开折叠详情",
            severity="information", timeout=2,
        )

    # spec v2 §2.2 / §2.3 / §2.4：App 级 BINDINGS 上提的 4 个键的 action 转发。
    # widget 自身 BINDINGS 在无 focus 时不触发 + RichLog 拿默认焦点后吞字符；
    # App 级 BINDINGS 命中后调既有 widget action_* 方法（接口零修改，单测通道保留）。

    def action_agents_next(self) -> None:
        """``j`` 键：调 AgentsList.action_select_next（spec v2 §2.2 + §8 #2）。

        App 级上提原因见 BINDINGS 注释（AgentsList widget 是 Static can_focus=False，
        widget BINDINGS 在无 focus 时不触发；AgentHistory 内嵌 RichLog 拿默认焦点后
        吞 j 字符）。本方法仅转发到 AgentsList.action_select_next——后者调 select()
        → ``_on_node_selected`` → ``AgentHistory.set_node`` 全量重渲。
        """
        try:
            self.query_one(AgentsList).action_select_next()
        except Exception:  # noqa: BLE001 —— AgentsList 未挂载（极端 headless）
            pass

    def action_agents_prev(self) -> None:
        """``k`` 键：调 AgentsList.action_select_prev（spec v2 §2.2 + §8 #2）。"""
        try:
            self.query_one(AgentsList).action_select_prev()
        except Exception:  # noqa: BLE001
            pass

    def action_history_toggle_expand(self) -> None:
        """``Enter`` 键：调 AgentHistory.action_toggle_expand（spec v2 §2.3 + reviewer P0-6）。

        注意：AgentHistory 自身也绑了 enter（widget 级 BINDINGS），但 RichLog 默认 focus
        时字符会被吞。App 级 BINDINGS 优先命中本方法 → 转发到 widget action（不破坏
        既有单测通道：``test_action_toggle_expand`` 直接调 widget.action_toggle_expand）。
        """
        try:
            self.query_one(AgentHistory).action_toggle_expand()
        except Exception:  # noqa: BLE001
            pass

    def action_history_cursor_down(self) -> None:
        """``down`` 键：调 AgentHistory.action_cursor_down（review remediation commit 5562e5e）。

        Rationale 见 BINDINGS 注释：j/k 已被 AgentsList 占用做横向 agent 切换；
        down/up 做同 agent 内纵向 entry 导航，职责正交。App 级 BINDINGS 优先命中本方法
        → 转发到 widget action（单测通道保留：``test_widgets.py`` 直接调 widget.action_cursor_*）。
        """
        try:
            self.query_one(AgentHistory).action_cursor_down()
        except Exception:  # noqa: BLE001
            pass

    def action_history_cursor_up(self) -> None:
        """``up`` 键：调 AgentHistory.action_cursor_up（review remediation commit 5562e5e）。"""
        try:
            self.query_one(AgentHistory).action_cursor_up()
        except Exception:  # noqa: BLE001
            pass

    def action_log_toggle_debug(self) -> None:
        """``L`` 键：调 LogStream.action_toggle_debug（spec v2 §2.4）。

        LogStream 是 RichLog（can_focus=True 默认），自己拿焦点时也会吞 L 字符；
        App 级 BINDINGS 上提确保命中。
        """
        try:
            self.query_one(LogStream).action_toggle_debug()
        except Exception:  # noqa: BLE001
            pass

    # ── header 刷新（SPEC §4.4）─────────────────────────────────────────

    def _refresh_header(self) -> None:
        """重算 header stats（done / awaiting / per-node usage / running）。"""
        # spec v2 §1.2 + §2.3：单 agent 视图（j/k 切换）。
        # 保留 ``filter_node=None`` 参数兼容 HeaderStats（Header footer 显示全部节点）。
        filter_node: str | None = None
        # spec v1.1 §6.2：per-node usage 按声明序（wf.nodes）输出，running 节点优先。
        per_node = [
            self._per_node_usage[n.name]
            for n in self.wf.nodes
            if n.name and n.name in self._per_node_usage
        ]
        self.query_one(Header).update_stats(HeaderStats(
            run_id=self.run_id,
            workflow_name=self.wf.name,
            done=self._done_nodes,
            total=self._total_nodes,
            awaiting_gate=len(self._awaiting_gates),
            per_node_usage=per_node,
            filter_node=filter_node,
            running_node=self._current_node,
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
