"""test_e2e_opencode_flush.py —— E2E-5：opencode stdio flush 兼容（SPEC phase-10 §6.3 / §D5.6）。

闭环验证（SPEC §6.3 E2E-5）：
    并发发 5 个 ``list_tools``（不等 reply）→ server 逐条 flush 应答 → 每个收到完整 tool list

核心 invariant（SPEC §6.3 硬约束 7 + opencode flush bug #21516）：
  - **真 stdio round-trip**：spawn ``orca mcp`` 子进程 + ``stdio_client`` 连接。
  - **逐条 flush**：连续 5 个 tool call 并发（``asyncio.gather``），server 必须逐条 flush
    应答，不批量 / 不丢（规避 opencode #21516：客户端不等 reply 批量发时 server 必须配合）。

为何用 ``list_tools`` 而非真 tool call：list_tools 是 MCP 协议层最轻的调用（无副作用、
无状态），纯粹测 stdio flush 通路。若 list_tools 并发能逐条收到，tool call 必然也能
（同走 ``stdio_server`` 写路径）。

附加：并发 5 个 ``start_workflow``（不同 yaml copy）断言各 task_id 唯一——进一步验证
并发 stdio round-trip 不串。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from tests.iface.mcp.conftest import (
    orca_mcp_subprocess,
    parse_tool_result,
    run_async,
)

# demo_linear 路径（项目根 examples/demo_linear.yaml）。
DEMO_LINEAR = str(Path(__file__).resolve().parents[3] / "examples" / "demo_linear.yaml")

# 期望的四件套 tool 名（D4 后 FastMCP 注册四个）。
_EXPECTED_TOOLS = {"start_workflow", "get_task_status", "resolve_gate", "cancel_task"}


def test_e2e_concurrent_list_tools_all_receive_full_tool_list(tmp_path):
    """E2E-5：并发 5 个 list_tools，每个都收到完整四件套（不批量 / 不丢）。

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
            f"reply {i} 应含四件套，实得 {names}（缺 {_EXPECTED_TOOLS - names}）"
        )


def test_e2e_concurrent_start_workflow_yields_unique_task_ids(tmp_path):
    """附加：并发 5 个 start_workflow（不同 yaml copy），各 task_id 唯一（不串）。

    进一步验证并发 stdio round-trip 不串：每个 start_workflow 应返回独立 task_id。
    用 5 份 yaml copy（避免文件锁 / 路径冲突；其实同文件也没事，但显式 copy 更稳）。
    """
    yaml_copies: list[str] = []
    for i in range(5):
        p = tmp_path / f"demo_{i}.yaml"
        shutil.copyfile(DEMO_LINEAR, p)
        yaml_copies.append(str(p))

    async def scenario() -> list[str]:
        async with orca_mcp_subprocess(tmp_path) as session:
            # 并发发 5 个 start_workflow
            results = await asyncio.gather(*[
                session.call_tool("start_workflow", {"yaml_path": yp})
                for yp in yaml_copies
            ])
            task_ids: list[str] = []
            for r in results:
                d = parse_tool_result(r)
                assert d["status"] == "running", f"start 应返回 running，实得 {d}"
                task_ids.append(d["task_id"])
            return task_ids

    task_ids = run_async(scenario())

    assert len(set(task_ids)) == 5, (
        f"5 个并发 start_workflow 应返回 5 个唯一 task_id，实得 {task_ids}"
    )
