"""tests/run/test_orchestrator_terminate.py —— terminate step 集成（orchestrator 端）。

覆盖 INTENT：
  - status=success → emit workflow_completed，outputs 用 terminate.outputs（**不**走 wf.outputs）
  - status=failed → emit workflow_failed{error_type=WorkflowTerminated, message=reason, node=...}
  - terminate 触达后**不**评估 routes（下游 node 不执行 / route_taken 不 emit）

策略：用 FakeExecutor 注入确定输出（不 spawn 真 claude）。``make_executor`` monkeypatch
让普通 agent node 走 FakeExecutor，terminate node 走真 TerminateExecutor（验证分派正确）。
"""

from __future__ import annotations

from orca.run.orchestrator import Orchestrator
from orca.schema import (
    AgentNode,
    Route,
    ScriptNode,
    TerminateNode,
    Workflow,
)
from tests.run.conftest import FakeExecutor, make_bus, run_async


def _make_orch(wf, tmp_path, *, monkeypatch=None) -> Orchestrator:
    """构造 Orchestrator，monkeypatch make_executor 让 agent 走 FakeExecutor。

    terminate node 不被 patch 覆盖（走真 TerminateExecutor）—— 通过判断 node.kind 实现。

    orchestrator 在 ``_execute_agent`` 内 ``from orca.exec.factory import make_executor``
    （function-local import，每次调用读 module 属性），故 patch ``orca.exec.factory``
    模块的 ``make_executor`` 属性即可生效。
    """
    bus, _ = make_bus(tmp_path)
    if monkeypatch is not None:
        from orca.exec import factory as factory_mod
        from orca.exec.factory import make_executor as real_make_executor

        def patched(node, *args, **kw):
            if node.kind == "agent":
                return FakeExecutor.produces({"category": "unknown"}, node_name=node.name)
            if node.kind == "terminate":
                from orca.exec.terminate import TerminateExecutor
                return TerminateExecutor()
            return real_make_executor(node, *args, **kw)

        monkeypatch.setattr(factory_mod, "make_executor", patched)
    return Orchestrator(wf, bus)


# ── status=success → workflow_completed with terminate.outputs ──────────────


def test_terminate_success_emits_workflow_completed_with_terminate_outputs(tmp_path, monkeypatch):
    """status=success → workflow_completed，outputs=terminate.outputs（覆盖 wf.outputs）。

    关键 INTENT：terminate 的 outputs **替代** wf.outputs（terminate 节点是终态出口），
    不走 _evaluate_outputs(wf.outputs) 路径。
    """
    wf = Workflow(
        name="terminate_success",
        entry="a",
        nodes=[
            AgentNode(name="a", prompt="p", routes=[Route(to="t")]),
            TerminateNode(
                name="t",
                status="success",
                outputs={"terminated_by": "{{ a.output.category }}"},
            ),
        ],
        # wf.outputs 故意设一组无关字段，验证 terminate 时被覆盖
        outputs={"should_not_appear": "{{ a.output.category }}"},
    )
    orch = _make_orch(wf, tmp_path, monkeypatch=monkeypatch)
    state = run_async(orch.run())

    assert state.status == "completed"
    completed_ev = [e for e in orch.bus.tape.replay() if e.type == "workflow_completed"][0]
    # outputs 来自 terminate.outputs，**不**是 wf.outputs
    assert completed_ev.data["outputs"] == {"terminated_by": "unknown"}
    assert "should_not_appear" not in completed_ev.data["outputs"]


# ── status=failed → workflow_failed{WorkflowTerminated} ──────────────────────


def test_terminate_failed_emits_workflow_failed_with_reason(tmp_path, monkeypatch):
    """status=failed → workflow_failed{kind: business_agent, message=reason, node=t}。

    关键 INTENT：terminate failed **不是** executor 失败（executor 正常 node_completed），
    是业务声明；phase-11 v2.1 / ADR §4.1 决策 1.2：TerminateNode failed 路径翻译为
    ``node_failed{kind=BUSINESS_AGENT}``，故 ``data.kind`` 是 ``business_agent``。
    """
    wf = Workflow(
        name="terminate_failed",
        entry="a",
        nodes=[
            AgentNode(name="a", prompt="p", routes=[Route(to="reject")]),
            TerminateNode(
                name="reject",
                status="failed",
                reason="rejected {{ a.output.category }}",
            ),
        ],
    )
    orch = _make_orch(wf, tmp_path, monkeypatch=monkeypatch)
    state = run_async(orch.run())

    assert state.status == "failed"
    failed_ev = [e for e in orch.bus.tape.replay() if e.type == "workflow_failed"][0]
    assert failed_ev.data["kind"] == "business_agent"
    assert failed_ev.data["message"] == "rejected unknown"
    assert failed_ev.data["node"] == "reject"


