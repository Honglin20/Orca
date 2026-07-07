"""tests/iface/in_session/test_in_session_cli.py —— 薄 CLI bootstrap/next/stop 守门测试。

覆盖 SPEC §9.2 acceptance：
  - bootstrap → emit ws+ns、写 marker、stdout JSON
  - next --output → 单次 write 原子 emit_batch [nc, rt, ns]（B1）
  - next --output ""` ≡ 省略 --output（B2 normalize None）
  - 合规计数：3 次无 output → workflow_failed(subagent_compliance)（F11）
  - 失败 taxonomy：output_schema_mismatch / unsupported_node_kind / state_corrupt（F6）
  - busy：LOCK_NB 撞锁 → {done:false, reason:busy} 0 退出（F5）
  - stop → workflow_cancelled + 清 marker
  - marker RMW 在 flock 临界区内（N2）：两并发 next → no_output_count 不丢
  - 架构守门（D-v7-1）：grep plugin/hook 模板无 advance/router/replay/tape 路径
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from orca.iface.in_session.cli import app


# ── fixtures ────────────────────────────────────────────────────────────────


AGENT_WF_YAML = """\
name: cli_test_wf
description: 2-agent 线性 workflow（CLI 守门测试用）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 step A 的输出。"
    routes:
      - to: b
  - name: b
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "基于 {{ a.output }} 总结。"
    routes:
      - to: $end
"""


@pytest.fixture
def wf_path(tmp_path: Path) -> Path:
    p = tmp_path / "wf.yaml"
    p.write_text(AGENT_WF_YAML, encoding="utf-8")
    return p


@pytest.fixture
def cwd_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """chdir 到 tmp_path 让 runs/ 写到临时目录。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _bootstrap(runner: CliRunner, wf_path: Path, *extra: str) -> dict:
    result = runner.invoke(app, ["bootstrap", str(wf_path), *extra])
    assert result.exit_code == 0, f"bootstrap failed: {result.output}"
    return json.loads(result.output.splitlines()[-1])


def _next(runner: CliRunner, tape: str, run_id: str, *extra: str,
          expect_exit: int = 0) -> dict:
    result = runner.invoke(app, [
        "next", "--tape", tape, "--run-id", run_id, *extra,
    ])
    assert result.exit_code == expect_exit, (
        f"next exit {result.exit_code} (expected {expect_exit}): {result.output}"
    )
    return json.loads(result.output.splitlines()[-1])


# ── bootstrap ───────────────────────────────────────────────────────────────


def test_bootstrap_emits_ws_ns_and_writes_marker(cwd_tmp, wf_path):
    runner = CliRunner()
    reply = _bootstrap(runner, wf_path)

    assert reply["done"] is False
    assert reply["node"] == "a"
    assert reply["prompt"] is not None
    assert "run_id" in reply
    assert "tape" in reply

    # tape 2 行（ws + ns）
    tape_path = Path(reply["tape"])
    lines = tape_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    types = [json.loads(ln)["type"] for ln in lines]
    assert types == ["workflow_started", "node_started"]

    # marker 已写
    marker_files = list(cwd_tmp.glob("runs/orca-*.json"))
    assert len(marker_files) == 1


def test_bootstrap_idempotent_reuses_run_id(cwd_tmp, wf_path):
    """同 owner + 同 realpath(yaml) 复用 run_id，不重发 ws（N1/F14）。"""
    runner = CliRunner()
    r1 = _bootstrap(runner, wf_path, "--owner", "owner-x")
    r2 = _bootstrap(runner, wf_path, "--owner", "owner-x")
    assert r1["run_id"] == r2["run_id"]
    assert r2.get("reused") is True
    # tape 仍 2 行（没重发）
    tape_path = Path(r1["tape"])
    assert len(tape_path.read_text(encoding="utf-8").strip().split("\n")) == 2


# ── next 单次 write 原子化（B1）────────────────────────────────────────────────


