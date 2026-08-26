"""test_e2e_opencode_flush.py —— E2E-5：opencode stdio flush 兼容（SPEC phase-10 §6.3 / §D5.6）。

闭环验证（SPEC §6.3 E2E-5）：
    并发发 5 个 ``list_tools``（不等 reply）→ server 逐条 flush 应答 → 每个收到完整 tool list

核心 invariant（SPEC §6.3 硬约束 7 + opencode flush bug #21516）：
  - **真 stdio round-trip**：spawn ``orca mcp`` 子进程 + ``stdio_client`` 连接。
  - **逐条 flush**：连续 5 个 tool call 并发（``asyncio.gather``），server 必须逐条 flush
    应答，不批量 / 不丢（规避 opencode #21516：客户端不等 reply 批量发时 server 必须配合）。

in-session v5 §6.2：期望 8 工具（Discovery 3 + Lifecycle 3 + History 2，删 get_agent_prompt）；
start_workflow E2E 用 catalog 模式（cwd 指向含 ``workflows/`` 的 tmp_path）。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from tests.iface.mcp.conftest import (
    orca_mcp_subprocess,
    run_async,
)

# demo_linear 路径（项目根 examples/demo_linear.yaml）。
DEMO_LINEAR = str(Path(__file__).resolve().parents[3] / "examples" / "demo_linear.yaml")

# 期望的 8 工具名（Discovery 3 + Lifecycle 3 + History 2，in-session v5 §6.2 删 get_agent_prompt）。
_EXPECTED_TOOLS = {
    "list_workflows",
    "describe_workflow",
    "list_agents",
    "start_workflow",
    "get_task_status",
    "cancel_task",
    "get_task_history",
    "get_agent",
}


def test_e2e_concurrent_list_tools_all_receive_full_tool_list(tmp_path):
    """E2E-5：并发 5 个 list_tools，每个都收到完整 8 工具（不批量 / 不丢）。

    SPEC §6.3 E2E-5：规避 opencode #21516，server 必须逐条 flush 应答。
    """
    async def scenario() -> list[set[str]]:
        async with orca_mcp_subprocess(tmp_path) as session:
            # 并发发 5 个 list_tools（不等 reply，asyncio.gather 同时发起）
            results = await asyncio.gather(
                *[session.list_tools() for _ in range(5)]
            )
            # 每个结果取 tool name 集合
            return [{t.name for t in r.tools} for r in results]

    tool_sets = run_async(scenario())

    assert len(tool_sets) == 5, f"应收到 5 个 reply，实得 {len(tool_sets)}"
    for i, names in enumerate(tool_sets):
        assert _EXPECTED_TOOLS.issubset(names), (
            f"reply {i} 应含 8 工具，实得 {names}（缺 {_EXPECTED_TOOLS - names}）"
        )


def test_e2e_concurrent_start_workflow_yields_unique_task_ids(tmp_path):
    """附加：并发 5 个 start_workflow（catalog 模式），各 task_id 唯一（不串）。

    v4：start_workflow 用 ``name`` 而非 ``yaml_path``。需要在 subprocess 的 cwd 下有
    ``workflows/demo_linear.yaml``。tmp_path/workflows/ 放一份，cwd=tmp_path。
    """
    # 在 tmp_path/workflows/ 放 demo_linear.yaml（catalog 扫描目录）
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DEMO_LINEAR, workflows_dir / "demo_linear.yaml")

    async def scenario() -> list[str]:
        async with orca_mcp_subprocess(tmp_path, cwd=tmp_path) as session:
            # 并发发 5 个 start_workflow（name-based，v4 catalog 模式）
            results = await asyncio.gather(*[
                session.call_tool("start_workflow", {"name": "demo_linear"})
                for _ in range(5)
            ])
            task_ids: list[str] = []
            for r in results:
                from tests.iface.mcp.conftest import parse_tool_result
                d = parse_tool_result(r)
                # v4 Result 信封：{ok: True, data: {task_id, status: "running"}, _hint}
                assert d["ok"] is True, f"start_workflow 应 ok=True，实得 {d}"
                assert d["data"]["status"] == "running", f"start 应返回 running，实得 {d}"
                task_ids.append(d["data"]["task_id"])
            return task_ids

    task_ids = run_async(scenario())

    assert len(set(task_ids)) == 5, (
        f"5 个并发 start_workflow 应返回 5 个唯一 task_id，实得 {task_ids}"
    )
