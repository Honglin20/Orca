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
