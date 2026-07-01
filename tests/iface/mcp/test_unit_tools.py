"""test_unit_tools.py —— MCP 三件套工具单元测试（SPEC phase-10 §D3.5）。

D3 阶段覆盖 start_workflow / get_task_status / resolve_gate（D4 在此文件追加 cancel_task 测）：
  - **start_workflow**：mock manager.start_run 返回 fake run_id，断言返回 task_id +
    status="running" + _hint 引导 poll。
  - **start_workflow 不阻塞**（HandleId pattern）：真 RunManager + monkey-patch
    _run_with_sem sleep 10s，start_workflow 应在 1s 内返回。
  - **get_task_status 五种 status**：mock run_summary 五种值，验证 _hint 内容 + 字段。
  - **get_task_status unknown**：run_summary 返回 None，断言 status="unknown" + _hint。
  - **resolve_gate 赢家**：mock handler.resolve 返回 True，断言 source="mcp" 被传入。
  - **resolve_gate 输家**：mock 返回 False，断言 _hint 含 "already resolved"。
  - **resolve_gate 未知 task_id**：manager.get_handle 返回 None，断言 status="unknown"。
  - **docstring 含强指令**：grep tool 函数 docstring 含 "Always call get_task_status"。
  - **source="mcp" 写死**：resolve_gate 调 handler.resolve 第三参数必须 == "mcp"。
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from orca.iface.mcp.server import OrcaMcpServer
from orca.iface.web.run_manager import RunManager

from tests.iface.web.conftest import run_async


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_server_with_mock_manager() -> tuple[OrcaMcpServer, MagicMock]:
    """构造 OrcaMcpServer，manager 是 MagicMock（方法可独立 mock）。"""
    mock_manager = MagicMock(spec=RunManager)
    return OrcaMcpServer(mock_manager), mock_manager  # type: ignore[arg-type]


# ── start_workflow ───────────────────────────────────────────────────────────


def test_start_workflow_returns_task_id_and_hint():
    """start_workflow 返回 {task_id, status:"running", _hint}。"""
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.start_run = AsyncMock(return_value="run_abc123")

    result = run_async(
        server.tool_start_workflow(yaml_path="examples/demo_linear.yaml")
    )

    assert result["task_id"] == "run_abc123"
    assert result["status"] == "running"
    assert "_hint" in result
    assert "run_abc123" in result["_hint"]
    assert "get_task_status" in result["_hint"]
    mock_manager.start_run.assert_awaited_once_with(
        "examples/demo_linear.yaml", None, None, None
    )


def test_start_workflow_passes_all_params():
    """start_workflow 透传 inputs / task / max_iter 给 manager.start_run。"""
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.start_run = AsyncMock(return_value="r1")

    run_async(
        server.tool_start_workflow(
            yaml_path="wf.yaml",
            inputs={"k": "v"},
            task="do it",
            max_iter=5,
        )
    )

    mock_manager.start_run.assert_awaited_once_with(
        "wf.yaml", {"k": "v"}, "do it", 5
    )


def test_start_workflow_does_not_block_on_slow_orchestrator(tmp_path):
    """HandleId pattern（SPEC §0.1 第三条）：start_workflow 秒级返回 run_id，即便编排
    后台 task 还在跑（甚至卡在 gate）。

    做法：用真 RunManager + monkey-patch _run_with_sem sleep 10s（模拟长跑）。
    start_workflow 应 <1s 返回 run_id（不等编排）。
    """
    from orca.iface.web.run_manager import RunManager as _RM

    yaml_path = tmp_path / "wf.yaml"
    yaml_path.write_text(
        """
name: gated
description: 测试用
entry: a
nodes:
  - name: a
    kind: script
    command: "echo hi"
    routes:
      - to: $end
