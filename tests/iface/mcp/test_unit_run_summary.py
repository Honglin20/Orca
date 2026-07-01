"""test_unit_run_summary.py —— RunManager.run_summary / _derive_mcp_status 单元测试
（SPEC phase-10 §3.3 / §D1.4）。

覆盖意图（非仅行为）：
  - **五种 status 各一例**：running / needs_decision / completed / failed / cancelled。
  - **needs_decision 优先于 running**：哪怕 handle.status=running，tape 有 pending gate
    → status="needs_decision"，gate 字段填充。
  - **completed → output 填充**：workflow_completed.data.outputs 进 output 字段。
  - **failed → error 填充**：handle.error 进 error 字段。
  - **cancelled → 其它字段 None**：纯终态，无 gate/output/error。
  - **未知 run_id → None**：MCP get_task_status 据此返回 status="unknown"。
  - **不含 _hint**：summary 是通用 dict，_hint 是 MCP 层加（§9.10）。

做法：用真 RunManager + demo_linear_yaml 起 run。Orchestrator.run 用 ``hold.wait()``
阻塞以保持 run 在 running（teardown 不会先跑），手动 emit 生命周期事件 + 设 status
模拟各状态。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from orca.iface.web.run_manager import RunManager

from tests.iface.web.conftest import demo_linear_yaml, make_manager, run_async


# ── helpers ──────────────────────────────────────────────────────────────────


async def _emit_requested(
    handle,
    *,
    gate_id: str = "g1",
    prompt: str = "批准？",
    options: list[str] | None = None,
    context: dict | None = None,
    source: str = "tool_permission",
    node: str | None = "n1",
    session_id: str | None = "sess-1",
) -> None:
    """往 handle.bus emit human_decision_requested（写 tape，模拟 node 触发 gate）。"""
    await handle.bus.emit(
        "human_decision_requested",
        data={
            "gate_id": gate_id,
            "prompt": prompt,
            "options": options if options is not None else ["allow", "deny"],
            "context": context if context is not None else {"tool": "Bash"},
            "source": source,
            "run_id": handle.run_id,
            "node": node,
        },
        node=node,
        session_id=session_id,
    )


def _patch_hold_orchestrator(hold: asyncio.Event):
    """返回一个永不返回的 Orchestrator.run patch（用 hold.wait() 阻塞）。

    让 _run_with_sem 卡在 orch.run() —— teardown 不会先跑，tape/bus 保持 open，
    测试可以安全 emit 事件 + 设 status。
    """

    async def hang(self):
        await hold.wait()

    return patch("orca.run.orchestrator.Orchestrator.run", hang)


# ── 五种 status（SPEC D1.4）──────────────────────────────────────────────────


def test_run_summary_running(tmp_path):
    """running：handle.status="running"，无 gate → status="running"，gate/output/error=None。"""
    manager = make_manager(tmp_path)

    async def go():
        yaml_path = demo_linear_yaml(tmp_path)
        hold = asyncio.Event()
        with _patch_hold_orchestrator(hold):
            run_id = await manager.start_run(str(yaml_path), {}, None, None)
            await asyncio.sleep(0.05)  # 进 sem + status=running
            summary = manager.run_summary(run_id)
            assert summary is not None
            assert summary["status"] == "running"
            assert summary["task_id"] == run_id
            assert summary["gate"] is None
            assert summary["output"] is None
            assert summary["error"] is None
            # 放行 + shutdown（teardown 走正常路径，不留 leaked task）
            hold.set()
        await manager.shutdown()

    run_async(go())


def test_run_summary_needs_decision(tmp_path):
    """needs_decision：tape 有 pending gate → status="needs_decision"，gate 字段填充。"""
    manager = make_manager(tmp_path)

    async def go():
        yaml_path = demo_linear_yaml(tmp_path)
        hold = asyncio.Event()
        with _patch_hold_orchestrator(hold):
            run_id = await manager.start_run(str(yaml_path), {}, None, None)
            await asyncio.sleep(0.05)  # 进 running
            handle = manager.get_handle(run_id)
            await _emit_requested(
                handle,
                gate_id="g_decide",
                prompt="批准部署？",
                options=["yes", "no"],
                context={"tool": "Bash", "args": "deploy"},
                source="tool_permission",
                node="deploy",
            )
            summary = manager.run_summary(run_id)
            assert summary is not None
            assert summary["status"] == "needs_decision"
            gate = summary["gate"]
            assert gate is not None
            assert gate["gate_id"] == "g_decide"
            assert gate["prompt"] == "批准部署？"
            assert gate["options"] == ["yes", "no"]
            assert gate["context"] == {"tool": "Bash", "args": "deploy"}
            # output/error 在 needs_decision 时为 None
            assert summary["output"] is None
            assert summary["error"] is None
            hold.set()
        await manager.shutdown()

    run_async(go())


def test_run_summary_completed(tmp_path):
    """completed：tape 有 workflow_completed → output 字段从 data.outputs 取。

    outputs 来自 ``workflow_completed.data.outputs``（reducer 不进 context），
    run_summary 扫 tape 取最后 workflow_completed 的 outputs。
    """
    manager = make_manager(tmp_path)

    async def go():
        yaml_path = demo_linear_yaml(tmp_path)
        hold = asyncio.Event()
        with _patch_hold_orchestrator(hold):
            run_id = await manager.start_run(str(yaml_path), {}, None, None)
            await asyncio.sleep(0.05)
            handle = manager.get_handle(run_id)
            await handle.bus.emit(
                "workflow_started",
                data={"inputs": {}, "node_count": 2, "entry": "a", "workflow_name": "demo"},
            )
            await handle.bus.emit(
                "node_completed",
                data={"elapsed": 0.1, "output": {"stdout": "step_a"}},
                node="a",
            )
            # workflow_completed 的 outputs 进 data.outputs（reducer 不投影进 context）
            await handle.bus.emit(
                "workflow_completed",
                data={"elapsed": 0.5, "outputs": {"result": "step_a"}},
            )
            # 手动设 status=completed（_run_with_sem 卡在 hold，未自动转）
            handle.status = "completed"
            summary = manager.run_summary(run_id)
            assert summary is not None
            assert summary["status"] == "completed"
            # output 字段从 workflow_completed.data.outputs 取
            assert summary["output"] == {"result": "step_a"}
            assert summary["gate"] is None
            assert summary["error"] is None
            hold.set()
        await manager.shutdown()

    run_async(go())


def test_run_summary_failed(tmp_path):
    """failed：handle.status="failed" + handle.error="..." → error 字段填充。"""
    manager = make_manager(tmp_path)

    async def go():
        yaml_path = demo_linear_yaml(tmp_path)
        hold = asyncio.Event()
        with _patch_hold_orchestrator(hold):
            run_id = await manager.start_run(str(yaml_path), {}, None, None)
            await asyncio.sleep(0.05)
            handle = manager.get_handle(run_id)
            await handle.bus.emit("workflow_started", data={"workflow_name": "demo"})
            # workflow_failed 让 tape 派生 status=failed
            await handle.bus.emit(
                "workflow_failed",
                data={"error_type": "ValueError", "message": "boom"},
                node="a",
            )
            handle.status = "failed"
            handle.error = "ValueError: boom"
            summary = manager.run_summary(run_id)
            assert summary is not None
            assert summary["status"] == "failed"
            assert summary["error"] == "ValueError: boom"
            assert summary["gate"] is None
            assert summary["output"] is None
            hold.set()
        await manager.shutdown()

    run_async(go())


def test_run_summary_cancelled(tmp_path):
    """cancelled：handle.status="cancelled" + tape 有 workflow_cancelled → status="cancelled"。"""
    manager = make_manager(tmp_path)

    async def go():
        yaml_path = demo_linear_yaml(tmp_path)
        hold = asyncio.Event()
        with _patch_hold_orchestrator(hold):
            run_id = await manager.start_run(str(yaml_path), {}, None, None)
            await asyncio.sleep(0.05)
            handle = manager.get_handle(run_id)
            await handle.bus.emit("workflow_started", data={"workflow_name": "demo"})
            # workflow_cancelled 让 tape 派生 status=cancelled
            await handle.bus.emit(
                "workflow_cancelled",
                data={"reason": "user_cancelled"},
            )
            handle.status = "cancelled"
            summary = manager.run_summary(run_id)
            assert summary is not None
            assert summary["status"] == "cancelled"
            # cancelled 时 gate/output/error 全 None（纯终态）
            assert summary["gate"] is None
            assert summary["output"] is None
            assert summary["error"] is None
            hold.set()
        await manager.shutdown()

    run_async(go())


# ── 反例 / 边界 ─────────────────────────────────────────────────────────────


def test_run_summary_unknown_run_returns_none(tmp_path):
    """未知 run_id → None（MCP get_task_status 据此返回 status="unknown"）。"""
    manager = make_manager(tmp_path)
    assert manager.run_summary("nonexistent-id") is None


def test_run_summary_has_no_hint_field(tmp_path):
    """summary 不含 ``_hint`` 字段（§9.10：_hint 是 MCP 层加，不是 run_summary 的产物）。"""
    manager = make_manager(tmp_path)

    async def go():
        yaml_path = demo_linear_yaml(tmp_path)
        hold = asyncio.Event()
        with _patch_hold_orchestrator(hold):
            run_id = await manager.start_run(str(yaml_path), {}, None, None)
            await asyncio.sleep(0.05)
            summary = manager.run_summary(run_id)
            assert summary is not None
            assert "_hint" not in summary
            hold.set()
        await manager.shutdown()

    run_async(go())


# ── _derive_mcp_status 纯逻辑分支 ────────────────────────────────────────────


def test_derive_mcp_status_branches():
    """_derive_mcp_status 五分支：completed/failed/cancelled 直接返回，
    running + pending_gate → needs_decision，否则 running。"""
    f = RunManager._derive_mcp_status
    assert f("completed", []) == "completed"
    assert f("failed", []) == "failed"
    assert f("cancelled", []) == "cancelled"
    # 有 pending gate → needs_decision（优先于 running）
    from orca.gates.types import HumanGate

    g = HumanGate(
        id="g1",
        prompt="?",
        context={},
        source="tool_permission",
        run_id="r1",
        node=None,
    )
    assert f("running", [g]) == "needs_decision"
    assert f("queued", [g]) == "needs_decision"
    # 无 pending gate → running
    assert f("running", []) == "running"
    assert f("queued", []) == "running"
