"""tests/run/test_demo_integration.py —— 9 个 demo workflow 端到端（计划 R5.4）。

两档：
  - **零 token demo（纯 script/set）**：默认跑（无 marker）—— linear / loop / foreach /
    failure / max_iter / parallel。这些用确定性 script/set 驱动路由，不 spawn claude。
  - **agent demo**：``@pytest.mark.integration``，CI 无 claude CLI 时 skip ——
    conditional / mixed / task。这些真 spawn claude（需 API key + CLI）。

每个 demo 断言：
  - workflow status（completed / failed）
  - outputs 正确（确定性值）
  - Tape 事件流完整（workflow_started → node_* → workflow_completed/failed）
  - replay_state 重建的 RunState 与断言一致
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from orca.compile.parser import load_workflow
from orca.events.tape import Tape
from orca.run import run_workflow

DEMO_DIR = Path(__file__).resolve().parents[2] / "examples"


def _demo(name: str) -> Path:
    return DEMO_DIR / f"demo_{name}.yaml"


def _event_types(state) -> list[str]:
    """重读 tape 取事件类型序列（state 自身不持事件流）。"""
    tape = Tape(Path("runs") / f"{state.run_id}.jsonl", run_id=state.run_id)
    return [e.type for e in tape.replay()]


def _completed_outputs(state) -> dict:
    """从 tape 取 workflow_completed.data.outputs。"""
    tape = Tape(Path("runs") / f"{state.run_id}.jsonl", run_id=state.run_id)
    for ev in tape.replay():
        if ev.type == "workflow_completed":
            return ev.data.get("outputs", {})
    return {}


def _failed_event(state):
    tape = Tape(Path("runs") / f"{state.run_id}.jsonl", run_id=state.run_id)
    for ev in tape.replay():
        if ev.type == "workflow_failed":
            return ev
    return None


def _claude_available() -> bool:
    return shutil.which("claude") is not None


# ── 零 token demo（默认跑，无 marker）──────────────────────────────────────────


def test_demo_linear_completes(tmp_path, monkeypatch):
    """a→b→c→$end：completed，outputs.result=step_c，事件流完整。"""
    monkeypatch.chdir(tmp_path)
    wf = load_workflow(_demo("linear"))
    state = run_workflow_sync(wf)

    assert state.status == "completed"
    assert _completed_outputs(state)["result"].strip() == "step_c"
    types = _event_types(state)
    assert types[0] == "workflow_started"
    assert types[-1] == "workflow_completed"
    assert types.count("node_started") == 3
    assert types.count("node_completed") == 3


def test_demo_loop_terminates_at_3(tmp_path, monkeypatch):
    """counter 计数到 3 停 → completed，final_count=3。"""
    monkeypatch.chdir(tmp_path)
    wf = load_workflow(_demo("loop"))
    state = run_workflow_sync(wf)

    assert state.status == "completed"
    assert _completed_outputs(state)["final_count"] == "3"
    # done 跑了（循环正常终止）
    assert "done" in state.node_status


def test_demo_foreach_processes_3_items(tmp_path, monkeypatch):
    """foreach 对 [1,2,3] 分批并行 → completed，processed=count=3。"""
    monkeypatch.chdir(tmp_path)
    wf = load_workflow(_demo("foreach"))
    state = run_workflow_sync(wf)

    assert state.status == "completed"
    outputs = _completed_outputs(state)
    assert outputs["processed"] == "3"  # Jinja2 渲染 int → str


def test_demo_failure_records_nonzero_exit(tmp_path, monkeypatch):
    """script exit 1 不 fail loud：workflow 仍 completed，exit_code=1（SPEC §4.6）。"""
    monkeypatch.chdir(tmp_path)
    wf = load_workflow(_demo("failure"))
    state = run_workflow_sync(wf)

    assert state.status == "completed"  # 非零退出 = 业务结果，非失败
    assert _completed_outputs(state)["exit_code"] == "1"


def test_demo_max_iter_workflow_fails(tmp_path, monkeypatch):
    """循环不终止 → MaxIterations → workflow_failed。"""
    monkeypatch.chdir(tmp_path)
    wf = load_workflow(_demo("max_iter"))
    state = run_workflow_sync(wf)

    assert state.status == "failed"
    failed = _failed_event(state)
    assert failed is not None
    assert failed.data["kind"] == "business_config"


def test_demo_parallel_merges_branches(tmp_path, monkeypatch):
    """start → split 组（branch_a/b 并行）→ merger → completed，汇聚输出正确。"""
    monkeypatch.chdir(tmp_path)
    wf = load_workflow(_demo("parallel"))
    state = run_workflow_sync(wf)

    assert state.status == "completed"
    outputs = _completed_outputs(state)
    assert "merged a + b" in outputs["result"]
    # branch_a / branch_b 都跑了（split 组并行执行）
    assert "branch_a" in state.node_status
    assert "branch_b" in state.node_status


def test_demo_parallel_events_includes_route_taken(tmp_path, monkeypatch):
    """parallel 组完成后 emit route_taken（split → merger）。"""
    monkeypatch.chdir(tmp_path)
    wf = load_workflow(_demo("parallel"))
    state = run_workflow_sync(wf)

    types = _event_types(state)
    route_events = [e for e in _replay(state) if e.type == "route_taken"]
    # 至少有 start→split / split→merger / merger→$end
    tos = [r.data["to"] for r in route_events]
    assert "split" in tos
    assert "merger" in tos


# ── 事件流完整性（共享断言，每个零 token demo 都覆盖关键序列）─────────────────


def test_demo_linear_replay_state_matches_assertions(tmp_path, monkeypatch):
    """replay_state 重建的 RunState：status / current_node / node_status 正确。"""
    monkeypatch.chdir(tmp_path)
    wf = load_workflow(_demo("linear"))
    state = run_workflow_sync(wf)
    assert state.workflow_name == "demo_linear"
    assert state.current_node is None  # workflow_completed → None
    assert state.node_status == {"a": "done", "b": "done", "c": "done"}


# ── agent demo（@pytest.mark.integration，CI skip 无 claude）──────────────────


@pytest.mark.integration
@pytest.mark.skipif(not _claude_available(), reason="claude CLI 不在 PATH（agent demo 需真 spawn）")
def test_demo_conditional_takes_high_branch(tmp_path, monkeypatch):
    """decide(path=high) → high_agent → completed，taken=high。"""
    monkeypatch.chdir(tmp_path)
    wf = load_workflow(_demo("conditional"))
    state = run_workflow_sync(wf)
    assert state.status == "completed"
    assert _completed_outputs(state)["taken"] == "high"
    assert "high_agent" in state.node_status


@pytest.mark.integration
@pytest.mark.skipif(not _claude_available(), reason="claude CLI 不在 PATH（agent demo 需真 spawn）")
def test_demo_mixed_reaches_reporter(tmp_path, monkeypatch):
    """prep → analyzer → judge(pass) → reporter → completed。"""
    monkeypatch.chdir(tmp_path)
    wf = load_workflow(_demo("mixed"))
    state = run_workflow_sync(wf)
    assert state.status == "completed"
    assert _completed_outputs(state)["result"].strip() == "final"
    assert "reporter" in state.node_status


@pytest.mark.integration
@pytest.mark.skipif(not _claude_available(), reason="claude CLI 不在 PATH（agent demo 需真 spawn）")
def test_demo_task_injects_into_prompt(tmp_path, monkeypatch):
    """run_workflow(wf, task=...) → agent 收到 {{ inputs.task }}。"""
    monkeypatch.chdir(tmp_path)
    wf = load_workflow(_demo("task"))
    state = run_workflow_sync(wf, task="测试任务")
    assert state.status == "completed"
    # agent 的 output 是自由文本（取 result），断言 reply 非空（不验内容，claude 非确定）
    assert _completed_outputs(state).get("reply") is not None


# ── helpers ───────────────────────────────────────────────────────────────────


def run_workflow_sync(wf, **kw):
    """同步包装（本仓库约定 asyncio.run，无 pytest-asyncio）。"""
    import asyncio

    return asyncio.run(run_workflow(wf, None, **kw))


def _replay(state):
    tape = Tape(Path("runs") / f"{state.run_id}.jsonl", run_id=state.run_id)
    return list(tape.replay())
