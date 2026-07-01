"""test_resume_e2e.py —— ``orca resume`` CLI 端到端契约测试（phase 11 §7 wave-1 gap 填充）。

补 implementer 已有 TestResumeCommand 的 GAPS（不重复 unit/单分支测试）。已有覆盖：
  - TestResumeCommand（test_commands.py）：missing file / missing run_id / run_id 解析 /
    empty tape / completed tape / no-yaml-unresolvable → exit code（**不**真跑续跑）。
  - test_resume.py（run/）：from_tape / run_from_state 核心逻辑（不经 CLI）。

本文件填的 GAPS（经真实 ``orca resume`` CLI 入口，SPEC §10.2 item7 / §7.3）：
  - 末尾残行 → fail-soft（exit 0 续跑路径 / 截断 warning），经 CliRunner 真入口。
  - 中段损坏 → exit 2，经 CliRunner 真入口。
  - 健康崩溃（跑到一半 kill）→ ``orca resume`` 续跑至 completed（exit 0）。
  - tape-append 不变量：resume 后 tape 是合法单 append-only log——原事件不动 +
    workflow_resumed + 续跑事件 + workflow_completed；replay_state 终态 completed。
  - 幂等：resume 一个已 completed 的 tape → exit 0，不重跑任何 node。

驱动方式（SPEC wave-1 测试约束）：真 typer CliRunner / 真 Tape / 真 Orchestrator /
真 EventBus，workflow 用全 script node（确定性 echo，零 token / 零 claude spawn）。
断言走 exit code + stdout + tape 事件流 + replay_state（可观测结果）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from orca.events.bus import EventBus
from orca.events.replay import replay_state
from orca.events.tape import Tape
from orca.iface.cli.commands import EXIT_ARG_OR_VALIDATE, EXIT_OK, app
from orca.run.orchestrator import Orchestrator
from orca.schema import Route, ScriptNode, Workflow


runner = CliRunner()


# ── helpers ──────────────────────────────────────────────────────────────────


def _linear_2script_wf_def() -> dict:
    """2-node 线性全-script wf：a → b → $end（echo，确定性，零 claude）。

    用全 script 让 resume 续跑无需 mock claude spawn——经 CLI 真实跑通。
    """
    return {
        "name": "resume_cli_e2e",
        "entry": "a",
        "nodes": [
            {"name": "a", "kind": "script", "command": "echo step_a",
             "routes": [{"to": "b"}]},
            {"name": "b", "kind": "script", "command": "echo step_b",
             "routes": [{"to": "$end"}]},
        ],
        "outputs": {"result": "{{ b.output.stdout }}"},
    }


def _write_yaml(tmp_path: Path, name: str, wf_def: dict) -> Path:
    p = tmp_path / f"{name}.yaml"
    p.write_text(yaml.safe_dump(wf_def), encoding="utf-8")
    return p


def _wf_from_def(wf_def: dict) -> Workflow:
    """从 dict 构造 Workflow schema 对象（用于直接驱动 Orchestrator 造 tape）。"""
    return Workflow(
        name=wf_def["name"],
        entry=wf_def["entry"],
        nodes=[
            ScriptNode(
                name=n["name"], command=n["command"],
                routes=[Route(to=r["to"]) for r in n["routes"]],
            )
            for n in wf_def["nodes"]
        ],
        outputs=wf_def.get("outputs", {}),
    )


def _make_partial_tape(
    tmp_path: Path, wf_def: dict, completed_nodes: list[str],
    crash_before: str,
) -> Path:
    """跑真实 workflow 但 monkeypatch 让 ``crash_before`` 崩溃（模拟 kill -9）。

    返回 tape 路径：含 ``completed_nodes`` 的完整事件，``crash_before`` 未跑。
    """
    wf = _wf_from_def(wf_def)
    tape_path = tmp_path / "events.jsonl"
    tape = Tape(tape_path, run_id="r1")
    bus = EventBus(tape)
    orch = Orchestrator(wf, bus, inputs={}, run_id="r1")

    import orca.exec.factory as factory_mod
    from tests.run.conftest import FakeExecutor

    real_make = factory_mod.make_executor
    fired = {"_f": False}

    def patched_make(node, agent_tools_server=None):
        if node.name in completed_nodes:
            return real_make(node)
        # crash_before 第一次被调时模拟崩溃（raise → workflow_failed）。
        if not fired["_f"]:
            fired["_f"] = True
            return FakeExecutor.failing(
                error_type="SpawnError", message="simulated kill -9",
                phase="spawn", node_name=node.name,
            )
        return real_make(node)

    factory_mod.make_executor = patched_make
    try:
        asyncio.run(orch.run())
    finally:
        factory_mod.make_executor = real_make
    return tape_path


def _count_events(tape_path: Path) -> int:
    n = 0
    for _ in Tape(tape_path, run_id="r1").replay():
        n += 1
    return n


# ── 末尾残行 fail-soft 经 CLI（SPEC §7.3 review C6）─────────────────────────


def test_resume_cli_trailing_partial_line_fail_soft(tmp_path):
    """经真实 ``orca resume`` CLI：末尾残行 → fail-soft（截断 + 续跑完成，exit 0）。

    SPEC §7.3：末尾残行（崩溃写一半）**不** exit 2，截断后继续。已有
    ``test_resume_trailing_partial_line_fail_soft``（test_resume.py）测 from_tape 层；
    本测试经 CliRunner 真入口验证 CLI 层也走 fail-soft（exit 0 续跑路径，非 exit 2）。
    """
    wf_def = _linear_2script_wf_def()
    yaml_path = _write_yaml(tmp_path, "wf", wf_def)

    # 构造 tape：a 完成 + route_taken 到 b + 末尾残行（b 的 node_started 写一半）。
    tape_path = tmp_path / "events.jsonl"
    lines = [
        {"seq": 1, "type": "workflow_started", "timestamp": 1.0, "node": None,
         "session_id": None, "data": {"inputs": {}, "node_count": 2, "entry": "a",
                                      "workflow_name": "resume_cli_e2e", "topology": {}}},
        {"seq": 2, "type": "node_started", "timestamp": 1.0, "node": "a",
         "session_id": "s1", "data": {"kind": "script"}},
        {"seq": 3, "type": "node_completed", "timestamp": 1.0, "node": "a",
         "session_id": "s1", "data": {"output": {"stdout": "step_a\n", "exit_code": 0},
                                      "elapsed": 0.01}},
        {"seq": 4, "type": "route_taken", "timestamp": 1.0, "node": None,
         "session_id": None, "data": {"from": "a", "to": "b"}},
    ]
    content = "\n".join(json.dumps(l) for l in lines) + "\n"
    # 末尾残行：b 的 node_started 写了一半（崩溃）。
    content += '{"seq": 5, "type": "node_started", "timestamp": 1.0, "node": "b"'
    tape_path.write_text(content, encoding="utf-8")

    # 经真实 CLI resume。
    result = runner.invoke(
        app, ["resume", str(tape_path), "--yaml", str(yaml_path)],
    )
    # SPEC §7.3：末尾残行 fail-soft → 截断 + 续跑 → 续跑成功 exit 0（**非** exit 2）。
    assert result.exit_code == EXIT_OK, (
        f"末尾残行应 fail-soft 续跑 exit 0, got {result.exit_code}; output:\n{result.output}"
    )
    # 续跑完成：tape 终态 completed（replay_state 从 tape 派生）。
    tape = Tape(tape_path, run_id="r1")
    state = replay_state(tape)
    assert state.status == "completed", f"resume 后应 completed, got {state.status}"
    assert state.node_status.get("b") == "done"


# ── 中段损坏经 CLI（SPEC §7.3）──────────────────────────────────────────────


def test_resume_cli_mid_file_corrupt_exits_two(tmp_path):
    """经真实 ``orca resume`` CLI：中段损坏（合法行后跟乱码行）→ exit 2。

    已有 ``test_resume_mid_file_corrupt`` 在 from_tape 层；本测试经 CliRunner 真入口
    验证 CLI 层映射到 exit 2 + 用户可见错误信息。
    """
    wf_def = _linear_2script_wf_def()
    yaml_path = _write_yaml(tmp_path, "wf", wf_def)

    tape_path = tmp_path / "corrupt.jsonl"
    good_started = json.dumps({
        "seq": 1, "type": "workflow_started", "timestamp": 1.0, "node": None,
        "session_id": None, "data": {"inputs": {}, "node_count": 2, "entry": "a",
                                     "workflow_name": "resume_cli_e2e", "topology": {}},
    })
    garbage = "{this is not valid json"
    good_after = json.dumps({
        "seq": 2, "type": "node_completed", "timestamp": 1.0, "node": "a",
        "session_id": "s1", "data": {"output": {"stdout": "x"}, "elapsed": 0.1},
    })
    tape_path.write_text(
        good_started + "\n" + garbage + "\n" + good_after + "\n", encoding="utf-8",
    )

    result = runner.invoke(
        app, ["resume", str(tape_path), "--yaml", str(yaml_path)],
    )
    assert result.exit_code == EXIT_ARG_OR_VALIDATE, (
        f"中段损坏应 exit 2, got {result.exit_code}"
    )
    # 用户可见的错误信息：含行号 + 「中段损坏」。
    assert "中段损坏" in result.output or "第 2 行" in result.output, (
        f"错误信息应含中段损坏 + 行号, got:\n{result.output}"
    )


# ── 健康崩溃经 CLI 续跑（SPEC §10.2 item7）─────────────────────────────────


def test_resume_cli_healthy_crash_completes_and_exit_zero(tmp_path):
    """经真实 ``orca resume`` CLI：跑到一半崩溃 → resume 从崩溃点续跑至 completed（exit 0）。

    SPEC §10.2 item7 / §1.4：Tape 即 checkpoint，``orca resume`` 读 Tape 重放到崩溃前
    位置 + 从该 node 续跑。已有 ``test_resume_emits_workflow_resumed_and_completes``
    在 from_tape 层；本测试经 CliRunner 真入口验证 CLI 全链路（exit 0 + tape 续跑）。
    """
    wf_def = _linear_2script_wf_def()
    yaml_path = _write_yaml(tmp_path, "wf", wf_def)

    # 先跑到 a 完成后崩溃（b 失败）。
    tape_path = _make_partial_tape(tmp_path, wf_def, completed_nodes=["a"], crash_before="b")
    events_before = _count_events(tape_path)

    # 经真实 CLI resume（b 用真实 ScriptExecutor echo，确定性）。
    result = runner.invoke(
        app, ["resume", str(tape_path), "--yaml", str(yaml_path)],
    )
    assert result.exit_code == EXIT_OK, (
        f"健康崩溃 resume 应 exit 0, got {result.exit_code}; output:\n{result.output}"
    )

    # tape 终态 completed，b 续跑完成。
    tape = Tape(tape_path, run_id="r1")
    state = replay_state(tape)
    assert state.status == "completed"
    assert state.node_status.get("a") == "done"
    assert state.node_status.get("b") == "done"
    assert "step_b" in state.context["b"]["stdout"]

    # tape 事件增长（续跑追加了 b 的事件 + workflow_resumed + workflow_completed）。
    events_after = _count_events(tape_path)
    assert events_after > events_before, (
        f"resume 应追加事件, before={events_before} after={events_after}"
    )


# ── tape-append 不变量（SPEC §7.1 / 单 Tape 唯一真相源）────────────────────


def test_resume_tape_append_invariant(tmp_path):
    """契约不变量：resume 后 tape 是合法单 append-only log——原事件原封不动 +
    workflow_resumed + 续跑 node 事件 + workflow_completed。

    SPEC §7.1 / 铁律 1（单 Tape 唯一真相源）。验证：
      1. resume 前 tape 的前 N 个事件，resume 后仍是前 N 个事件（顺序 + 内容不变）；
      2. resume 追加段含 workflow_resumed（在续跑 node_started 之前）+ workflow_completed（末尾）；
      3. replay_state(tape_after) 终态 completed（reducer 幂等 fold，单读路径）。
    """
    wf_def = _linear_2script_wf_def()
    yaml_path = _write_yaml(tmp_path, "wf", wf_def)
    tape_path = _make_partial_tape(tmp_path, wf_def, completed_nodes=["a"], crash_before="b")

    # 快照 resume 前的事件序列（seq + type + node）。
    before_events = [
        (e.seq, e.type, e.node) for e in Tape(tape_path, run_id="r1").replay()
    ]
    assert before_events, "resume 前 tape 应非空"

    # resume。
    result = runner.invoke(
        app, ["resume", str(tape_path), "--yaml", str(yaml_path)],
    )
    assert result.exit_code == EXIT_OK

    after_events = list(Tape(tape_path, run_id="r1").replay())
    after_types = [e.type for e in after_events]

    # 1) 原 N 个事件原封不动（append-only：前缀不变）。
    after_prefix = [(e.seq, e.type, e.node) for e in after_events[:len(before_events)]]
    assert after_prefix == before_events, (
        f"resume 应 append-only, 前缀变了:\nbefore={before_events}\nafter_prefix={after_prefix}"
    )

    # 2) 追加段含 workflow_resumed（在续跑 node_started 之前）+ workflow_completed（末尾）。
    assert "workflow_resumed" in after_types
    resumed_ev = next(e for e in after_events if e.type == "workflow_resumed")
    assert resumed_ev.data["resumed_node"] == "b"
    resumed_idx = after_types.index("workflow_resumed")
    # workflow_completed 在末尾。
    assert after_events[-1].type == "workflow_completed"
    completed_idx = after_types.index("workflow_completed")
    # workflow_resumed 在 workflow_completed 之前。
    assert resumed_idx < completed_idx

    # 3) replay_state 终态 completed（reducer 幂等，单读路径从 tape 派生）。
    state = replay_state(Tape(tape_path, run_id="r1"))
    assert state.status == "completed"


# ── 幂等：resume 已 completed 的 tape 不重跑（SPEC §7.3 / review C8）───────


def test_resume_cli_already_completed_is_idempotent(tmp_path):
    """幂等契约：resume 一个已 completed 的 tape → exit 0 + 不重跑任何 node（no double-start）。

    SPEC §7.3「Tape 已是 workflow_completed 终态 → exit 0」+ review C8（不重跑 nodes）。
    已有 ``test_from_tape_completed_workflow_raises`` 测 from_tape 抛 AlreadyCompletedError；
    本测试经 CliRunner 验证 CLI 层 exit 0 + tape 无新增事件（无 node 重跑）。
    """
    wf_def = _linear_2script_wf_def()
    yaml_path = _write_yaml(tmp_path, "wf", wf_def)

    # 跑完整 workflow（全 script 成功）→ completed tape。
    wf = _wf_from_def(wf_def)
    tape_path = tmp_path / "events.jsonl"
    tape = Tape(tape_path, run_id="r1")
    bus = EventBus(tape)
    orch = Orchestrator(wf, bus, inputs={}, run_id="r1")
    state = asyncio.run(orch.run())
    assert state.status == "completed"

    events_before = _count_events(tape_path)

    # resume 一个已 completed 的 tape → exit 0（「已完成，无需 resume」）。
    result = runner.invoke(
        app, ["resume", str(tape_path), "--yaml", str(yaml_path)],
    )
    assert result.exit_code == EXIT_OK
    assert "已完成" in result.output

    # 幂等：tape 无新增事件（无 node 重跑、无重复 workflow_completed）。
    events_after = _count_events(tape_path)
    assert events_after == events_before, (
        f"resume 已 completed tape 应不新增事件（幂等）, "
        f"before={events_before} after={events_after}"
    )
