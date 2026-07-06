"""test_unit_tools.py —— MCP v4 9 工具单元测试（SPEC phase-10 §2 / §2.4b v2）。

覆盖 9 工具（Discovery 4 + Lifecycle 3 + History 2）的核心 intent：

**Discovery 组**：
  - list_workflows 返 has_setup 标记
  - describe_workflow 返 setup phase 元信息
  - get_agent_prompt 借 prompt 文本（from workflow.setup / pool）
  - list_agents 扫 agent 池

**Lifecycle 组**：
  - start_workflow v4 签名（name-based + setup_outputs）+ Result 信封
  - start_workflow setup_required → kind=business_config + 引导 _hint（三重杠杆 B）
  - start_workflow setup_outputs_mismatch → kind=business_config
  - start_workflow 不阻塞（HandleId pattern）
  - get_task_status 4 status（无 needs_decision）+ Result 信封
  - cancel_task Result 信封

**History 组**：
  - get_task_history 读 tape 事件
  - get_agent 返 agent 详情

**信封 / kind 铁律**：
  - 所有 tool 返 ``{ok, data?, error?, _hint?}``
  - error.kind 是 ErrorKind 值（无 layer）
  - setup_required/setup_outputs_mismatch → kind=business_config（SPEC §2.4b）
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.exec.error_kinds import ErrorKind
from orca.iface.mcp.server import OrcaMcpServer, _result_to_dict
from orca.exec.result import Error, Result
from orca.iface.web.run_manager import RunManager

from tests.iface.web.conftest import run_async


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_server_with_mock_manager() -> tuple[OrcaMcpServer, MagicMock]:
    """构造 OrcaMcpServer，manager 是 MagicMock（方法可独立 mock）。"""
    mock_manager = MagicMock(spec=RunManager)
    return OrcaMcpServer(mock_manager), mock_manager  # type: ignore[arg-type]


# ── Result 信封序列化 ─────────────────────────────────────────────────────────


def test_result_to_dict_ok_with_data_and_hint():
    """ok=True → {ok: True, data: ..., _hint: ...}（无 error 字段）。"""
    r = Result.ok_({"x": 1}, hint="next step")
    d = _result_to_dict(r)
    assert d["ok"] is True
    assert d["data"] == {"x": 1}
    assert d["_hint"] == "next step"
    assert "error" not in d


def test_result_to_dict_err_with_kind_no_layer():
    """ok=False → {ok: False, error: {kind, message, retryable?}, _hint?}，**无 layer**。

    ADR §4.1 决策 1.3：Error 信封无 layer 字段（kind 前缀派生）。
    """
    r = Result.err(
        Error(kind=ErrorKind.BUSINESS_CONFIG, message="bad config"),
        hint="fix config",
    )
    d = _result_to_dict(r)
    assert d["ok"] is False
    assert "data" not in d
    assert d["error"]["kind"] == "business_config"
    assert d["error"]["message"] == "bad config"
    # 关键铁律：error 无 layer 字段（ADR §4.1 决策 1.3）
    assert "layer" not in d["error"]
    assert d["_hint"] == "fix config"


# ── Discovery 组 ─────────────────────────────────────────────────────────────


def test_list_workflows_returns_has_setup_flag(tmp_path, monkeypatch):
    """list_workflows 返 has_setup 标记（三重杠杆 A）。

    合成 workflows/demo.yaml（无 setup）→ has_setup=False。monkeypatch catalog 目录。
    """
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "demo.yaml").write_text(
        """
name: demo
description: test
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
    monkeypatch.setattr(
        "orca.iface.mcp.catalog._workflow_dirs",
        lambda: [wf_dir],
    )

    server, _ = _make_server_with_mock_manager()
    result = run_async(server.tool_list_workflows())

    assert result["ok"] is True
    workflows = result["data"]["workflows"]
    assert len(workflows) == 1
    assert workflows[0]["name"] == "demo"
    assert workflows[0]["has_setup"] is False
    assert "has_setup" in result["_hint"]  # 三重杠杆 A hint


