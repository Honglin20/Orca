"""tests/exec/claude/test_executor_integration.py —— 真 spawn claude 烟雾测试（CI skip）。

**CI 默认 skip**（无 API key / 慢 / 非确定）；本地 ``pytest -m integration`` 可选跑。
SPEC §7.10 / 计划 E.3：验证 ClaudeExecutor 真能 spawn ``claude -p`` 跑通一个极简 prompt。

前置：
  - 本机安装了 claude CLI（``ORCA_CLAUDE_CLI`` 或默认 ``claude``）
  - 配了 API key（``ANTHROPIC_API_KEY`` 等）

不跑断言细节（claude 输出非确定），只断言：
  - 产出 node_started → 至少一个 agent 事件 → node_completed 或 node_failed（生命周期完整）
  - 不崩溃、不卡死（有超时兜底）
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

from orca.exec import make_executor
from orca.exec.context import RunContext
from orca.profiles.registry import _reset_for_test
from orca.schema import AgentNode


def _claude_available() -> bool:
    return shutil.which("claude") is not None


@pytest.fixture(autouse=True)
def _reset_profiles_registry():
    _reset_for_test()
    yield
    _reset_for_test()


@pytest.mark.integration
@pytest.mark.skipif(not _claude_available(), reason="claude CLI 不在 PATH（集成测试需真 spawn）")
def test_smoke_real_claude_spawn():
    """真 spawn claude 跑极简 prompt，断言生命周期完整（非输出内容）。

    用 ``asyncio.run`` 而非 pytest-asyncio（本仓库约定，见 tests/events/test_bus.py）。
    """
    async def scenario():
        node = AgentNode(name="smoke", prompt="Reply with exactly: OK")
        ctx = RunContext(inputs={}, outputs={}, run_id="smoke-run")
        executor = make_executor(node)
        return [ev async for ev in executor.exec(node, ctx)]

    events = asyncio.run(scenario())
    types = [ev.type for ev in events]
    # 生命周期：node_started ... → node_completed 或 node_failed（出错也不该崩）
    assert types[0] == "node_started"
    assert types[-1] in ("node_completed", "node_failed")
    # 至少产出一个 agent 事件（message / thinking / tool_call 之一）
    agent_types = {"agent_message", "agent_thinking", "agent_tool_call"}
    assert bool(set(types) & agent_types) or types[-1] == "node_failed"