# ── terminate 触达后不评估 routes ───────────────────────────────────────────


def test_terminate_does_not_evaluate_routes_or_run_downstream(tmp_path, monkeypatch):
    """terminate 触达后不评估 routes：route_taken 不 emit，下游 node 不执行。

    INTENT：terminate 短路控制流——即使下游 node 已定义也不会被执行；即便 terminate 的
    routes 字段有内容（虽然 compile 层会拦，runtime 也需保证不调 router.resolve）。
    """
    wf = Workflow(
        name="terminate_short_circuit",
        entry="a",
        nodes=[
            AgentNode(name="a", prompt="p", routes=[Route(to="t")]),
            TerminateNode(name="t", status="success"),
            # 下游 node：terminate 触达时不应执行
            AgentNode(name="should_not_run", prompt="p", routes=[Route(to="$end")]),
        ],
    )
    # routes 求值前就短路，故 a→t 之后不该走到 should_not_run
    # 给 a 设 routes=[to=t]，给 t 不设 routes（terminate 不评估）
    orch = _make_orch(wf, tmp_path, monkeypatch=monkeypatch)
    state = run_async(orch.run())

    # terminate 触达 → workflow_completed（success），should_not_run 没被执行
    assert state.status == "completed"
    assert "a" in state.node_status
    assert "t" in state.node_status
    # should_not_run 不在 node_status（没 node_started）—— 证明 routes 没被评估
    assert "should_not_run" not in state.node_status

    # route_taken 事件：a→t 出现，但 t→ 后续不出现（terminate 不 emit route_taken）
    route_events = [e for e in orch.bus.tape.replay() if e.type == "route_taken"]
    route_froms = {e.data["from"] for e in route_events}
    assert "a" in route_froms  # a→t 正常 emit
    assert "t" not in route_froms  # terminate 不 emit route_taken（控制流短路）


# ── ScriptNode → terminate（real shell，端到端最小例）─────────────────────────


def test_e2e_script_to_terminate_failed(tmp_path):
    """端到端：script（真 echo）→ terminate(failed) → workflow_failed{WorkflowTerminated}。

    不 monkeypatch make_executor，让 script + terminate 走真路径（script 跑真 shell，
    terminate 走真 TerminateExecutor）。
    """
    wf = Workflow(
        name="terminate_e2e",
        entry="classifier",
        nodes=[
            ScriptNode(
                name="classifier",
                command='echo \'{"category": "unknown"}\'',
                parse_json=True,
                routes=[Route(to="reject")],
            ),
            TerminateNode(
                name="reject",
                status="failed",
                reason="rejected category {{ classifier.output.json.category }}",
            ),
        ],
    )
    bus, _ = make_bus(tmp_path)
    orch = Orchestrator(wf, bus)
    state = run_async(orch.run())

    assert state.status == "failed"
    failed_ev = [e for e in orch.bus.tape.replay() if e.type == "workflow_failed"][0]
    assert failed_ev.data["kind"] == "business_agent"
    assert failed_ev.data["message"] == "rejected category unknown"
    assert failed_ev.data["node"] == "reject"
    # classifier 已执行（script 真 echo）
    assert state.node_status.get("classifier") == "done"
    assert state.node_status.get("reject") == "done"


def test_e2e_script_to_terminate_success(tmp_path):
    """端到端：script → terminate(success) → workflow_completed with terminate.outputs。"""
    wf = Workflow(
        name="terminate_e2e_success",
        entry="classifier",
        nodes=[
            ScriptNode(
                name="classifier",
                command='echo \'{"category": "ok"}\'',
                parse_json=True,
                routes=[Route(to="finish")],
            ),
            TerminateNode(
                name="finish",
                status="success",
                outputs={"final_category": "{{ classifier.output.json.category }}"},
            ),
        ],
    )
    bus, _ = make_bus(tmp_path)
    orch = Orchestrator(wf, bus)
    state = run_async(orch.run())

    assert state.status == "completed"
    completed_ev = [e for e in orch.bus.tape.replay() if e.type == "workflow_completed"][0]
    assert completed_ev.data["outputs"] == {"final_category": "ok"}