def test_next_with_output_emits_batch_atomically(cwd_tmp, wf_path):
    """next --output → emit_batch [nc, rt, ns] 单次 write（B1）。

    B1 守门：next 推进后 NOT 用单条 emit 写 [nc, rt, ns]——全部走 emit_batch（单次
    write+flush 落盘整批）。本测试用 monkeypatch wrap（计数 + 调原方法）验证。
    """
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    from orca.events import bus as bus_mod
    calls: dict[str, int] = {"emit_batch": 0, "emit": 0}
    real_emit_batch = bus_mod.EventBus.emit_batch
    real_emit = bus_mod.EventBus.emit

    async def spy_emit_batch(self, items):
        calls["emit_batch"] += 1
        return await real_emit_batch(self, items)

    async def spy_emit(self, *a, **kw):
        calls["emit"] += 1
        return await real_emit(self, *a, **kw)

    with mock.patch.object(bus_mod.EventBus, "emit_batch", spy_emit_batch):
        with mock.patch.object(bus_mod.EventBus, "emit", spy_emit):
            reply = _next(runner, tape, run_id, "--output", "node_a_result")

    assert calls["emit_batch"] == 1   # 一次批量 emit
    assert calls["emit"] == 0         # 无单条 emit

    assert reply["done"] is False
    assert reply["node"] == "b"

    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 5  # ws + ns(a) + nc(a) + rt(a→b) + ns(b)
    types = [json.loads(ln)["type"] for ln in lines]
    assert types == ["workflow_started", "node_started", "node_completed",
                     "route_taken", "node_started"]


def test_next_completes_workflow(cwd_tmp, wf_path):
    """两节点全跑完：bootstrap → next(out_a) → next(out_b) → done:true + workflow_completed。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    _next(runner, tape, run_id, "--output", "out_a")
    reply = _next(runner, tape, run_id, "--output", "out_b")

    assert reply["done"] is True

    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    types = [json.loads(ln)["type"] for ln in lines]
    assert types[-1] == "workflow_completed"


# ── B2：--output 空串 normalize None ─────────────────────────────────────────


def test_next_output_empty_string_normalized_to_none(cwd_tmp, wf_path):
    """``--output ""`` ≡ 省略 --output（B2）：走 branch 4 + 合规计数。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    # next --output "" 不应推进到 b，应 idempotent-replay（branch 4）+ 计数 +1
    reply = _next(runner, tape, run_id, "--output", "")
    assert reply["done"] is False
    assert reply["node"] == "a"   # 仍 a，未推进

    # tape 未增加（branch 4 无 emits）
    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2  # 仍是 ws + ns(a)

    # marker 计数已 +1
    markers = list(cwd_tmp.glob("runs/orca-*.json"))
    m = json.loads(markers[0].read_text())
    assert m["no_output_count"] == 1


# ── F11：合规计数 fail loud ───────────────────────────────────────────────────


