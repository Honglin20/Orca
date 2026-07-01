"""test_e2e_cross_shell.py —— E2E-3：跨壳 race（MCP + Web，SPEC phase-10 §6.3 / §D5.4）。

闭环验证（SPEC §6.3 E2E-3 + 三通道竞速机制，SPEC §6 / shells-design-draft §6）：
    start → fire gate → Web 答（赢家）→ MCP 答（晚到，输家）→ 广播写 tape →
    后续 get_task_status 不再见 needs_decision

核心 invariants（SPEC §6.3 硬约束）：
  2. **不 mock gates**：用真 ``HumanGateHandler.request`` / ``resolve``（硬 invariant 2）。
  5. **跨壳 race 端到端**（硬 invariant 5）：Web 赢 / MCP 输 + 广播写 tape + 后续 poll
     看不到 needs_decision。这是三通道竞速（SPEC §6 / 草稿 §6）的端到端验证。
  8. **fail loud**：source 错 / MCP 误赢 / 广播丢失 → raise。

跨壳模拟（不 spawn 真 Web HTTP server，直接调 ``handler.resolve(source="web")``）：
    ``handler.resolve`` 是 phase-6 已稳定的竞速入口（threading.Lock 保护 first-wins），
    Web 路由 ``POST /gate/respond`` 内部就是调它（见 ``orca/iface/web/routes/gate.py``）。
    直接调 ``resolve(source="web")`` 等价模拟 Web 答，零额外 HTTP 栈。
"""

from __future__ import annotations

import asyncio

from tests.iface.mcp.conftest import (
    fire_gate,
    make_inprocess_harness,
    run_async,
    slow_workflow,
)


def test_e2e_cross_shell_web_wins_mcp_loses(tmp_path):
    """E2E-3：Web 答（source="web"）赢，MCP 答（source="mcp"）晚到输。

    SPEC §6.3 E2E-3 + §6 / shells-design-draft §6 三通道竞速端到端验证。

    步骤（SPEC §D5.4）：
      1. fire gate（gate_task create）
      2. ``await asyncio.sleep(0.2)`` 让 requested 写 tape
      3. **Web 答**：直接调 ``handler.resolve(gate_id, "no", source="web")`` → True（赢家）
      4. **MCP 晚到**：调 ``server.tool_resolve_gate(...)`` → ``ok=False``（输家）
      5. gate_task 收到 ``("no", "web")``（赢家 source）
      6. 后续 get_task_status 不再见 needs_decision（resolved 写 tape）

    timing 不严格（handler.resolve 有 threading.Lock 保证 first-wins）。
    """
    yaml_path = slow_workflow(tmp_path)
    harness = make_inprocess_harness(tmp_path)

    async def scenario() -> tuple[bool, dict, tuple[str, str], dict]:
        try:
            run_id = await harness.manager.start_run(str(yaml_path))
            await asyncio.sleep(0.3)  # 等 _run_with_sem 进 sem + gate_handler.start

            # 1. fire gate（后台 task）
            gate_task = asyncio.create_task(
                fire_gate(harness, run_id, "test_gate_e2e_3")
            )
            await asyncio.sleep(0.2)  # 等 requested 写 tape

            handle = harness.manager.get_handle(run_id)
            assert handle is not None

            # 2. Web 答（直接调 handler.resolve，模拟 POST /gate/respond）
            web_ok = handle.gate_handler.resolve(
                "test_gate_e2e_3", "no", source="web"
            )
            assert web_ok is True, "Web 先答应赢（first-wins）"

            # 3. MCP 晚到（调 server.tool_resolve_gate，source 写死 "mcp"）
            mcp_result = await harness.server.tool_resolve_gate(
                task_id=run_id, gate_id="test_gate_e2e_3", decision="yes"
            )

            # 4. gate_task 收到 ("no", "web")（赢家 source）
            answer, source = await asyncio.wait_for(gate_task, timeout=5.0)

            # 5. 后续 get_task_status 不再见 needs_decision（resolved 写 tape）
            await asyncio.sleep(0.1)  # 让 broadcaster emit resolved 写 tape
            final_status = await harness.server.tool_get_task_status(task_id=run_id)
            return web_ok, mcp_result, (answer, source), final_status
        finally:
            await harness.aclose()

    web_ok, mcp_result, (answer, source), final = run_async(scenario())

    # 断言：跨壳 race 端到端
    assert web_ok is True, "Web 先答，应赢"
    assert mcp_result["ok"] is False, "MCP 晚到，应输（ok=False）"
    assert "already resolved" in mcp_result["_hint"].lower(), (
        f"MCP 输家 _hint 应含 'already resolved'，实得 {mcp_result['_hint']!r}"
    )
    assert answer == "no", f"赢家 answer 应生效（Web 答 'no'），实得 {answer!r}"
    assert source == "web", (
        f"赢家 source 应是 'web'（三通道竞速 first-wins），实得 {source!r}"
    )
    assert final["status"] != "needs_decision", (
        f"resolved 写 tape 后 status 不应再见 needs_decision，实得 {final['status']!r}"
    )