""",
        encoding="utf-8",
    )

    manager = _RM(runs_dir=tmp_path / "runs")

    async def slow_run_with_sem(handle, inputs, task, max_iter):
        await asyncio.sleep(10)  # 模拟 10s 长跑
        handle.status = "completed"

    manager._run_with_sem = slow_run_with_sem  # type: ignore[assignment]

    server = OrcaMcpServer(manager)
    start = time.monotonic()
    result = run_async(server.tool_start_workflow(yaml_path=str(yaml_path)))
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"start_workflow 阻塞了 {elapsed:.2f}s（应秒级返回，不等编排）"
    assert result["status"] == "running"
    assert result["task_id"].startswith("gated-")

    run_async(manager.shutdown(timeout=1.0))


# ── get_task_status ──────────────────────────────────────────────────────────


def _summary(**overrides):
    """构造 run_summary 返回 dict（默认 running）。"""
    base = {
        "task_id": "r1",
        "status": "running",
        "current_node": "step_b",
        "progress": "2/5",
        "cost": 0.01,
        "elapsed": 12.3,
        "gate": None,
        "output": None,
        "error": None,
    }
    base.update(overrides)
    return base


def test_get_task_status_running():
    """running → _hint 显式建议结束 turn（防 CC 循环检测，§2.3）。"""
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.run_summary = MagicMock(return_value=_summary(status="running"))

    result = run_async(server.tool_get_task_status(task_id="r1"))

    assert result["status"] == "running"
    assert "End your turn" in result["_hint"]
    assert result["progress"] == "2/5"
    # 不含 dag/chart_json（SPEC §0.1 第七条）
    assert "dag" not in result
    assert "chart_json" not in result


def test_get_task_status_needs_decision():
    """needs_decision → _hint 引导 resolve_gate，gate 字段填充。"""
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.run_summary = MagicMock(
        return_value=_summary(
            status="needs_decision",
            gate={
                "gate_id": "g1",
                "prompt": "批准？",
                "options": ["allow", "deny"],
                "context": {"tool": "Bash"},
            },
        )
    )

    result = run_async(server.tool_get_task_status(task_id="r1"))

    assert result["status"] == "needs_decision"
    assert "resolve_gate" in result["_hint"]
    assert result["gate"]["gate_id"] == "g1"


def test_get_task_status_completed():
    """completed → _hint 指 output 字段。"""
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.run_summary = MagicMock(
        return_value=_summary(status="completed", output={"result": "ok"})
    )

    result = run_async(server.tool_get_task_status(task_id="r1"))

    assert result["status"] == "completed"
    assert "Output is in" in result["_hint"]
    assert result["output"] == {"result": "ok"}


def test_get_task_status_failed():
    """failed → _hint 指 error 字段。"""
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.run_summary = MagicMock(
        return_value=_summary(status="failed", error="ValueError: boom")
    )

    result = run_async(server.tool_get_task_status(task_id="r1"))

    assert result["status"] == "failed"
    assert "Error is in" in result["_hint"]
    assert result["error"] == "ValueError: boom"


def test_get_task_status_cancelled():
    """cancelled → _hint 简短确认。"""
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.run_summary = MagicMock(return_value=_summary(status="cancelled"))

    result = run_async(server.tool_get_task_status(task_id="r1"))

    assert result["status"] == "cancelled"
    assert "cancelled" in result["_hint"].lower()


def test_get_task_status_unknown_returns_hint():
    """未知 task_id → run_summary 返回 None → status="unknown" + _hint。"""
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.run_summary = MagicMock(return_value=None)

    result = run_async(server.tool_get_task_status(task_id="ghost"))

    assert result["status"] == "unknown"
    assert result["task_id"] == "ghost"
    assert "_hint" in result


# ── resolve_gate ─────────────────────────────────────────────────────────────


def test_resolve_gate_winner_source_mcp():
    """赢家：handler.resolve 返回 True，source="mcp" 被传入（SPEC §0.1 第五条）。"""
    server, mock_manager = _make_server_with_mock_manager()
    mock_handle = MagicMock()
    mock_handle.gate_handler.resolve = MagicMock(return_value=True)
    mock_manager.get_handle = MagicMock(return_value=mock_handle)

    result = run_async(
        server.tool_resolve_gate(task_id="r1", gate_id="g1", decision="allow")
    )

    assert result["ok"] is True
    assert result["status"] == "running"
    assert "accepted" in result["_hint"].lower()
    # 关键断言：source="mcp" 写死（SPEC §0.1 第五条）
    mock_handle.gate_handler.resolve.assert_called_once_with(
        "g1", "allow", source="mcp"
    )


def test_resolve_gate_loser_hint_already_resolved():
    """输家：handler.resolve 返回 False，_hint 含 'already resolved'。"""
    server, mock_manager = _make_server_with_mock_manager()
    mock_handle = MagicMock()
    mock_handle.gate_handler.resolve = MagicMock(return_value=False)
    mock_manager.get_handle = MagicMock(return_value=mock_handle)

    result = run_async(
        server.tool_resolve_gate(task_id="r1", gate_id="g1", decision="deny")
    )

    assert result["ok"] is False
    assert result["status"] == "needs_decision"
    assert "already resolved" in result["_hint"].lower()


def test_resolve_gate_unknown_task_id():
    """未知 task_id：manager.get_handle 返回 None → status="unknown"。"""
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.get_handle = MagicMock(return_value=None)

    result = run_async(
        server.tool_resolve_gate(task_id="ghost", gate_id="g1", decision="yes")
    )

    assert result["ok"] is False
    assert result["status"] == "unknown"


# ── docstring 强指令（SPEC §2.4）─────────────────────────────────────────────


def test_tool_docstrings_contain_chain_instruction():
    """每个 tool 的 docstring 必须含显式 chain 调指令（SPEC §2.4）。"""
    assert "Always call get_task_status" in OrcaMcpServer.tool_start_workflow.__doc__
    assert "Always call" in OrcaMcpServer.tool_get_task_status.__doc__
    assert "Always call get_task_status" in OrcaMcpServer.tool_resolve_gate.__doc__


# ── FastMCP 注册三件套（D3）───────────────────────────────────────────────────


def test_fastmcp_lists_three_tools():
    """``OrcaMcpServer`` 构造后 FastMCP 注册三件套（D3 阶段；D4 加 cancel_task 后变四件套）。"""
    m = RunManager()
    server = OrcaMcpServer(m)
    tools = server._mcp._tool_manager._tools
    names = set(tools.keys())
    assert names == {"start_workflow", "get_task_status", "resolve_gate"}
