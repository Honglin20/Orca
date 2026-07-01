"""test_app.py —— OrcaApp 组装 + 事件消费 + gate 流程单测（SPEC §6.2 §6.3 / 计划 C3.3）。

headless Textual ``run_test()`` pilot 测试，CI 友好。覆盖：
  - compose 产出 4 个核心 widget（Header/DagTree/ActiveNode/LogStream）+ Footer
  - 事件分发：注入 node_started/completed → DagTree 图标更新 + Header 计数
  - gate 流程：注入 human_decision_requested → GateModal 弹出 → press allow → resolve 被调
  - **DAG 在 gate 期间继续刷新**（SPEC §6.0 铁律 3 / §1 决策 1：Textual 决定性优势）
  - 广播输家：modal 在屏时收到 human_decision_resolved → modal 关闭（不 resolve）
  - terminal_state 透传（编排 worker 写入）
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from unittest.mock import patch

import yaml

from orca.iface.cli.app import OrcaApp, _GateHttpBridge
from orca.iface.cli.widgets import ActiveNode, DagTree, Header, LogStream
from orca.iface.cli.screens.gate_modal import GateModal


def run_async(coro):
    return asyncio.run(coro)


def _app(tmp_path, wf, **kwargs):
    """构造 OrcaApp，tape 写 tmp_path（避免污染 CWD + 文件句柄泄漏）。

    review blocker 修复：OrcaApp 默认 tape_path 是 ``./runs/<run_id>.jsonl``，测试若不
    注入会在仓库根目录创建文件。所有 pilot 测试都经此 helper 构造 app。

    自动 mock ``kickoff`` 为 no-op：on_mount 现在会自动调 kickoff（修复 ``orca run``
    真实运行的 ``no running event loop`` bug），但 pilot 测试用 ``_event()`` 注入 fake
    events 测渲染，不需要真起编排（spawn claude / uvicorn）。需要真起编排的测试可
    显式 ``app.kickoff = OrcaApp.kickoff.__get__(app)`` 还原。
    """
    app = OrcaApp(wf=wf, tape_path=tmp_path / "events.jsonl", **kwargs)
    app.kickoff = lambda: None  # type: ignore[method-assign]  # no-op：pilot 测试不真起编排
    return app


def _linear_workflow(tmp_path: Path) -> Path:
    """最小线性 workflow yaml（a→$end，全 script，零依赖，零 token）。"""
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump({
        "name": "t",
        "entry": "a",
        "nodes": [
            {"name": "a", "kind": "script", "command": "echo hi",
             "routes": [{"to": "$end"}]},
        ],
    }), encoding="utf-8")
    return p


def _load(p: Path):
    from orca.compile import load_workflow
    return load_workflow(p)


def _event(etype: str, data: dict | None = None, *, node: str | None = None,
           session_id: str | None = None, seq: int = 0, timestamp: float = 0.0):
    """构造一个轻量 event-like 对象（duck-typed：``type/data/node/session_id/seq/timestamp``）。

    用 SimpleNamespace 而非 pydantic Event，避免构造 Event 时要 seq 全局唯一——
    _dispatch_to_widgets 只读字段，不关心 Event 是否真 pydantic 实例。
    """
    from types import SimpleNamespace
    return SimpleNamespace(
        type=etype, data=data or {}, node=node,
        session_id=session_id, seq=seq, timestamp=timestamp,
    )


# ── compose 结构（SPEC §3.2）──────────────────────────────────────────────


class TestCompose:
    """OrcaApp.compose 产出 Header/DagTree/ActiveNode/LogStream + Footer。"""

    def test_compose_yields_core_widgets(self, tmp_path):
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                assert app.query_one(Header) is not None
                assert app.query_one(DagTree) is not None
                assert app.query_one(ActiveNode) is not None
                assert app.query_one(LogStream) is not None
        run_async(scenario())

    def test_dag_tree_built_from_workflow(self, tmp_path):
        """DagTree 初始化：全部 node 显示为 pending。"""
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                tree = app.query_one(DagTree)
                assert "○ a" == tree.label_of("a")
        run_async(scenario())

    def test_header_shows_workflow_name(self, tmp_path):
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                header = app.query_one(Header)
                assert header.stats is not None
                assert "t" in header.stats.render_text()
        run_async(scenario())


# ── 事件分发（SPEC §6.0 铁律 1：纯派生）───────────────────────────────────


class TestEventDispatch:
    """注入 fake 事件 → widget 状态更新正确（replay 一致性）。"""

    def test_node_started_sets_running_icon(self, tmp_path):
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                app._dispatch_to_widgets(_event("node_started", {"kind": "script"}, node="a"))
                await pilot.pause()
                tree = app.query_one(DagTree)
                assert "✽ a" == tree.label_of("a")  # running
                # ActiveNode 切到 a
                assert app.query_one(ActiveNode).active == "a"
        run_async(scenario())

    def test_node_completed_sets_done_icon_and_increments_header(self, tmp_path):
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                app._dispatch_to_widgets(_event("node_completed", {"elapsed": 0.5}, node="a"))
                await pilot.pause()
                tree = app.query_one(DagTree)
                assert "✓ a" == tree.label_of("a")  # done
                header = app.query_one(Header)
                assert header.stats.done == 1
                assert "1/1 nodes" in header.stats.render_text()
        run_async(scenario())

    def test_node_failed_sets_failed_icon(self, tmp_path):
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                app._dispatch_to_widgets(
                    _event("node_failed", {"error_type": "ExecTimeout", "message": "boom"}, node="a"),
                )
                await pilot.pause()
                assert "! a" == app.query_one(DagTree).label_of("a")
        run_async(scenario())

    def test_log_stream_receives_agent_events(self, tmp_path):
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                app._dispatch_to_widgets(
                    _event("agent_message", {"text": "hello"},
                           node="a", session_id="abcd1234"),
                )
                await pilot.pause()
                await pilot.pause()  # RichLog 异步 flush
                log = app.query_one(LogStream)
                text = _flatten_strips(log.lines)
                assert "hello" in text
        run_async(scenario())

    def test_idempotent_dispatch_replay_consistent(self, tmp_path):
        """SPEC §6.0 铁律 1：同一事件序列重放，状态必然一致。"""
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                # 重放两次同一事件序列
                for _ in range(2):
                    app._dispatch_to_widgets(_event("node_started", {}, node="a"))
                    await pilot.pause()
                # running 状态保持（不会因重放退化成 done 或 pending）
                assert "✽ a" == app.query_one(DagTree).label_of("a")
        run_async(scenario())


# ── gate 流程（SPEC §6.3 / 计划 C3.3）────────────────────────────────────


class TestGateFlow:
    """gate 弹窗 + resolve + DAG 期间刷新（Textual 决定性优势核心验证）。"""

    def _gate_request_event(self, *, gate_id="g-test", source="tool_permission",
                            node="review", prompt="批准 Bash？"):
        return _event(
            "human_decision_requested",
            {
                "gate_id": gate_id, "prompt": prompt,
                "options": ["allow", "deny"], "source": source,
                "context": {"tool": "Bash", "tool_input": {"command": "rm -rf x"}},
                "run_id": "r-test", "node": node,
            },
            node=node, session_id="sess-gate",
        )

    def test_gate_request_pushes_modal(self, tmp_path):
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                app._dispatch_to_widgets(self._gate_request_event())
                await pilot.pause()
                await pilot.pause()  # @work _push_gate_modal 异步起
                assert isinstance(app.screen, GateModal)
                # header awaiting 计数 = 1
                header = app.query_one(Header)
                assert header.stats.awaiting_gate == 1
        run_async(scenario())

    def test_press_allow_calls_resolve_with_cli_source(self, tmp_path):
        """SPEC §6.0 铁律 2：用户答 → handler.resolve(gate_id, answer, 'cli')。"""
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)
        resolved_calls = []
        app.gate_handler.resolve = lambda gid, ans, src: resolved_calls.append((gid, ans, src)) or True

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                app._dispatch_to_widgets(self._gate_request_event())
                await pilot.pause()
                await pilot.pause()
                # 点 allow 按钮
                await pilot.click("#gate-allow")
                await pilot.pause()
                await pilot.pause()
                assert resolved_calls == [("g-test", "allow", "cli")]
        run_async(scenario())

    def test_dag_refreshes_while_gate_pending(self, tmp_path):
        """SPEC §6.0 铁律 3 / §1 决策 1：gate 在屏期间，DAG 仍能被新事件更新。

        这是 Textual 相对 Rich Live 的决定性优势的回归保护——用 Rich Live 时
        gate prompt 阻塞期间无法更新 DAG（Discussion #1791）。此处用 pilot 注入
        gate 事件 → 注入另一个 node 的状态更新 → 验证两者都生效（互不阻塞）。
        """
        # 用 2-node workflow 让 gate 在 node a 时，还能更新 node b
        from orca.compile import load_workflow
        p = tmp_path / "wf2.yaml"
        p.write_text(yaml.safe_dump({
            "name": "t2", "entry": "a",
            "nodes": [
                {"name": "a", "kind": "script", "command": "echo a",
                 "routes": [{"to": "b"}]},
                {"name": "b", "kind": "script", "command": "echo b",
                 "routes": [{"to": "$end"}]},
            ],
        }), encoding="utf-8")
        wf = load_workflow(p)
        app = _app(tmp_path, wf)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                # gate 弹在 node a
                app._dispatch_to_widgets(self._gate_request_event(node="a"))
                await pilot.pause()
                await pilot.pause()
                assert isinstance(app.screen, GateModal)  # gate 在屏
                # 同时，node b 收到 completed（DAG 在 gate 期间继续刷新）
                app._dispatch_to_widgets(_event("node_completed", {"elapsed": 1.0}, node="b"))
                await pilot.pause()
                # gate modal 还在（用户没答）
                assert isinstance(app.screen, GateModal)
                # node b 的图标确实更新了（证明 gate 没冻结 DAG 渲染）
                assert "✓ b" == app.query_one(DagTree).label_of("b")
        run_async(scenario())

    def test_broadcast_loser_dismisses_modal_without_resolve(self, tmp_path):
        """SPEC §4.5 决策 5：别壳先答 → 本壳 modal dismiss，不调 resolve（赢家已 resolve）。"""
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)
        resolved_calls = []
        app.gate_handler.resolve = lambda gid, ans, src: resolved_calls.append((gid, ans, src)) or True

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                app._dispatch_to_widgets(self._gate_request_event())
                await pilot.pause()
                await pilot.pause()
                assert isinstance(app.screen, GateModal)
                # 别壳先答（广播）
                app._dispatch_to_widgets(_event(
                    "human_decision_resolved",
                    {"gate_id": "g-test", "answer": "allow", "resolved_by": "web"},
                    node="review", session_id="sess-gate",
                ))
                await pilot.pause()
                await pilot.pause()
                # modal 自动关
                assert not isinstance(app.screen, GateModal)
                # 本壳没调 resolve（别壳已 resolve）
                assert resolved_calls == []
                # header awaiting 清零
                assert app.query_one(Header).stats.awaiting_gate == 0
        run_async(scenario())

    def test_resolved_unblocks_node_icon(self, tmp_path):
        """gate resolved → node 从 blocked 回 running（claude resume 继续跑）。"""
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                app._dispatch_to_widgets(self._gate_request_event(node="a"))
                await pilot.pause()
                await pilot.pause()
                assert "⏸ a" == app.query_one(DagTree).label_of("a")  # blocked
                app._dispatch_to_widgets(_event(
                    "human_decision_resolved",
                    {"gate_id": "g-test", "answer": "allow", "resolved_by": "web"},
                    node="a", session_id="sess-gate",
                ))
                await pilot.pause()
                # node 解除 blocked → running
                assert "✽ a" == app.query_one(DagTree).label_of("a")
        run_async(scenario())


# ── terminal_state（退出码依据）─────────────────────────────────────────


class TestTerminalState:
    """terminal_state 由编排 worker 写入；commands 据 .status 决定 exit code。"""

    def test_initial_terminal_state_is_none(self, tmp_path):
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)
        assert app.terminal_state is None


# ── commands._run_workflow 退出码（headless，mock Orchestrator）───────────


class TestRunExitCodesHeadless:
    """``_run_workflow`` 据 terminal_state.status 决定 exit 0/1（不依赖真 claude）。

    review 建议修复：completed→0 / failed→1 路径无 headless 单测，补之。用 monkeypatch
    把 ``Orchestrator.run`` 替换成「直接置 terminal_state 然后 close bus」，并 mock 掉
    Textual ``App.run``（不真起 TUI），断言退出码常量。
    """

    def _patched_app(self, app, status):
        """让 app.run() 不真起 TUI，只模拟编排 worker 写入 terminal_state。

        同时把 ``kickoff`` 替换成 no-op，避免 _run_workflow 调它时真起 HTTP 桥 + 真跑
        Orchestrator（这两者都需要 TUI 的事件循环，mock run 下没有 loop）。
        """
        from orca.schema import RunState

        def _fake_run():
            # 模拟 _run_pipeline 写入终态（status 由测试控制）
            app.terminal_state = RunState(
                run_id=app.run_id, workflow_name=app.wf.name, status=status,
            )
        app.run = _fake_run
        app.kickoff = lambda: None  # no-op：不起 HTTP 桥 / 不跑 Orchestrator
        return app

    def test_completed_exits_zero(self, tmp_path):
        from orca.iface.cli.commands import _run_workflow, RunConfig, EXIT_OK
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)
        self._patched_app(app, "completed")
        with patch("orca.iface.cli.app.OrcaApp", return_value=app):
            code = _run_workflow(RunConfig(yaml_path=tmp_path / "wf.yaml"))
        assert code == EXIT_OK

    def test_failed_exits_one(self, tmp_path):
        from orca.iface.cli.commands import _run_workflow, RunConfig, EXIT_RUN_FAILED
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)
        self._patched_app(app, "failed")
        with patch("orca.iface.cli.app.OrcaApp", return_value=app):
            code = _run_workflow(RunConfig(yaml_path=tmp_path / "wf.yaml"))
        assert code == EXIT_RUN_FAILED

    def test_user_quit_no_terminal_state_exits_one(self, tmp_path):
        """用户中途 q 退出（terminal_state 留 None）→ exit 1（fail loud）。"""
        from orca.iface.cli.commands import _run_workflow, RunConfig, EXIT_RUN_FAILED
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)
        # 不写 terminal_state（模拟 q 退出）；kickoff 也 no-op 避免 HTTP 桥 + Orchestrator
        app.run = lambda: None
        app.kickoff = lambda: None
        with patch("orca.iface.cli.app.OrcaApp", return_value=app):
            code = _run_workflow(RunConfig(yaml_path=tmp_path / "wf.yaml"))
        assert code == EXIT_RUN_FAILED


# ── _GateHttpBridge 线程/loop 隔离（review 建议补的并发测试）───────────────


def _free_port() -> int:
    """找一个空闲端口（避免测试间端口占用冲突）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestGateHttpBridge:
    """_GateHttpBridge 起停 + 失败 fail loud（review major #2）。

    覆盖：
      - start→stop 干净退出（线程 join 在 5s 内，无 leaked task）
      - start 在被占用端口失败 → fail loud 记 error（_start_error 非 None），TUI 不崩
    """

    def test_start_stop_exits_cleanly(self):
        from orca.events.bus import EventBus
        from orca.events.tape import Tape
        from orca.gates.handler import HumanGateHandler
        from orca.gates.context_registry import SessionContextRegistry
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tape = Tape(Path(td) / "e.jsonl", run_id="r")
            bus = EventBus(tape)
            handler = HumanGateHandler(bus)
            registry = SessionContextRegistry()
            port = _free_port()
            bridge = _GateHttpBridge(handler, registry, port=port)

            bridge.start()
            thread = bridge._thread
            assert thread is not None
            # ready 应在 5s 内 set（uvicorn startup 完成）
            assert bridge._ready.wait(timeout=5.0), "uvicorn 5s 内未就绪"
            assert bridge._start_error is None, f"启动失败：{bridge._start_error}"

            bridge.stop()
            # 线程在 5s 内退出（stop 内部 join；_thread 已置 None，故先 capture 句柄）
            assert not thread.is_alive(), "HTTP bridge 线程未在 5s 内退出"
            # handler broadcaster 干净停（无 leaked task 警告 = stop 真等到了 handler.stop）
            assert bridge._thread is None

    def test_start_on_occupied_port_fails_loud(self):
        """端口被占 → uvicorn startup 失败 → _start_error 非 None（fail loud）。"""
        from orca.events.bus import EventBus
        from orca.events.tape import Tape
        from orca.gates.handler import HumanGateHandler
        from orca.gates.context_registry import SessionContextRegistry
        import tempfile

        # 先占住一个端口
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        occupied_port = blocker.getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as td:
                tape = Tape(Path(td) / "e.jsonl", run_id="r")
                bus = EventBus(tape)
                handler = HumanGateHandler(bus)
                registry = SessionContextRegistry()
                bridge = _GateHttpBridge(handler, registry, port=occupied_port)

                bridge.start()
                # ready 会 set（无论成功失败），但 _start_error 应非 None
                assert bridge._ready.wait(timeout=5.0)
                # fail loud：startup 失败记录了 error（不静默吞）
                assert bridge._start_error is not None
                bridge.stop()  # 清理（即使失败也要 join 线程）
        finally:
            blocker.close()


