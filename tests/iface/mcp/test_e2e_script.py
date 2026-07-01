"""test_e2e_script.py —— E2E-1：纯 script workflow 全链路（SPEC phase-10 §6.3 / §D5.2）。

闭环验证（SPEC §6.3 E2E-1）：
    start_workflow (demo_linear.yaml) → 轮询 get_task_status → completed
        → 断言 output 含 "step_c"（c 节点 echo 输出）

核心 invariants（SPEC §6.3 硬约束）：
  1. **真 stdio round-trip**：spawn ``orca mcp`` 子进程 + ``stdio_client`` 连接（硬 invariant 7）。
  2. **不 mock 引擎**：真 ``Orchestrator`` + 真 tape + 真 EventBus（硬 invariant 1）。
  3. **完整链路**：start → poll → terminal，断言终态字段（output / status）（硬 invariant 3）。
  4. **fail loud**：timeout / 拿不到 expected status / 字段缺失 → raise（硬 invariant 8）。

CI 可跑（无 API key / 无 claude 依赖，demo_linear 纯 script）。
"""

from __future__ import annotations

from pathlib import Path

from tests.iface.mcp.conftest import (
    orca_mcp_subprocess,
    parse_tool_result,
    poll_to_terminal,
    run_async,
)

# workflow 路径（项目根 examples/demo_linear.yaml）。
DEMO_LINEAR = str(Path(__file__).resolve().parents[3] / "examples" / "demo_linear.yaml")


def test_e2e_script_workflow_completes_and_output_has_step_c(tmp_path):
    """E2E-1：demo_linear.yaml start → poll → completed，output.result 含 'step_c'。

    完整链路（SPEC §6.3 E2E-1）：
      1. start_workflow → task_id + status="running" + _hint 引导 poll
      2. 轮询 get_task_status → completed
      3. 断言 output 是 ``{"result": "step_c\\n"}``（demo_linear.outputs 模板 ``{{ c.output.stdout }}``）
      4. 断言 _hint 在 completed 时正确（"Output is in the `output` field."）
    """
    async def scenario() -> dict:
        async with orca_mcp_subprocess(tmp_path) as session:
            # 1. start_workflow（demo_linear 纯 script，零 token / 零 claude）
            start_raw = await session.call_tool(
                "start_workflow", {"yaml_path": DEMO_LINEAR}
            )
            start = parse_tool_result(start_raw)
            assert start["status"] == "running", f"start 应返回 running，实得 {start}"
            task_id = start["task_id"]
            assert task_id, "task_id 非空"
            assert "get_task_status" in start["_hint"]

            # 2. 轮询到终态（demo_linear 秒级，30s 是充裕兜底）
            return await poll_to_terminal(session, task_id, deadline_s=30.0)

    final = run_async(scenario())

    # 3. 断言终态
    assert final["status"] == "completed", (
        f"demo_linear 应秒级 completed，实得 status={final['status']!r}"
    )
    # 4. output 字段：demo_linear.outputs.result = "{{ c.output.stdout }}" → "step_c\n"
    output = final.get("output")
    assert output is not None, f"completed 应有 output，实得 {final}"
    assert "result" in output, f"output 应有 result 键，实得 {output}"
    assert "step_c" in str(output["result"]), (
        f"output.result 应含 step_c（c 节点 echo 输出），实得 {output['result']!r}"
    )
    # 5. _hint 在 completed 时正确（SPEC §2.3）
    assert "Output is in" in final["_hint"], (
        f"completed _hint 应指向 output 字段，实得 {final['_hint']!r}"
    )

