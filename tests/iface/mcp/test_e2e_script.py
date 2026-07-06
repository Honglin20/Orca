"""test_e2e_script.py —— E2E-1：纯 script workflow 全链路（SPEC phase-10 §6.3 / §D5.2）。

闭环验证（SPEC §6.3 E2E-1）：
    start_workflow (demo_linear via catalog) → 轮询 get_task_status → completed
        → 断言 output 含 "step_c"（c 节点 echo 输出）

核心 invariants（SPEC §6.3 硬约束）：
  1. **真 stdio round-trip**：spawn ``orca mcp`` 子进程 + ``stdio_client`` 连接（硬 invariant 7）。
  2. **不 mock 引擎**：真 ``Orchestrator`` + 真 tape + 真 EventBus（硬 invariant 1）。
  3. **完整链路**：start → poll → terminal，断言终态字段（output / status）（硬 invariant 3）。
  4. **fail loud**：timeout / 拿不到 expected status / 字段缺失 → raise（硬 invariant 8）。

v4 更新（2026-07-07）：
  - start_workflow 改为 ``name`` 参数（catalog 模式）；subprocess cwd = tmp_path，
    内含 ``workflows/demo_linear.yaml``。
  - Result 信封：start 返 ``{ok: True, data: {task_id, status}, _hint}``；
    get_task_status 返 ``{ok: True, data: {status, output?, ...}, _hint}``。

CI 可跑（无 API key / 无 claude 依赖，demo_linear 纯 script）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from tests.iface.mcp.conftest import (
    orca_mcp_subprocess,
    parse_tool_result,
    run_async,
)

# workflow 源路径（项目根 examples/demo_linear.yaml）。
DEMO_LINEAR_SRC = Path(__file__).resolve().parents[3] / "examples" / "demo_linear.yaml"


async def _poll_to_terminal_v4(session, task_id: str, *, deadline_s: float) -> dict:
    """v4 Result 信封版轮询：get_task_status → 解包 data → 到终态返 data dict。"""
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    last: dict = {}
    while loop.time() < deadline:
        result = await session.call_tool("get_task_status", {"task_id": task_id})
        envelope = parse_tool_result(result)
        assert envelope["ok"] is True, f"get_task_status 应 ok=True，实得 {envelope}"
        last = envelope["data"]
        if last.get("status") in ("completed", "failed", "cancelled"):
            # 附带 _hint（在信封层，不在 data）
            last["_hint"] = envelope.get("_hint", "")
            return last
        await asyncio.sleep(0.5)
    raise TimeoutError(
        f"task {task_id} 未在 {deadline_s}s 内到终态（last status={last.get('status')!r}）"
    )


def test_e2e_script_workflow_completes_and_output_has_step_c(tmp_path):
    """E2E-1：demo_linear start → poll → completed，output.result 含 'step_c'。

    完整链路（SPEC §6.3 E2E-1）：
      1. start_workflow(name="demo_linear") → task_id + status="running"
      2. 轮询 get_task_status → completed
      3. 断言 output 是 ``{"result": "step_c\\n"}``
      4. 断言 _hint 在 completed 时正确
    """
    # catalog 模式：在 tmp_path/workflows/ 放 demo_linear.yaml
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DEMO_LINEAR_SRC, workflows_dir / "demo_linear.yaml")

    async def scenario() -> dict:
        async with orca_mcp_subprocess(tmp_path, cwd=tmp_path) as session:
            # 1. start_workflow（v4 name-based catalog）
            start_raw = await session.call_tool(
                "start_workflow", {"name": "demo_linear"}
            )
            start_env = parse_tool_result(start_raw)
            assert start_env["ok"] is True, f"start 应 ok=True，实得 {start_env}"
            start = start_env["data"]
            assert start["status"] == "running", f"start 应返回 running，实得 {start}"
            task_id = start["task_id"]
            assert task_id, "task_id 非空"
            assert "get_task_status" in start_env["_hint"]

            # 2. 轮询到终态
            return await _poll_to_terminal_v4(session, task_id, deadline_s=30.0)

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
    # 5. _hint 在 completed 时正确（SPEC §2.5）
    assert "Output is in" in final["_hint"], (
        f"completed _hint 应指向 output 字段，实得 {final['_hint']!r}"
    )