# ── helper（与 test_widgets 同款）─────────────────────────────────────────


def _flatten_strips(strips) -> str:
    """拍平 RichLog ``lines``（Strip 列表）为纯文本（断言用）。"""
    parts = []
    for strip in strips:
        for segment in strip._segments:
            parts.append(segment.text)
    return "".join(parts)


# ── phase 11 §3：Interrupt UI（Ctrl+G → InterruptModal + 事件分发）───────────


class TestInterruptFlow:
    """Ctrl+G 弹 InterruptModal + interrupt_* 事件分发到 LogStream（SPEC §3.1）。"""

    def test_ctrl_g_pushes_interrupt_modal(self, tmp_path):
        """pilot 按 Ctrl+G → InterruptModal 在屏。"""
        from orca.iface.cli.screens.interrupt_modal import InterruptModal

        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)
        # action_interrupt 需要 _orchestrator 非 None；mock 一个避免它早 return。
        app._orchestrator = object()  # truthy 占位

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("ctrl+g")
                await pilot.pause()
                await pilot.pause()  # @work action_interrupt 异步起
                assert isinstance(app.screen, InterruptModal)
        run_async(scenario())

    def test_ctrl_g_without_orchestrator_warns_no_modal(self, tmp_path):
        """无编排在跑（_orchestrator=None）→ Ctrl+G 不弹 modal，LogStream 写提示。"""
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)
        # _orchestrator 默认 None（未起编排）

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("ctrl+g")
                await pilot.pause()
                await pilot.pause()
                # 不弹 InterruptModal
                from orca.iface.cli.screens.interrupt_modal import InterruptModal
                assert not isinstance(app.screen, InterruptModal)
        run_async(scenario())

    def test_app_dispatches_interrupt_resolved_to_logstream(self, tmp_path):
        """注入 interrupt_resolved 事件 → LogStream 显示描述（SPEC §4.3 全部入日志）。"""
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                app._dispatch_to_widgets(_event(
                    "interrupt_resolved",
                    {"interrupt_id": "i1", "action": "continue",
                     "guidance": "用更保守的方案", "resolved_by": "cli"},
                    node="cfg", session_id="sess-x",
                ))
                await pilot.pause()
                await pilot.pause()  # RichLog 异步 flush
                text = _flatten_strips(app.query_one(LogStream).lines)
                assert "interrupt continue" in text
                assert "用更保守的方案" in text
        run_async(scenario())

    def test_app_dispatches_interrupt_requested_to_logstream(self, tmp_path):
        """注入 interrupt_requested 事件 → LogStream 显示描述。"""
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                app._dispatch_to_widgets(_event(
                    "interrupt_requested",
                    {"interrupt_id": "i1", "node": "cfg", "run_id": "r1",
                     "session_id": "sess-x", "elapsed_at_request": 12.3,
                     "source": "cli"},
                    node="cfg", session_id="sess-x",
                ))
                await pilot.pause()
                await pilot.pause()
                text = _flatten_strips(app.query_one(LogStream).lines)
                assert "interrupt requested" in text
                assert "cfg" in text
        run_async(scenario())

    def test_app_dispatches_prompt_rendered_to_logstream(self, tmp_path):
        """注入 prompt_rendered 事件 → LogStream 显示描述（含 preview 截断）。"""
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                app._dispatch_to_widgets(_event(
                    "prompt_rendered",
                    {"node": "cfg", "session_id": "sess-x",
                     "preview": "任务描述...\n\n[User Guidance]\n- 用更保守的方案"},
                    node="cfg", session_id="sess-x",
                ))
                await pilot.pause()
                await pilot.pause()
                text = _flatten_strips(app.query_one(LogStream).lines)
                assert "prompt rendered" in text
        run_async(scenario())

    def test_node_started_tracks_current_node_and_session(self, tmp_path):
        """node_started 事件 → app 追踪 _current_node / _current_session_id（action_interrupt 用）。"""
        wf = _load(_linear_workflow(tmp_path))
        app = _app(tmp_path, wf)

        async def scenario():
            async with app.run_test() as pilot:
                await pilot.pause()
                app._dispatch_to_widgets(_event(
                    "node_started", {"kind": "agent"},
                    node="cfg", session_id="sess-running",
                ))
                await pilot.pause()
                assert app._current_node == "cfg"
                assert app._current_session_id == "sess-running"
                assert app._node_started_at is not None
        run_async(scenario())