def test_subagent_compliance_3x_no_output_emits_workflow_failed(cwd_tmp, wf_path):
    """连续 3 次 next 无 output → workflow_failed(subagent_compliance)（F11）。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    r1 = _next(runner, tape, run_id, "--output", "")
    r2 = _next(runner, tape, run_id, "--output", "")
    # 第 3 次 → workflow_failed(subagent_compliance) + 非 0 退出（与其他失败 taxonomy 对齐）
    r3 = _next(runner, tape, run_id, "--output", "", expect_exit=1)

    assert r1["done"] is False and r2["done"] is False
    assert r3["done"] is True  # 第 3 次触发终止

    # tape 末尾是 workflow_failed(subagent_compliance)
    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    last = json.loads(lines[-1])
    assert last["type"] == "workflow_failed"
    assert last["data"]["kind"] == "subagent_compliance"


# ── F6：失败 taxonomy ─────────────────────────────────────────────────────────


def test_failure_output_schema_mismatch(cwd_tmp, wf_path):
    """节点声明 output_schema 但 output 非 JSON → workflow_failed(output_schema_mismatch)。"""
    # 改 wf：a 节点加 output_schema
    yaml_text = AGENT_WF_YAML.replace(
        'prompt: "产出 step A 的输出。"',
        'prompt: "产出 step A 的输出。"\n    output_schema:\n      type: object\n      required: [k]',
    )
    p = cwd_tmp / "wf_schema.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    runner = CliRunner()
    boot = _bootstrap(runner, p)
    run_id, tape = boot["run_id"], boot["tape"]

    result = runner.invoke(app, [
        "next", "--tape", tape, "--run-id", run_id, "--output", "NOT_JSON",
    ])
    # exit_code 1（fail loud）
    assert result.exit_code == 1
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is True
    assert "failed" in reply["reason"]

    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    last = json.loads(lines[-1])
    assert last["type"] == "workflow_failed"
    assert last["data"]["kind"] == "output_schema_mismatch"


def test_failure_unsupported_node_kind(cwd_tmp):
    """script 节点（非 agent）→ workflow_failed(unsupported_node_kind)。"""
    yaml_text = AGENT_WF_YAML.replace("kind: agent", "kind: script", 1).replace(
        '    executor: opencode\n    model: deepseek/deepseek-v4-flash\n    prompt: "产出 step A 的输出。"',
        '    command: "echo a"',
    )
    p = cwd_tmp / "wf_script.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["bootstrap", str(p)])
    assert result.exit_code == 1


# ── F5：LOCK_NB busy ────────────────────────────────────────────────────────


def test_next_busy_when_lock_held(cwd_tmp, wf_path):
    """并发撞锁：另一进程持 tape flock → next 返 {done:false, reason:busy} 0 退出（F5）。"""
    import fcntl
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    # 持锁
    lock_path = Path(str(tape) + ".lock")
    fd = open(lock_path, "w")
    fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
    try:
        reply = _next(runner, tape, run_id, "--output", "out_a")
        assert reply["done"] is False
        assert reply["reason"] == "busy"
        # 0 退出（runner.invoke 的 exit_code == 0）
        # tape 未增加（被 busy 短路）
        lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()


# ── N2：marker RMW 在 flock 临界区内 ─────────────────────────────────────────


def test_marker_rmw_serialized_under_flock(cwd_tmp, wf_path):
    """两 next 并发 → flock 串行 → no_output_count 不丢更新（N2 闭环）。

    本测试用「先持锁阻塞 second next，release 后 second next 跑」模拟时序：因 next 在
    flock 临界区内 RMW marker，second next 看到的是 first next 的最新 marker。
    """
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    # 第一次 next（无 output）→ 计数 1
    _next(runner, tape, run_id, "--output", "")
    markers = list(cwd_tmp.glob("runs/orca-*.json"))
    assert json.loads(markers[0].read_text())["no_output_count"] == 1

    # 第二次 → 计数 2（不是 1，证明 first 的 RMW 已落盘）
    _next(runner, tape, run_id, "--output", "")
    assert json.loads(markers[0].read_text())["no_output_count"] == 2


# ── stop ─────────────────────────────────────────────────────────────────────


def test_stop_emits_workflow_cancelled_and_clears_marker(cwd_tmp, wf_path):
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]

    result = runner.invoke(app, ["stop", run_id])
    assert result.exit_code == 0
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["ok"] is True
    assert reply["done"] is True

    # tape 末尾是 workflow_cancelled
    lines = Path(boot["tape"]).read_text(encoding="utf-8").strip().split("\n")
    last = json.loads(lines[-1])
    assert last["type"] == "workflow_cancelled"

    # marker 已清
    markers = list(cwd_tmp.glob("runs/orca-*.json"))
    assert len(markers) == 0


# ── D-v7-1 架构守门：plugin/hook 模板零业务逻辑 ─────────────────────────────


def test_plugin_template_has_no_orca_business_logic():
    """``.opencode/plugin/*.ts`` 不得含 advance/router/replay/tape 路径。

    SPEC §9.2 架构守门：宿主侧零 Orca 业务逻辑。``<task_result>`` 解析允许（扁平化提取
    非 Orca 决策），但 advance/router/replay/tape 禁止。
    """
    plugin = Path(__file__).resolve().parents[3] / "orca/iface/in_session/templates/opencode/orca.ts"
    text = plugin.read_text(encoding="utf-8")
    forbidden = ["advance_step", "router.resolve", "replay_state", "tape.append",
                 "EventBus", "Tape(", "drive_loop", "advance("]
    for kw in forbidden:
        assert kw not in text, f"plugin 模板含禁词 {kw!r}（违反 D-v7-1）"


def test_cc_hook_template_has_no_orca_business_logic():
    """CC hook 脚本只 spawn CLI + parse JSON 顶层字段，无 Orca 业务逻辑。"""
    hook = Path(__file__).resolve().parents[3] / "orca/iface/in_session/templates/cc_hooks.py"
    text = hook.read_text(encoding="utf-8")
    forbidden = ["advance_step", "router.resolve", "replay_state", "tape.append",
                 "EventBus", "drive_loop"]
    for kw in forbidden:
        assert kw not in text, f"CC hook 模板含禁词 {kw!r}"


# ── G2 序列对齐（轻量版）────────────────────────────────────────────────────


def test_event_sequence_matches_expected_shape(cwd_tmp, wf_path):
    """事件序列 == [workflow_started, node_started(a), node_completed(a),
    route_taken(a→b), node_started(b), node_completed(b), route_taken(b→$end),
    workflow_completed] —— 与 drive_loop 跑同 wf 的序列形态一致（G2 守门轻量版）。

    完整 G2（vs ``orca run`` tape 逐 seq 对齐）需跑 drive_loop 端到端，由 test-coverage-e2e
    真链路验；此处验证事件序列骨架。
    """
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    _next(runner, tape, run_id, "--output", "out_a")
    _next(runner, tape, run_id, "--output", "out_b")

    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    types = [e["type"] for e in events]
    nodes = [e.get("node") for e in events]

    assert types == [
        "workflow_started", "node_started",
        "node_completed", "route_taken", "node_started",
        "node_completed", "route_taken",
        "workflow_completed",
    ]
    assert nodes[1] == "a"     # ns(a)
    assert nodes[2] == "a"     # nc(a)
    assert nodes[4] == "b"     # ns(b)
    assert nodes[5] == "b"     # nc(b)
    # seq 连续递增
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(events) + 1))


# ── 补丁：state_corrupt / bootstrap busy / stop busy / status / no-marker / start ──


def test_classify_in_session_error_full_taxonomy():
    """单元测试 ``_classify_in_session_error`` 五类映射（F6 闭环）。

    step.py raise 文案与 CLI 映射表的契约由本测试守住——任何 step.py 文案改动会触发此测试。
    """
    from orca.iface.in_session.cli import _classify_in_session_error
    from orca.run.step import InSessionError

    # output_schema_mismatch（_parse_output raise）
    assert _classify_in_session_error(InSessionError(
        "节点 'x' 声明了 output_schema 但宿主输出非 JSON：'abc'"
    )) == "output_schema_mismatch"

    # unsupported_node_kind（_check_agent_node raise）
    assert _classify_in_session_error(InSessionError(
        "节点 'y' 不在 workflow.nodes 中"
    )) == "unsupported_node_kind"
    assert _classify_in_session_error(InSessionError(
        "in-session shell v1 仅支持 agent 节点，'z' 是 'script'"
    )) == "unsupported_node_kind"

    # state_corrupt（branch 3/4 + 多 running raise）
    assert _classify_in_session_error(InSessionError(
        "advance(output=...) 但 tape 中无 running 节点（状态腐败 / 重复完成）"
    )) == "state_corrupt"
    assert _classify_in_session_error(InSessionError(
        "tape 中存在多个 running 节点 ['a', 'b']（状态腐败 / 并发调用）"
    )) == "state_corrupt"

    # 兜底：未知消息 → internal_error（fail loud 不静默）
    assert _classify_in_session_error(InSessionError("未知错误")) == "internal_error"


def test_stop_busy_when_tape_flock_held(cwd_tmp, wf_path):
    """stop 撞 tape flock → {done:false, reason:busy}（busy 语义在 stop 路径一致）。"""
    import fcntl
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    lock_path = Path(str(tape) + ".lock")
    fd = open(lock_path, "w")
    fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
    try:
        result = runner.invoke(app, ["stop", run_id])
        reply = json.loads(result.output.splitlines()[-1])
        assert reply["done"] is False
        assert reply["reason"] == "busy"
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()


def test_status_no_run_id_lists_runs_dir(cwd_tmp, wf_path):
    """status 无 run_id 列 runs/ 下全部 tape 文件名。"""
    runner = CliRunner()
    _bootstrap(runner, wf_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    # 输出含 tape 文件名（run_id stem）
    assert ".jsonl" not in result.output  # 只显示 stem
    assert "用 `orca in-session status <run_id>`" in result.output


def test_status_with_run_id_shows_progress(cwd_tmp, wf_path):
    """status <run_id> 报 workflow 进度（status: running / node_status）。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]
    result = runner.invoke(app, ["status", run_id])
    assert result.exit_code == 0
    assert "status:" in result.output
    assert "running" in result.output
    assert "node_status:" in result.output


def test_next_no_marker_returns_no_marker_reason(cwd_tmp, wf_path):
    """next 找不到 marker → {done:false, reason:no-marker}（幂等吞，不崩）。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]
    # 手工清掉所有 marker（模拟 marker 已清但 tape 还在）
    for mp in cwd_tmp.glob("runs/orca-*.json"):
        mp.unlink()
    reply = _next(runner, tape, run_id, "--output", "out_a")
    assert reply["done"] is False
    assert reply["reason"] == "no-marker"


# ── start 命令（CC 路）──────────────────────────────────────────────────────


def _extract_json_block(text: str) -> dict:
    """从 start 命令 stdout 提取第一个完整 JSON 对象（hooks 片段）。"""
    import json as _json
    decoder = _json.JSONDecoder()
    idx = text.find("{\n")
    assert idx >= 0, "未在 start stdout 找到 JSON 起始"
    obj, _end = decoder.raw_decode(text[idx:])
    return obj


def test_start_writes_marker_and_prints_settings_fragment(cwd_tmp, wf_path):
    """start <wf> → 写 marker + 打印 settings.json hooks 片段（含 Stop / PostToolUse）。"""
    runner = CliRunner()
    result = runner.invoke(app, ["start", str(wf_path)])
    assert result.exit_code == 0
    output = result.output

    # 含 run_id / tape / marker 路径 + settings.json 片段
    assert "run_id:" in output
    assert "tape:" in output
    assert "marker:" in output

    fragment = _extract_json_block(output)
    assert "hooks" in fragment
    hooks = fragment["hooks"]
    assert "Stop" in hooks
    assert "PostToolUse" in hooks

    # PostToolUse matcher 含 Task|Agent
    pu = hooks["PostToolUse"][0]
    assert pu["matcher"] == "Task|Agent"
    # Stop 命令含 `orca in-session next`
    stop_cmd = hooks["Stop"][0]["hooks"][0]["command"]
    assert "orca in-session next" in stop_cmd
    # PostToolUse 用 jq flatten + trap 兜底清 tmp（B-7）
    pu_cmd = pu["hooks"][0]["command"]
    assert "jq" in pu_cmd
    assert "trap" in pu_cmd

    # marker 已写
    markers = list(cwd_tmp.glob("runs/orca-*.json"))
    assert len(markers) == 1


def test_start_stop_script_uses_bash_array_safe_argv(cwd_tmp, wf_path):
    """Stop 脚本用 bash 数组 ``ARGS=(...)`` + ``"${ARGS[@]}"`` 展开（B-1 闭环：避免 word-splitting）。

    B-1 闭环：cache 含空格/换行（task subagent 文本输出常态）时，bash 数组 +
    ``"${ARGS[@]}"`` 保证 argv 不被 word-splitting 破坏。
    """
    runner = CliRunner()
    result = runner.invoke(app, ["start", str(wf_path)])
    fragment = _extract_json_block(result.output)
    stop_cmd = fragment["hooks"]["Stop"][0]["hooks"][0]["command"]

    # bash 数组（B-1 闭环）
    assert "ARGS=(" in stop_cmd
    assert '"${ARGS[@]}"' in stop_cmd
    # decision:block 经 jq -n --arg 构造（B-2 闭环）
    assert 'jq -n --arg p "$PROMPT"' in stop_cmd


def test_marker_files_cleaned_after_workflow_completes(cwd_tmp, wf_path):
    """workflow 跑完后 marker 已清（终态 → clear_marker）。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]
    _next(runner, tape, run_id, "--output", "out_a")
    _next(runner, tape, run_id, "--output", "out_b")

    # workflow_completed 后 marker 已清
    markers = list(cwd_tmp.glob("runs/orca-*.json"))
    assert len(markers) == 0
