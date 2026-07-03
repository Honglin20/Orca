"""tests/run/test_orchestrator_agent_tools.py —— Orchestrator 的 AgentToolsMcpServer 生命周期（phase 11 §5.1）。

INTENT：
  - 注入 agent_tools_server → run() 期间 server.started（port 非 None）；run 结束 server stopped。
  - server=None（默认）→ run() 不起 server，既有行为不变（向后兼容，回归保护）。
  - workflow 失败时 server 也被 stop（finally 兜底，无 leaked task）。

策略：用 script node workflow（零 token，确定性）+ FakeServer（spy start/stop 次数 + 不真起 SSE）。
"""

from __future__ import annotations

from pathlib import Path

from orca.run.orchestrator import Orchestrator
from orca.schema import Route, ScriptNode, Workflow

from tests.run.conftest import make_bus, run_async


def _linear_script_wf() -> Workflow:
    """entry a → $end（单 script node，零 token，确定性，不 spawn claude）。"""
    return Workflow(
        name="demo_at",
        entry="a",
        nodes=[ScriptNode(name="a", command="echo ok", routes=[Route(to="$end")])],
        outputs={},
    )


class _FakeAgentToolsServer:
    """AgentToolsMcpServer 替身：spy start/stop（不真起 SSE server，测试快且无端口依赖）。

    暴露 orchestrator 用到的方法（start/stop）；orchestrator 不调其他方法（_dispatch 走
    script node 分支，不经 agent_tools_server），故 spy 这两个就够。
    """

    def __init__(self) -> None:
        self.start_count = 0
        self.stop_count = 0
        self._port: int | None = None

    async def start(self) -> int:
        self.start_count += 1
        self._port = 7422
        return self._port

    async def stop(self) -> None:
        self.stop_count += 1
        self._port = None


def test_orchestrator_starts_and_stops_agent_tools_server(tmp_path):
    """注入 server → run() 起 server（start_count=1），结束 stop（stop_count=1）。"""
    bus, _ = make_bus(tmp_path)
    server = _FakeAgentToolsServer()
    orch = Orchestrator(_linear_script_wf(), bus, agent_tools_server=server)

    state = run_async(orch.run())

    assert state.status == "completed"
    assert server.start_count == 1, f"server.start 应调一次，实际 {server.start_count}"
    assert server.stop_count == 1, f"server.stop 应调一次（run 收尾），实际 {server.stop_count}"


def test_orchestrator_no_agent_tools_server_when_none(tmp_path):
    """server=None → run() 正常完成，无异常（向后兼容，回归保护）。"""
    bus, _ = make_bus(tmp_path)
    orch = Orchestrator(_linear_script_wf(), bus, agent_tools_server=None)

    state = run_async(orch.run())

    assert state.status == "completed"
    # node 正常完成（script executor 不依赖 server）
    assert state.node_status == {"a": "done"}


def test_orchestrator_stops_server_on_workflow_failure(tmp_path):
    """workflow 失败时 server 也被 stop（finally 兜底，无 leaked task）。

    用一个必失败的 script node（Jinja2 引用未定义变量 → ExecError(phase=render) → node_failed
    → workflow_failed），断言 server.stop 仍被调。ScriptExecutor 不因 exit_code != 0 失败
    （它把 exit_code 存进 output），故用 render 错误触发失败。
    """
    bus, _ = make_bus(tmp_path)
    server = _FakeAgentToolsServer()
    wf = Workflow(
        name="demo_at_fail",
        entry="a",
        # 引用未定义变量 → StrictUndefined → ExecError(phase=render)
        nodes=[ScriptNode(name="a", command="echo {{ undefined_var }}", routes=[Route(to="$end")])],
        outputs={},
    )
    orch = Orchestrator(wf, bus, agent_tools_server=server)

    state = run_async(orch.run())

    assert state.status == "failed", f"expected failed, got {state.status}"
    assert server.start_count == 1
    assert server.stop_count == 1, "workflow 失败时 server.stop 也必须被调（finally 兜底）"


def test_orchestrator_propagates_agent_tools_start_failure(tmp_path):
    """agent_tools_server.start() 失败 → fail loud（workflow_failed，不让死端口静默流到 spawn）。

    INTENT（铁律 12）：start 失败必须可见，不能半静默降级（否则每个 agent spawn 时 write_config
    拿不到 port 才崩，错误延后且定位困难）。_FakeAgentToolsServer.start 抛 OSError 模拟 bind 失败。
    """
    bus, _ = make_bus(tmp_path)

    class _BoomServer(_FakeAgentToolsServer):
        async def start(self) -> int:
            self.start_count += 1
            raise OSError("simulated bind failure (port occupied)")

    server = _BoomServer()
    orch = Orchestrator(_linear_script_wf(), bus, agent_tools_server=server)

    state = run_async(orch.run())

    # start 抛 → workflow_failed（fail loud）；stop 仍被 finally 调（幂等兜底）
    assert state.status == "failed", f"start 失败应 fail loud，实际 {state.status}"
    assert server.start_count == 1
    assert server.stop_count == 1, "start 失败后 stop 仍被 finally 调（兜底）"


# ── §11.4 run-scoped unregister：两 run 顺序跑不泄漏 session 路由 ──────────────


