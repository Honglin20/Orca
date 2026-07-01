"""test_e2e_gate.py —— E2E-2：gate 全流程（SPEC phase-10 §6.3 / §D5.3）。

闭环验证（SPEC §6.3 E2E-2）：
    start → fire 合成 gate → get_task_status=needs_decision → resolve_gate →
    后续 get_task_status 不再见 needs_decision

核心 invariants（SPEC §6.3 硬约束）：
  2. **不 mock gates**：用真 ``HumanGateHandler.request`` / ``resolve``（硬 invariant 2）。
  3. **完整链路**：start → gate → resolve → 终态（硬 invariant 3）。
  4. **source="mcp" 端到端**：``gate_task`` 收到的 ``(answer, source)`` 中 source **必须** 是
     "mcp"（硬 invariant 4，SPEC §0.1 第五条端到端兑现）。
  8. **fail loud**：拿不到 expected status / source 错 → raise。

gate 触发不依赖 claude（用 in-process harness 直接调 ``handler.request``，
``conftest.fire_gate`` 共用 helper，DRY）。
"""

from __future__ import annotations

import asyncio

from tests.iface.mcp.conftest import (
    fire_gate,
    make_inprocess_harness,
    run_async,
    slow_workflow,
)


def test_e2e_gate_full_flow_resolve_via_mcp(tmp_path):
    """E2E-2：gate 全流程 start → fire → needs_decision → resolve_gate → source="mcp"。

    SPEC §6.3 E2E-2 + §0.1 第五条 source="mcp" 端到端验证。
    """
    yaml_path = slow_workflow(tmp_path)
    harness = make_inprocess_harness(tmp_path)

    async def scenario() -> tuple[dict, dict, tuple[str, str], dict]:
        try:
            # 1. 起 slow run（后台跑 ``sleep 10``，gate 在测试侧 fire，与 run 并发）
            run_id = await harness.manager.start_run(str(yaml_path))

            # 2. 等 orchestrator 进 sem + gate_handler.start()（_run_with_sem 内）。
            #    gate_handler 未 start 时 request 仍 await，但 resolve 入队会 warning（无 broadcaster）。
            #    给 0.3s 让 _run_with_sem 进 sem + gate_handler.start。
            await asyncio.sleep(0.3)

            # 3. fire 合成 gate（后台 task；request 写 tape 后 await fut）
            gate_task = asyncio.create_task(
                fire_gate(harness, run_id, "test_gate_e2e_2")
            )
            # 等 requested 事件写 tape（request 内 ``await bus.emit`` 完成）
            await asyncio.sleep(0.2)

            # 4. get_task_status 应见 needs_decision（gate 详情齐全）
            status_raw = await harness.server.tool_get_task_status(task_id=run_id)
            # 5. resolve_gate via MCP（source 写死 "mcp"，SPEC §0.1 第五条）
            resolve_raw = await harness.server.tool_resolve_gate(
                task_id=run_id, gate_id="test_gate_e2e_2", decision="yes"
            )
            # 6. gate_task 收到 (answer, source)；source 必须是 "mcp"（硬 invariant 4）
            answer, source = await asyncio.wait_for(gate_task, timeout=5.0)
            # 7. 后续 get_task_status 不再见 needs_decision（resolved 写 tape）
            await asyncio.sleep(0.1)  # 让 broadcaster emit resolved 写 tape
            final_status = await harness.server.tool_get_task_status(task_id=run_id)
            return status_raw, resolve_raw, (answer, source), final_status
        finally:
            await harness.aclose()

    status, resolve, (answer, source), final = run_async(scenario())

    # 断言（async 外，便于 fail 时看清哪个断言挂）
    assert status["status"] == "needs_decision", (
        f"fire gate 后应 needs_decision，实得 {status['status']!r}"
    )
    gate = status["gate"]
    assert gate is not None, "needs_decision 时 gate 字段应填充"
    assert gate["gate_id"] == "test_gate_e2e_2"
    assert gate["prompt"] == "批准部署？"
    assert gate["options"] == ["yes", "no"]
    assert gate["context"] == {"env": "prod"}
    assert resolve["ok"] is True, "MCP 是赢家，应 ok=True"
    assert resolve["status"] == "running"
    assert answer == "yes"
    assert source == "mcp", (
        f"source 必须是 'mcp'（SPEC §0.1 第五条端到端），实得 {source!r}"
    )
    assert final["status"] != "needs_decision", (
        f"resolved 后 status 不应再见 needs_decision，实得 {final['status']!r}"
    )