def test_list_workflows_has_setup_true(tmp_path, monkeypatch):
    """合成 setup workflow → has_setup=True。"""
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "setup_wf.yaml").write_text(
        """
name: setup_wf
description: has setup
setup:
  - name: collector
    kind: agent
    prompt: "collect info"
    output_schema:
      type: object
      properties:
        host: {type: string}
      required: [host]
entry: deploy
nodes:
  - name: deploy
    kind: script
    command: "echo deploy"
    routes:
      - to: $end
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "orca.iface.mcp.catalog._workflow_dirs",
        lambda: [wf_dir],
    )

    server, _ = _make_server_with_mock_manager()
    result = run_async(server.tool_list_workflows())

    assert result["ok"] is True
    workflows = result["data"]["workflows"]
    assert len(workflows) == 1
    assert workflows[0]["has_setup"] is True


def test_describe_workflow_not_found_returns_business_config():
    """describe_workflow 未知 name → ok=False + kind=business_config + _hint 引导。"""
    server, _ = _make_server_with_mock_manager()
    result = run_async(server.tool_describe_workflow(name="nonexistent_wf"))
    assert result["ok"] is False
    assert result["error"]["kind"] == "business_config"
    assert "list_workflows" in result["_hint"]


def test_describe_workflow_returns_setup_metadata(tmp_path, monkeypatch):
    """describe_workflow 返 setup phase 元信息（agent names + output_schema）。"""
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "setup_wf.yaml").write_text(
        """
name: setup_wf
description: has setup
setup:
  - name: collector
    kind: agent
    prompt: "collect info"
    output_schema:
      type: object
      properties:
        host: {type: string}
      required: [host]
entry: deploy
nodes:
  - name: deploy
    kind: script
    command: "echo {{ setup.collector.output.host }}"
    routes:
      - to: $end
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "orca.iface.mcp.catalog._workflow_dirs",
        lambda: [wf_dir],
    )

    server, _ = _make_server_with_mock_manager()
    result = run_async(server.tool_describe_workflow(name="setup_wf"))

    assert result["ok"] is True
    data = result["data"]
    assert data["has_setup"] is True
    assert len(data["setup"]) == 1
    assert data["setup"][0]["name"] == "collector"
    assert data["setup"][0]["output_schema"]["properties"]["host"]["type"] == "string"
    # hint 引导 setup 流程
    assert "get_agent_prompt" in result["_hint"]


def test_get_agent_prompt_from_workflow_setup(tmp_path, monkeypatch):
    """get_agent_prompt(workflow=...) 借 setup agent prompt 文本。"""
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "setup_wf.yaml").write_text(
        """
name: setup_wf
description: has setup
setup:
  - name: collector
    kind: agent
    prompt: "Ask user for NAS host and backup strategy."
    output_schema:
      type: object
      properties:
        host: {type: string}
      required: [host]
entry: deploy
nodes:
  - name: deploy
    kind: script
    command: "echo deploy"
    routes:
      - to: $end
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "orca.iface.mcp.catalog._workflow_dirs",
        lambda: [wf_dir],
    )

    server, _ = _make_server_with_mock_manager()
    result = run_async(
        server.tool_get_agent_prompt(name="collector", workflow="setup_wf")
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["name"] == "collector"
    assert "NAS host" in data["prompt"]
    # hint 引导 setup_outputs
    assert "setup_outputs" in result["_hint"]


def test_get_agent_prompt_setup_agent_not_in_workflow(tmp_path, monkeypatch):
    """workflow 存在但 setup agent name 不匹配 → business_config error。"""
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "setup_wf.yaml").write_text(
        """
name: setup_wf
description: has setup
setup:
  - name: collector
    kind: agent
    prompt: "collect"
entry: deploy
nodes:
  - name: deploy
    kind: script
    command: "echo deploy"
    routes:
      - to: $end
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "orca.iface.mcp.catalog._workflow_dirs",
        lambda: [wf_dir],
    )

    server, _ = _make_server_with_mock_manager()
    result = run_async(
        server.tool_get_agent_prompt(name="wrong_name", workflow="setup_wf")
    )
    assert result["ok"] is False
    assert result["error"]["kind"] == "business_config"