class _RegistrySpyServer(_FakeAgentToolsServer):
    """带共享 registry 的 fake server：spy unregister_run 调用 + 暴露 registry 内容。

    用于「两 run 顺序跑」隔离测试：orchestrator._stop_agent_tools 调 unregister_run(run_id)
    清该 run 的 session 路由。验证 run-A 的 register 不应残留到 run-B 开始时（防泄漏）。
    """

    def __init__(self, registry) -> None:  # type: ignore[no-untyped-def]
        super().__init__()
        self._registry = registry
        self.unregister_run_calls: list[str] = []

    def register_session(self, *, session_id: str, run_id: str, node: str) -> None:
        self._registry.register(session_id, run_id, node)

    def unregister_run(self, run_id: str) -> int:
        self.unregister_run_calls.append(run_id)
        return self._registry.unregister_run(run_id)


def test_orchestrator_unregisters_run_sessions_after_completion_no_leak(tmp_path):
    """两 run 顺序跑（共享 registry）→ 每跑完一个 run，其 session 路由被清，无跨 run 泄漏。

    INTENT（§11.4 run-scoped unregister 契约）：ClaudeExecutor 每个 agent node spawn 都
    register 一条 (session_id → run_id, node)。run 结束时 orchestrator 调
    ``unregister_run(run_id)`` 批清。若清理漏了（如 orchestrator 没在 run() finally 调、
    或 unregister_run 按 node 而非 run 批清），session 路由会跨 run 累积 → 内存泄漏 +
    路由表污染（run-A 的 session 残留可能让 run-B 的 hook 桥查到错 run）。

    验证：模拟两个 run 顺序跑（共享同一 server + registry），每个 run 内模拟一个 agent
    node spawn（register 一条）。run-A 跑完后 registry 应不含 run-A 条目；run-B 跑完后
    应不含 run-B 条目；最终 registry 全空。
    """
    from orca.gates.context_registry import SessionContextRegistry
    from orca.schema import AgentNode, Route, Workflow

    shared_registry = SessionContextRegistry()
    server = _RegistrySpyServer(shared_registry)

    from orca.exec.interface import Executor
    from orca.schema import Event

    class _RegisteringFakeAgent(Executor):
        """模拟 ClaudeExecutor：exec 时 register_session 一条，再 yield 完成事件。

        让 orchestrator._dispatch 真正走 agent 分支（node.kind=="agent"），同时模拟
        ClaudeExecutor.exec 内 spawn 前的 register_session 债（§11.4）。
        """

        def __init__(self, run_id: str, node: str, server_ref: _RegistrySpyServer):
            self._run_id = run_id
            self._node = node
            self._server_ref = server_ref

        async def exec(self, node, ctx):  # type: ignore[override]
            import time
            import uuid
            from collections.abc import AsyncIterator

            sid = uuid.uuid4().hex
            # 模拟 ClaudeExecutor.exec 的 register_session（spawn 前，§11.4 时机前移）
            self._server_ref.register_session(
                session_id=sid, run_id=self._run_id, node=self._node,
            )
            yield Event(
                seq=0, type="node_started", timestamp=time.time(),
                node=self._node, session_id=sid, data={"kind": "agent"},
            )
            yield Event(
                seq=0, type="node_completed", timestamp=time.time(),
                node=self._node, session_id=sid,
                data={"output": {"ok": True}, "elapsed": 0.0},
            )

    def _agent_wf(run_id_label: str) -> Workflow:
        return Workflow(
            name=f"demo_at_{run_id_label}",
            entry="a",
            nodes=[AgentNode(name="a", prompt="p", routes=[Route(to="$end")])],
            outputs={},
        )

    from orca.exec import factory as factory_mod

    orig_make_executor = factory_mod.make_executor

    # ── run-A ──────────────────────────────────────────────────────────────
    bus_a, _ = make_bus(tmp_path / "runA")
    fake_a = _RegisteringFakeAgent("run-A-id", "a", server)
    factory_mod.make_executor = lambda node, agent_tools_server=None, bus=None, **kwargs: fake_a
    try:
        orch_a = Orchestrator(
            _agent_wf("A"), bus_a, run_id="run-A-id", agent_tools_server=server,
        )
        state_a = run_async(orch_a.run())
    finally:
        factory_mod.make_executor = orig_make_executor

    assert state_a.status == "completed"
    # run-A 跑完：unregister_run("run-A-id") 被调，registry 不含 run-A 条目
    assert "run-A-id" in server.unregister_run_calls, (
        f"run-A 结束应调 unregister_run('run-A-id')，实际 calls={server.unregister_run_calls}"
    )
    leaked_a = [
        sid for sid, loc in shared_registry._map.items() if loc.run_id == "run-A-id"
    ]
    assert not leaked_a, (
        f"run-A 跑完后其 session 路由应被清空（防泄漏），实际残留：{leaked_a}"
    )

    # ── run-B（同一 server + registry，模拟「daemon 长跑多 run」场景）─────────
    bus_b, _ = make_bus(tmp_path / "runB")
    fake_b = _RegisteringFakeAgent("run-B-id", "a", server)
    factory_mod.make_executor = lambda node, agent_tools_server=None, bus=None, **kwargs: fake_b
    try:
        orch_b = Orchestrator(
            _agent_wf("B"), bus_b, run_id="run-B-id", agent_tools_server=server,
        )
        state_b = run_async(orch_b.run())
    finally:
        factory_mod.make_executor = orig_make_executor

    assert state_b.status == "completed"
    assert "run-B-id" in server.unregister_run_calls
    # 最终：registry 全空（两个 run 的 session 路由都被各自 unregister_run 清掉）
    assert not shared_registry._map, (
        f"两 run 都跑完后 registry 应全空（无跨 run 泄漏），实际残留："
        f"{dict(shared_registry._map)}"
    )
    # 两个 run 各调一次 unregister_run（run-scoped，非全局清空）
    assert server.unregister_run_calls.count("run-A-id") == 1
    assert server.unregister_run_calls.count("run-B-id") == 1
