"""test_e2e_claude.py —— E2E-4：真 claude workflow（SPEC phase-10 §6.3 / §D5.5）。

闭环验证（SPEC §6.3 E2E-4）：
    start_workflow (demo_task.yaml) → 轮询 get_task_status → completed
        → assert output 含 "DONE"（claude 遵循 prompt 回 DONE）

**@pytest.mark.integration**（CI skip，本地 ``pytest -m integration`` 跑）：
    需 ``ANTHROPIC_API_KEY`` + ``claude`` CLI。缺一即 skip（fail loud 但不阻塞 CI）。

核心 invariants（SPEC §6.3 硬约束）：
  1. **不 mock 引擎**：真 ``Orchestrator`` + 真 claude call + 真 tape + 真 EventBus。
  7. **真 stdio round-trip**：spawn ``orca mcp`` 子进程 + ``stdio_client`` 连接。
  8. **fail loud**：超时 / 非 completed / output 不含 DONE → raise。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.iface.mcp.conftest import (
    orca_mcp_subprocess,
    parse_tool_result,
    poll_to_terminal,
    run_async,
)

# demo_task 路径（项目根 examples/demo_task.yaml，真 claude agent）。
DEMO_TASK = str(Path(__file__).resolve().parents[3] / "examples" / "demo_task.yaml")


def _skip_if_no_claude_env():
    """缺 API key / claude CLI → pytest.skip（不阻塞 CI，fail loud 但不 fail）。

    SPEC §D5.5 易踩坑提醒：用 ``os.environ.get`` + ``shutil.which`` 检查，缺一即 skip。
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY 未设，跳过真 claude E2E（SPEC §D5.5）")
    if not shutil.which("claude"):
        pytest.skip("claude CLI 不可用，跳过真 claude E2E（SPEC §D5.5）")


@pytest.mark.integration
def test_e2e_claude_workflow_completes_and_output_has_DONE(tmp_path):
    """E2E-4：demo_task.yaml start → poll → completed，output.reply 含 'DONE'。

    SPEC §6.3 E2E-4：真 claude call，验证完整链路（start → poll → terminal → output）。
    inputs.task = "回复 DONE"，claude 应遵循 prompt 回 DONE。
    """
    _skip_if_no_claude_env()

    async def scenario() -> dict:
        async with orca_mcp_subprocess(tmp_path) as session:
            # start_workflow（demo_task.yaml 真 claude agent）
            start_raw = await session.call_tool(
                "start_workflow",
                {"yaml_path": DEMO_TASK, "inputs": {"task": "回复 DONE"}},
            )
            start = parse_tool_result(start_raw)
            assert start["status"] == "running", f"start 应返回 running，实得 {start}"
            task_id = start["task_id"]
            assert task_id, "task_id 非空"

            # 轮询到终态（真 claude 慢，120s 充裕兜底，1s 间隔）
            return await poll_to_terminal(session, task_id, deadline_s=120.0, interval_s=1.0)

    final = run_async(scenario())

    assert final["status"] == "completed", (
        f"demo_task 应 completed，实得 status={final['status']!r}；"
        f"error={final.get('error')!r}"
    )
    output = final.get("output")
    assert output is not None, f"completed 应有 output，实得 {final}"
    # demo_task.outputs.reply = "{{ worker.output }}"（claude 整个 stdout）
    reply = str(output.get("reply", ""))
    assert "DONE" in reply.upper(), (
        f"output.reply 应含 'DONE'（claude 遵循 prompt），实得 {reply!r}"
    )