# ── Lifecycle 组 ─────────────────────────────────────────────────────────────


def _setup_workflow_yaml(tmp_path, monkeypatch):
    """在 tmp_path/workflows/ 放合成 setup workflow（三重杠杆 E2E 共用）。"""
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "setup_wf.yaml").write_text(
        """
name: setup_wf
description: has setup
setup:
  - name: collector
    kind: agent
    prompt: "collect info"
    output_schema:
      type: object
      properties:
        host: {type: string}
      required: [host]
entry: deploy
nodes:
  - name: deploy
    kind: script
    command: "echo {{ setup.collector.output.host }}"
    routes:
      - to: $end
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "orca.iface.mcp.catalog._workflow_dirs",
        lambda: [wf_dir],
    )


def test_start_workflow_no_setup_completes(tmp_path, monkeypatch):
    """start_workflow 无 setup workflow → 启动成功 + Result 信封。"""
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "demo.yaml").write_text(
        """
name: demo
description: no setup
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
    monkeypatch.setattr(
        "orca.iface.mcp.catalog._workflow_dirs",
        lambda: [wf_dir],
    )

    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.start_run = AsyncMock(return_value="run_abc")

    result = run_async(server.tool_start_workflow(name="demo"))

    assert result["ok"] is True
    assert result["data"]["task_id"] == "run_abc"
    assert result["data"]["status"] == "running"
    assert "get_task_status" in result["_hint"]
    mock_manager.start_run.assert_awaited_once()


def test_start_workflow_setup_required_three_lever_b(tmp_path, monkeypatch):
    """三重杠杆 B：start_workflow has_setup=true + setup_outputs=None → business_config。"""
    _setup_workflow_yaml(tmp_path, monkeypatch)
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.start_run = AsyncMock(return_value="should_not_be_called")

    result = run_async(
        server.tool_start_workflow(name="setup_wf", setup_outputs=None)
    )

    assert result["ok"] is False
    assert result["error"]["kind"] == "business_config"
    assert "setup" in result["error"]["message"].lower()
    # 引导性 _hint（杠杆 B 核心）
    assert "describe_workflow" in result["_hint"]
    assert "get_agent_prompt" in result["_hint"]
    # start_run 不应被调（fail loud 在 manager 之前）
    mock_manager.start_run.assert_not_awaited()


def test_start_workflow_setup_outputs_mismatch(tmp_path, monkeypatch):
    """setup_outputs key 不匹配 → kind=business_config + mismatch hint。"""
    _setup_workflow_yaml(tmp_path, monkeypatch)
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.start_run = AsyncMock(return_value="should_not_be_called")

    result = run_async(
        server.tool_start_workflow(
            name="setup_wf",
            setup_outputs={"wrong_agent": {"host": "x"}},
        )
    )

    assert result["ok"] is False
    assert result["error"]["kind"] == "business_config"
    assert "mismatch" in result["error"]["message"].lower()
    # mismatch hint 引导
    assert "keys must match" in result["_hint"]
    mock_manager.start_run.assert_not_awaited()


def test_start_workflow_setup_outputs_schema_invalid(tmp_path, monkeypatch):
    """setup_outputs schema 校验失败（缺 required 字段）→ kind=business_config。"""
    _setup_workflow_yaml(tmp_path, monkeypatch)
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.start_run = AsyncMock(return_value="should_not_be_called")

    result = run_async(
        server.tool_start_workflow(
            name="setup_wf",
            setup_outputs={"collector": {}},  # 缺 required host
        )
    )

    assert result["ok"] is False
    assert result["error"]["kind"] == "business_config"
    assert "collector" in result["error"]["message"]
    mock_manager.start_run.assert_not_awaited()


def test_start_workflow_setup_outputs_valid_passes(tmp_path, monkeypatch):
    """setup_outputs 校验通过 → 启动成功（start_run 被调）。"""
    _setup_workflow_yaml(tmp_path, monkeypatch)
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.start_run = AsyncMock(return_value="run_ok")

    result = run_async(
        server.tool_start_workflow(
            name="setup_wf",
            setup_outputs={"collector": {"host": "nas1.example.com"}},
        )
    )

    assert result["ok"] is True
    assert result["data"]["task_id"] == "run_ok"
    mock_manager.start_run.assert_awaited_once()


def test_start_workflow_not_in_catalog(tmp_path, monkeypatch):
    """start_workflow 未知 name → business_config error。"""
    monkeypatch.setattr(
        "orca.iface.mcp.catalog._workflow_dirs",
        lambda: [tmp_path / "nonexistent"],
    )
    server, mock_manager = _make_server_with_mock_manager()
    result = run_async(server.tool_start_workflow(name="ghost"))
    assert result["ok"] is False
    assert result["error"]["kind"] == "business_config"


def test_start_workflow_does_not_block_on_slow_orchestrator(tmp_path, monkeypatch):
    """HandleId pattern：start_workflow 秒级返回（不等编排）。

    用真 RunManager + monkey-patch _run_with_sem sleep 5s。start <1s 返回。
    """
    import hashlib

    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "slow.yaml").write_text(
        """
name: slow_demo
description: slow
entry: a
nodes:
  - name: a
    kind: script
    command: "sleep 10"
    routes:
      - to: $end
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "orca.iface.mcp.catalog._workflow_dirs",
        lambda: [wf_dir],
    )

    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:6]
    short_runs = Path(f"/tmp/orca-mcp-{h}/runs")
    short_runs.parent.mkdir(parents=True, exist_ok=True)
    manager = RunManager(runs_dir=short_runs)

    async def slow_run_with_sem(handle, inputs, task, max_iter):
        await asyncio.sleep(5)
        handle.status = "completed"

    manager._run_with_sem = slow_run_with_sem  # type: ignore[assignment]

    server = OrcaMcpServer(manager)
    start = time.monotonic()
    result = run_async(server.tool_start_workflow(name="slow_demo"))
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"start_workflow 阻塞了 {elapsed:.2f}s（应秒级返回）"
    assert result["ok"] is True
    assert result["data"]["status"] == "running"

    run_async(manager.shutdown(timeout=1.0))


# ── get_task_status（v4：无 needs_decision）───────────────────────────────────


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
    """running → _hint 显式建议结束 turn（防 CC 循环检测）。"""
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.run_summary = MagicMock(return_value=_summary(status="running"))

    result = run_async(server.tool_get_task_status(task_id="r1"))

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    assert "End your turn" in result["_hint"]
    assert result["data"]["progress"] == "2/5"
    # v4：gate 字段不透传（execute phase 永不中断）
    assert "gate" not in result["data"]


def test_get_task_status_completed():
    """completed → _hint 指 output 字段。"""
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.run_summary = MagicMock(
        return_value=_summary(status="completed", output={"result": "ok"})
    )

    result = run_async(server.tool_get_task_status(task_id="r1"))

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert "Output is in" in result["_hint"]
    assert result["data"]["output"] == {"result": "ok"}


def test_get_task_status_failed():
    """failed → _hint 指 error 字段。"""
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.run_summary = MagicMock(
        return_value=_summary(status="failed", error="ValueError: boom")
    )

    result = run_async(server.tool_get_task_status(task_id="r1"))

    assert result["ok"] is True
    assert result["data"]["status"] == "failed"
    assert "Error is in" in result["_hint"]
    assert result["data"]["error"] == "ValueError: boom"


def test_get_task_status_cancelled():
    """cancelled → _hint 简短确认。"""
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.run_summary = MagicMock(return_value=_summary(status="cancelled"))

    result = run_async(server.tool_get_task_status(task_id="r1"))

    assert result["ok"] is True
    assert result["data"]["status"] == "cancelled"
    assert "cancelled" in result["_hint"].lower()


def test_get_task_status_unknown_returns_hint():
    """未知 task_id → run_summary 返回 None → status="unknown" + _hint。"""
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.run_summary = MagicMock(return_value=None)

    result = run_async(server.tool_get_task_status(task_id="ghost"))

    assert result["ok"] is True
    assert result["data"]["status"] == "unknown"
    assert result["data"]["task_id"] == "ghost"
    assert "_hint" in result


# ── History 组 ───────────────────────────────────────────────────────────────


def test_get_task_history_unknown_returns_error():
    """get_task_history 未知 task_id → ok=False + business_config。"""
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.get_run_events = MagicMock(side_effect=KeyError("unknown run_id: ghost"))

    result = run_async(server.tool_get_task_history(task_id="ghost"))

    assert result["ok"] is False
    assert result["error"]["kind"] == "business_config"


def test_get_task_history_returns_events():
    """get_task_history 返 tape 事件摘要列表。"""
    from orca.schema.event import Event

    events = [
        Event(seq=1, type="workflow_started", timestamp=0.0, data={}),
        Event(seq=2, type="node_completed", timestamp=1.0, node="a", data={"output": "ok"}),
    ]
    server, mock_manager = _make_server_with_mock_manager()
    mock_manager.get_run_events = MagicMock(return_value=events)

    result = run_async(server.tool_get_task_history(task_id="r1"))

    assert result["ok"] is True
    assert result["data"]["count"] == 2
    assert len(result["data"]["events"]) == 2
    assert result["data"]["events"][0]["type"] == "workflow_started"
    assert result["data"]["events"][1]["summary"] == "ok"


# ── get_agent（History 组，防 _cwd 回归）──────────────────────────────────────


def test_get_agent_nonexistent_returns_business_config(tmp_path, monkeypatch):
    """get_agent 未知 name → ok=False + kind=business_config（不 raise NameError）。

    回归守护：早期实现误删 _cwd() helper 导致 NameError（E2E 实测发现）。
    """
    # 隔离 cwd 到 tmp_path（避免扫到项目 agents/）
    monkeypatch.chdir(tmp_path)
    server, _ = _make_server_with_mock_manager()

    result = run_async(server.tool_get_agent(name="nonexistent_xyz"))

    assert result["ok"] is False
    assert result["error"]["kind"] == "business_config"
    assert "list_agents" in result["_hint"]


def test_get_agent_returns_prompt_preview(tmp_path, monkeypatch):
    """get_agent 找到 agent → 返 prompt_preview + meta。"""
    # 合成 agent pool
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "simple.md").write_text(
        "---\ndescription: test agent\n---\nDo the thing.\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    server, _ = _make_server_with_mock_manager()

    result = run_async(server.tool_get_agent(name="simple"))

    assert result["ok"] is True
    assert result["data"]["name"] == "simple"
    assert result["data"]["description"] == "test agent"
    assert "Do the thing" in result["data"]["prompt_preview"]


# ── FastMCP 注册 9 工具 ──────────────────────────────────────────────────────


def test_fastmcp_lists_nine_tools():
    """FastMCP 注册 9 工具（v4 SPEC §2.1）。"""
    m = RunManager()
    server = OrcaMcpServer(m)
    tools = server._mcp._tool_manager._tools
    names = set(tools.keys())
    assert names == {
        "list_workflows",
        "describe_workflow",
        "list_agents",
        "get_agent_prompt",
        "start_workflow",
        "get_task_status",
        "cancel_task",
        "get_task_history",
        "get_agent",
    }


def test_no_resolve_gate_in_v4():
    """v4 删 resolve_gate（execute phase 永不中断，§0.1 铁律 7）。"""
    m = RunManager()
    server = OrcaMcpServer(m)
    assert not hasattr(server, "tool_resolve_gate")
    tools = server._mcp._tool_manager._tools
    assert "resolve_gate" not in tools


def test_tool_docstrings_contain_chain_instruction():
    """每个 tool 的 docstring 必须含显式 chain 调指令（SPEC §2.6 杠杆 C）。"""
    m = RunManager()
    server = OrcaMcpServer(m)
    # start_workflow docstring 含 setup 引导
    assert "describe_workflow" in server.tool_start_workflow.__doc__
    # get_task_status docstring 含 poll 引导
    assert "get_task_status" in server.tool_start_workflow.__doc__
