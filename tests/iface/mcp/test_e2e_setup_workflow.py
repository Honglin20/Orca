"""test_e2e_setup_workflow.py —— E2E-2：setup workflow 全链路（SPEC phase-10 §6.3）。

闭环验证（SPEC §6.3 E2E-2，CI 可跑版）：
    合成 setup workflow（has_setup=true）→
    start_workflow without setup_outputs → business_config error →
    get_agent_prompt → 借 prompt →
    start_workflow with setup_outputs → poll → completed

核心 invariants：
  - 三重杠杆 B 拦截（start_workflow setup_required → kind=business_config）
  - get_agent_prompt 借 setup agent prompt 文本
  - setup_outputs 校验通过 → workflow 启动 → 完成
  - Result 信封贯穿（所有 tool 返 ``{ok, data?, error?, _hint?}``）

CI 可跑（无 API key / 无 claude 依赖，setup + execute 全 script）。
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


SETUP_WORKFLOW_YAML = """
name: setup_e2e
description: E2E setup workflow（collector + deploy，全 script）
setup:
  - name: collector
    kind: agent
    prompt: |
      Ask the user for a target host and port.
      Return structured output: {host: string, port: integer}.
    output_schema:
      type: object
      properties:
        host: {type: string}
        port: {type: integer}
      required: [host, port]
entry: deploy
nodes:
  - name: deploy
    kind: script
    command: "echo 'deploy to {{ setup.collector.output.host }}:{{ setup.collector.output.port }}'"
    routes:
      - to: $end
outputs:
  result: "{{ deploy.output.stdout }}"
"""


async def _poll_to_completed(session, task_id: str, *, deadline_s: float = 30.0) -> dict:
    """轮询 get_task_status 到 completed（v4 Result 信封版）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    last: dict = {}
    while loop.time() < deadline:
        result = await session.call_tool("get_task_status", {"task_id": task_id})
        env = parse_tool_result(result)
        assert env["ok"] is True, f"get_task_status 应 ok=True，实得 {env}"
        last = env["data"]
        if last.get("status") in ("completed", "failed", "cancelled"):
            last["_hint"] = env.get("_hint", "")
            return last
        await asyncio.sleep(0.5)
    raise TimeoutError(
        f"task {task_id} 未在 {deadline_s}s 内到终态（last={last!r}）"
    )


def test_e2e_setup_workflow_three_lever_b_and_completion(tmp_path):
    """E2E-2：setup workflow 全链路（三重杠杆 B → prompt → start with outputs → completed）。

    步骤（SPEC §6.3 E2E-2 CI 版）：
      1. list_workflows → has_setup=true 标记
      2. start_workflow without setup_outputs → ok=False + kind=business_config
      3. describe_workflow → 返 setup agent 元信息
      4. get_agent_prompt → 借 prompt 文本
      5. start_workflow with setup_outputs → ok=True + task_id
      6. poll → completed
    """
    # 在 tmp_path/workflows/ 放 setup workflow（catalog 模式）
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / "setup_e2e.yaml").write_text(
        SETUP_WORKFLOW_YAML, encoding="utf-8"
    )

    async def scenario() -> dict:
        async with orca_mcp_subprocess(tmp_path, cwd=tmp_path) as session:
            # 1. list_workflows → has_setup=true（杠杆 A）
            lw_raw = await session.call_tool("list_workflows", {})
            lw = parse_tool_result(lw_raw)
            assert lw["ok"] is True
            setups = [w for w in lw["data"]["workflows"] if w["name"] == "setup_e2e"]
            assert len(setups) == 1
            assert setups[0]["has_setup"] is True

            # 2. start_workflow without setup_outputs → business_config（杠杆 B）
            bad_raw = await session.call_tool(
                "start_workflow", {"name": "setup_e2e"}
            )
            bad = parse_tool_result(bad_raw)
            assert bad["ok"] is False
            assert bad["error"]["kind"] == "business_config"
            assert "setup" in bad["error"]["message"].lower()
            # 引导性 _hint
            assert "get_agent_prompt" in bad["_hint"]

            # 3. describe_workflow → setup agent 元信息
            dw_raw = await session.call_tool(
                "describe_workflow", {"name": "setup_e2e"}
            )
            dw = parse_tool_result(dw_raw)
            assert dw["ok"] is True
            assert dw["data"]["has_setup"] is True
            assert len(dw["data"]["setup"]) == 1
            assert dw["data"]["setup"][0]["name"] == "collector"

            # 4. get_agent_prompt → 借 prompt
            gap_raw = await session.call_tool(
                "get_agent_prompt",
                {"name": "collector", "workflow": "setup_e2e"},
            )
            gap = parse_tool_result(gap_raw)
            assert gap["ok"] is True
            assert "host" in gap["data"]["prompt"]

            # 5. start_workflow with valid setup_outputs → 启动
            start_raw = await session.call_tool(
                "start_workflow",
                {
                    "name": "setup_e2e",
                    "setup_outputs": {
                        "collector": {"host": "prod.example.com", "port": 22}
                    },
                },
            )
            start = parse_tool_result(start_raw)
            assert start["ok"] is True
            assert start["data"]["status"] == "running"
            task_id = start["data"]["task_id"]

            # 6. poll → completed
            final = await _poll_to_completed(session, task_id)
            return final

    final = run_async(scenario())

    # 终态断言
    assert final["status"] == "completed", (
        f"setup_e2e 应 completed，实得 status={final['status']!r}"
    )
    output = final.get("output")
    assert output is not None, f"completed 应有 output，实得 {final}"
    assert "result" in output, f"output 应有 result 键，实得 {output}"
    # 注入验证：setup_outputs 真渗到 render——deploy 命令消费了 {{ setup.collector.output.* }}
    assert "prod.example.com:22" in output["result"], (
        f"setup_outputs 未注入 render，result={output['result']!r}"
    )
    assert "deploy to" in str(output["result"]), (
        f"output.result 应含 deploy 输出，实得 {output['result']!r}"
    )
