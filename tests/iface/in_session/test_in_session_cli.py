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
    # 默认带 --inputs "{}" 真启动（bootstrap 不带 --inputs 只返 inputs_schema 不启动）。
    # 调用方经 extra 传 --inputs 时 click last-wins 覆盖默认。
    result = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}", *extra])
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


def test_bootstrap_duplicate_same_wf_fails_loud(cwd_tmp, wf_path):
    """v3 §7.3（m12）：同 wf 已有活跃 marker（未终态）再 bootstrap → fail loud。

    取代旧 N1「同 owner+yaml 复用 run_id」——v3 改 fail loud 防孤儿（marker 只 3 字段，
    无 owner；按 wf.name 经 tape workflow_started 匹配活跃 run）。
    """
    runner = CliRunner()
    r1 = _bootstrap(runner, wf_path)
    # 第二次 bootstrap 同 wf（同 wf.name，未终态）→ fail loud（exit 1）。
    result = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    assert result.exit_code == 1
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["reason"] == "duplicate-active-run"
    assert reply["run_id"] == r1["run_id"]
    assert "orca next" in reply["hint"]
    assert "orca stop" in reply["hint"]

    # tape 仍只 2 行（第二次未 emit；fail loud 在 emit 前）。
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
    # v5 §8 step 5b：信封加 error_kind（与 tape data.kind 同值，字段名不同——B4/B7）
    assert reply["error_kind"] == "output_schema_mismatch"
    # 反向守门：信封不得出现塌缩值 "in_session_error"（旧 isinstance 分流已消除）
    assert "in_session_error" not in json.dumps(reply)

    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    last = json.loads(lines[-1])
    assert last["type"] == "workflow_failed"
    assert last["data"]["kind"] == "output_schema_mismatch"
    assert "in_session_error" not in json.dumps(last["data"])


def test_failure_unsupported_node_kind(cwd_tmp):
    """script 节点（非 agent）→ workflow_failed(unsupported_node_kind)。"""
    yaml_text = AGENT_WF_YAML.replace("kind: agent", "kind: script", 1).replace(
        '    executor: opencode\n    model: deepseek/deepseek-v4-flash\n    prompt: "产出 step A 的输出。"',
        '    command: "echo a"',
    )
    p = cwd_tmp / "wf_script.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["bootstrap", str(p), "--inputs", "{}"])
    assert result.exit_code == 1
    # v5 §8 step 5b：bootstrap 失败信封加 error_kind（unsupported_node_kind）+ 反向无 in_session_error
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is True
    assert reply["error_kind"] == "unsupported_node_kind"
    assert "in_session_error" not in json.dumps(reply)


def test_failure_output_schema_field_violation(cwd_tmp):
    """output_schema 声明 + output 是合法 JSON 但缺 required 字段 → output_schema_mismatch。

    2026-07-08：``_parse_output`` 加 jsonschema 字段校验。缺字段在 parse 期被抓（早于
    下游 render 的 UndefinedError），归类 output_schema_mismatch，干净 workflow_failed。
    （此前仅 json.loads，缺字段漏到 render → 脏崩溃。）
    """
    yaml_text = AGENT_WF_YAML.replace(
        'prompt: "产出 step A 的输出。"',
        'prompt: "产出 step A 的输出。"\n    output_schema:\n      type: object\n      required: [k]\n      properties:\n        k: {type: string}',
    )
    p = cwd_tmp / "wf_schema_field.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    runner = CliRunner()
    boot = _bootstrap(runner, p)
    run_id, tape = boot["run_id"], boot["tape"]

    # 合法 JSON 但缺 required 字段 k
    result = runner.invoke(app, [
        "next", "--tape", tape, "--run-id", run_id, "--output", '{"x": 1}',
    ])
    assert result.exit_code == 1
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is True
    assert "failed" in reply["reason"]
    # v5 §8 step 5b：信封 error_kind + 反向无 in_session_error
    assert reply["error_kind"] == "output_schema_mismatch"
    assert "in_session_error" not in json.dumps(reply)
    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    last = json.loads(lines[-1])
    assert last["type"] == "workflow_failed"
    assert last["data"]["kind"] == "output_schema_mismatch"


def test_advance_step_inline_fallback_vs_compact(tmp_path):
    """``advance_step`` 交付模式守门：``prompts_dir=None`` → inline 全量 prompt；给定 → compact 文件。

    daemon / 直调单测走 inline（``prompts_dir=None``）；生产 bootstrap/next 走 compact。
    两模式渲染内容一致（同 ``render_prompt`` 输出），仅交付载体不同。
    """
    from orca.compile import load_workflow
    from orca.events.tape import Tape
    from orca.run.step import advance_step

    wf_path = tmp_path / "wf.yaml"
    wf_path.write_text(AGENT_WF_YAML, encoding="utf-8")
    wf = load_workflow(wf_path)

    # inline 回退（prompts_dir=None）：prompt=渲染全文、prompt_file=None、resources_root=None
    tape1 = Tape(tmp_path / "tape1.jsonl", run_id="r1", resume=True)
    res_inline = advance_step(tape1, wf, run_id="r1", prompts_dir=None)
    assert res_inline.prompt_file is None, "inline 回退不落盘"
    assert res_inline.resources_root is None
    assert res_inline.prompt and "产出 step A" in res_inline.prompt, (
        "inline 回退返全量渲染 prompt（含 entry 指令文本）"
    )

    # compact（prompts_dir 给定）：prompt=None、prompt_file 落盘、文件内容 == inline 全量
    tape2 = Tape(tmp_path / "tape2.jsonl", run_id="r2", resume=True)
    pdir = tmp_path / "prompts"
    res_compact = advance_step(tape2, wf, run_id="r2", prompts_dir=pdir)
    assert res_compact.prompt is None, "compact 不返全量 prompt（只返指针，由 cli 拼）"
    assert res_compact.prompt_file is not None
    compact_text = Path(res_compact.prompt_file).read_text(encoding="utf-8")
    assert compact_text == res_inline.prompt, (
        "compact 落盘内容必须 == inline 全量渲染文本（同 render_prompt 输出）"
    )


def _spy_tape_replay(tape) -> tuple[callable, list[int]]:
    """spy ``tape.replay``：返回 ``(patched_replay, calls)``，calls 收集每次调用的 since_seq。

    用 list 而非 nonlocal int：调用方拿到引用即可读取最终计数，避免闭包陷阱。
    保 lazy 生成器：返回 ``original_replay(*args, **kwargs)`` 不消费迭代器。
    """
    calls: list[int] = []
    original_replay = tape.replay

    def counting_replay(since_seq: int = 0):
        calls.append(since_seq)
        return original_replay(since_seq)

    tape.replay = counting_replay  # type: ignore[method-assign]
    return counting_replay, calls


def test_advance_step_bootstrap_traverses_tape_once(tmp_path):
    """SPEC §3 O1a AC：``advance_step`` bootstrap 分支（pending）单次调用 tape 遍历 2→1。

    重构前：``replay_state(tape)`` + ``Orchestrator._inputs_from_tape(tape)`` = 2 次遍历。
    重构后：``_replay_state_and_inputs(tape)`` = 1 次遍历。
    """
    from orca.compile import load_workflow
    from orca.events.tape import Tape
    from orca.run.step import advance_step

    wf_path = tmp_path / "wf.yaml"
    wf_path.write_text(AGENT_WF_YAML, encoding="utf-8")
    wf = load_workflow(wf_path)

    # bootstrap 场景：空 tape（pending 分支）
    tape = Tape(tmp_path / "tape_boot.jsonl", run_id="r1", resume=True)
    _, calls = _spy_tape_replay(tape)

    res = advance_step(tape, wf, run_id="r1", prompts_dir=None)
    assert res.node == "a", "bootstrap 应推到 entry 节点 a"
    # StepResult 形状断言：bootstrap 应 emit workflow_started + node_started(a) 两条
    # （若 advance_step 在 _replay_state_and_inputs 之前 short-circuit 返回，emits 会空，
    # 配合下方 calls==1 双锁，加速回归定位）。
    assert len(res.emits) == 2, (
        f"bootstrap 应 emit [workflow_started, node_started] 两条，实得 {len(res.emits)} 条"
    )

    assert len(calls) == 1, (
        f"advance_step (bootstrap) 应只遍历 tape 一次（SPEC §3 O1a），实得 {len(calls)} 次"
    )


def test_advance_step_next_traverses_tape_once(tmp_path):
    """SPEC §3 O1a AC：``advance_step`` advance 分支（output 给出）单次调用 tape 遍历 2→1。

    next 路径有 output 推进：重构前同样 2 次遍历（replay_state + _inputs_from_tape），
    重构后 1 次。两分支独立断言（pending/advance 各自走 merged call site）。
    """
    import asyncio

    from orca.compile import load_workflow
    from orca.events.tape import Tape
    from orca.run.step import advance_step

    wf_path = tmp_path / "wf.yaml"
    wf_path.write_text(AGENT_WF_YAML, encoding="utf-8")
    wf = load_workflow(wf_path)

    # 预填 tape：workflow_started + node_started(a) → 推进 a 时 advance 分支
    pre_tape = Tape(tmp_path / "tape_next.jsonl", run_id="r1")
    try:
        asyncio.run(pre_tape.append({
            "type": "workflow_started", "timestamp": 1.0, "node": None,
            "session_id": None,
            "data": {"workflow_name": "cli_test_wf", "entry": "a",
                     "inputs": {"task": "demo"}, "yaml_path": str(wf_path)},
        }))
        asyncio.run(pre_tape.append({
            "type": "node_started", "timestamp": 2.0, "node": "a",
            "session_id": "s1", "data": {"kind": "agent"},
        }))
    finally:
        pre_tape.close()

    # 重开只读 tape + spy
    tape = Tape(tmp_path / "tape_next.jsonl", run_id="r1", resume=True)
    _, calls = _spy_tape_replay(tape)

    res = advance_step(tape, wf, run_id="r1", prompts_dir=None, output="out_from_a")
    assert res.done is False, "a → b 推进，workflow 未完"
    assert res.node == "b", "a 完成后应推到 b"
    # StepResult 形状断言：advance 应 emit [node_completed(a), route_taken, node_started(b)]
    # 三条（同上，配合 calls==1 双锁，加速回归定位）。
    assert len(res.emits) == 3, (
        f"advance 应 emit [node_completed, route_taken, node_started] 三条，"
        f"实得 {len(res.emits)} 条"
    )

    assert len(calls) == 1, (
        f"advance_step (advance/output) 应只遍历 tape 一次（SPEC §3 O1a），实得 {len(calls)} 次"
    )


def test_failure_output_schema_malformed(cwd_tmp):
    """output_schema 自身畸形（YAML 写错）→ 干净 workflow_failed（review 🔴，D-v8.x-2）。

    ``jsonschema.validate`` 对畸形 schema 抛 ``SchemaError``（非 ``ValidationError`` 子类）。
    只 catch ValidationError 会逃逸成脏崩溃。``_parse_output`` 必须 catch 两者。
    """
    # required 元素非字符串 → SchemaError
    yaml_text = AGENT_WF_YAML.replace(
        'prompt: "产出 step A 的输出。"',
        'prompt: "产出 step A 的输出。"\n    output_schema:\n      type: object\n      required: [1, 2, 3]',
    )
    p = cwd_tmp / "wf_schema_malformed.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    runner = CliRunner()
    boot = _bootstrap(runner, p)
    run_id, tape = boot["run_id"], boot["tape"]

    result = runner.invoke(app, [
        "next", "--tape", tape, "--run-id", run_id, "--output", '{"k": "v"}',
    ])
    assert result.exit_code == 1, f"malformed schema 应 fail loud，实得 exit 0: {result.output}"
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is True
    assert "failed" in reply["reason"]
    # v5 §8 step 5b：信封 error_kind + 反向无 in_session_error
    assert reply["error_kind"] == "output_schema_mismatch"
    assert "in_session_error" not in json.dumps(reply)
    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    last = json.loads(lines[-1])
    assert last["type"] == "workflow_failed"
    # 归类 output_schema_mismatch（消息含 output_schema；语义为 schema 畸形见 message）
    assert last["data"]["kind"] == "output_schema_mismatch"


def test_failure_render_error_clears_marker(cwd_tmp):
    """下游 prompt 引用上游缺失字段（无 schema）→ 干净 workflow_failed(render_error) + 清 marker。

    2026-07-08 bug 闭环：此前 render 抛 ``ExecError``（非 InSessionError）逃逸 cli.py 的
    ``except InSessionError`` → 脏崩溃（无 workflow_failed、不清 marker、tape 悬挂、下次卡死）。
    现 ``_render_or_fail`` 把 ExecError 包成 InSessionError("渲染节点…") → 走既有干净路径。

    v5 §8 step 5b：从此前误并入 ``test_failure_output_schema_malformed`` 函数体（裸字符串
    表达式分隔）拆出为独立测试（code-reviewer Round 2 m1）——两者 yaml/run/断言皆独立，
    合并会误导诊断方向。
    """
    # node b 引用 a.output.nope（a 无 schema，output 是自由文本，无 nope 字段）
    yaml_text = AGENT_WF_YAML.replace(
        'prompt: "基于 {{ a.output }} 总结。"',
        'prompt: "基于 {{ a.output.nope }} 总结。"',
    )
    p = cwd_tmp / "wf_render_err.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    runner = CliRunner()
    boot = _bootstrap(runner, p)
    run_id, tape = boot["run_id"], boot["tape"]

    result = runner.invoke(app, [
        "next", "--tape", tape, "--run-id", run_id, "--output", "OUT_A",
    ])
    assert result.exit_code == 1
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is True
    assert "failed" in reply["reason"]
    # v5 §8 step 5b：信封 error_kind(render_error) + 反向无 in_session_error
    assert reply["error_kind"] == "render_error"
    assert "in_session_error" not in json.dumps(reply)

    # 干净终态：tape 末条 workflow_failed(render_error)
    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    last = json.loads(lines[-1])
    assert last["type"] == "workflow_failed"
    assert last["data"]["kind"] == "render_error", (
        f"render 错必须归类 render_error，实得 {last['data']['kind']}"
    )

    # marker 已清（不卡死）：runs/orca-<owner>.json 不存在（owner=run_id，CLI bootstrap 路径）
    marker = Path(tape).parent / f"orca-{run_id}.json"
    assert not marker.exists(), (
        f"render_error 终态后 marker 必须清除（否则下次 /orca run 卡死），仍存在：{marker}"
    )


# ── F5：LOCK_NB busy ────────────────────────────────────────────────────────


def test_next_busy_when_lock_held(cwd_tmp, wf_path):
    """并发撞锁：另一进程持 tape flock → next 返 {done:false, reason:busy} 0 退出（F5）。

    SPEC §3 O4：busy 信封含 ``retry_after_ms``（500ms），主 session 据它等待重试。
    """
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
        # SPEC §3 O4：retry_after_ms 必出，主 session 据它等待重试同一 next（不重派子代理）。
        assert reply["retry_after_ms"] == 500
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
    """``.opencode/plugins/*.ts`` 不得含 advance/router/replay/tape 路径。

    SPEC §9.2 架构守门：宿主侧零 Orca 业务逻辑。``<task_result>`` 解析允许（扁平化提取
    非 Orca 决策），但 advance/router/replay/tape 禁止。
    """
    plugin = Path(__file__).resolve().parents[3] / "orca/iface/in_session/templates/opencode/orca.ts"
    text = plugin.read_text(encoding="utf-8")
    forbidden = ["advance_step", "router.resolve", "replay_state", "tape.append",
                 "EventBus", "Tape(", "drive_loop", "advance("]
    for kw in forbidden:
        assert kw not in text, f"plugin 模板含禁词 {kw!r}（违反 D-v7-1）"


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


def test_classify_in_session_error_uses_explicit_kind():
    """``_classify_in_session_error`` 直读 ``exc.error_kind``（取代消息子串匹配，类型安全）。

    分类由 step.py 各 raise 处传 ``error_kind=ERR_*`` 显式携带；本测试守门「显式字段」契约：
    每个常量经 classifier 直返同名；缺省 → ``internal_error`` 兜底。
    raise 文案改动**不应**再触发本测试（旧子串匹配已移除）。

    v5 §8 step 5b：``_classify_in_session_error`` 抽到 ``_step_io`` helper（daemon/cli 共享，
    ``fail_in_session`` 内部调用）。import 直接从 helper。
    """
    from orca.iface.in_session._step_io import _classify_in_session_error
    from orca.run.step import (
        ERR_OUTPUT_SCHEMA_MISMATCH, ERR_RENDER_ERROR, ERR_UNSUPPORTED_NODE_KIND,
        ERR_STATE_CORRUPT, ERR_INTERNAL_ERROR, InSessionError,
    )

    # 各 taxonomy 常量经 classifier 直返（消息任意，不参与分类）
    for kind in (ERR_OUTPUT_SCHEMA_MISMATCH, ERR_RENDER_ERROR,
                 ERR_UNSUPPORTED_NODE_KIND, ERR_STATE_CORRUPT):
        assert _classify_in_session_error(
            InSessionError("任意文案，不参与分类", error_kind=kind)
        ) == kind

    # 缺省 error_kind → internal_error 兜底（fail loud 不静默）
    assert _classify_in_session_error(InSessionError("未知错误")) == ERR_INTERNAL_ERROR


def test_stop_busy_when_tape_flock_held(cwd_tmp, wf_path):
    """stop 撞 tape flock → {done:false, reason:busy}（busy 语义在 stop 路径一致）。

    SPEC §3 O4：busy 信封含 ``retry_after_ms``（与 next 路径一致）。
    """
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
        assert reply["retry_after_ms"] == 500  # SPEC §3 O4
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()


def test_bootstrap_busy_when_tape_flock_held(cwd_tmp, wf_path, monkeypatch):
    """bootstrap 撞 tape flock → {done:false, reason:busy, retry_after_ms:500}（SPEC §3 O4）。

    bootstrap 撞锁罕见（通常首调），但路径要一致：3 处 busy 信封（bootstrap/next/stop）
    统一形态，主 session 据 retry_after_ms 等待重试。

    模拟方式：monkeypatch ``_try_acquire_flock`` 在 bootstrap 路径返 None。bootstrap 在
    gen run_id 后才调 ``_try_acquire_flock``（global dupe-check lock 释后），所以副作用
    仅在 bootstrap 内部，不污染其它测试。
    """
    runner = CliRunner()
    from orca.iface.in_session import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_try_acquire_flock", lambda tape_path: None)

    result = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    # bootstrap 撞锁返 busy，exit_code 0（与 next 一致；非 fail loud）
    assert result.exit_code == 0, result.output
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is False
    assert reply["reason"] == "busy"
    assert reply["retry_after_ms"] == 500


def test_status_no_run_id_lists_runs_dir(cwd_tmp, wf_path):
    """status 无 run_id 列 runs/ 下全部 tape 文件名。"""
    runner = CliRunner()
    _bootstrap(runner, wf_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    # 输出含 tape 文件名（run_id stem）
    assert ".jsonl" not in result.output  # 只显示 stem
    # 提示文案统一用 --run-id 形态（spec §2.1 / DEFECT-2：SKILL.md/spec/CLI 三处一致）。
    assert "用 `orca status --run-id <run_id>`" in result.output


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


def test_status_run_id_option_mirrors_positional(cwd_tmp, wf_path):
    """DEFECT-2：``status --run-id <id>`` 与位置参数 ``status <id>`` 等价（spec §2.1）。

    SKILL.md / spec §2.1 都写 ``--run-id``；旧 CLI 只接位置参数 → 主 session 照文档跑报错。
    现在两种形态都接受，输出一致。
    """
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]

    positional = runner.invoke(app, ["status", run_id])
    option = runner.invoke(app, ["status", "--run-id", run_id])
    assert positional.exit_code == 0, positional.output
    assert option.exit_code == 0, option.output
    # 输出一致（同一 run 同一时刻 replay_state 相同）
    assert "running" in option.output
    assert "node_status:" in option.output
    assert positional.output == option.output


def test_status_run_id_option_with_json_flag(cwd_tmp, wf_path):
    """DEFECT-2：``status --run-id <id> --json`` 走 JSON 出口（spec §2.3 单 run 详情契约）。"""
    import json as _json
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]

    result = runner.invoke(app, ["status", "--run-id", run_id, "--json"])
    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output.splitlines()[-1])
    assert payload["run_id"] == run_id
    assert payload["status"] == "running"


def test_status_positional_and_option_same_value_ok(cwd_tmp, wf_path):
    """DEFECT-2：位置参数与 --run-id 同传且**同值** → 视作一次（容错；用户复制粘贴常见）。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]
    result = runner.invoke(app, ["status", run_id, "--run-id", run_id])
    assert result.exit_code == 0, result.output
    assert "running" in result.output


def test_status_positional_and_option_conflict_fails_loud(cwd_tmp, wf_path):
    """DEFECT-2：位置参数与 --run-id 同传且**不同值** → fail loud（BadParameter，铁律 12）。

    不静默选其中一个——让用户看到两条路冲突，明确报错。
    """
    runner = CliRunner()
    _bootstrap(runner, wf_path)
    result = runner.invoke(app, ["status", "rid-a", "--run-id", "rid-b"])
    assert result.exit_code != 0
    # BadParameter 提示含两个值（让用户看到冲突来源）
    assert "rid-a" in result.output
    assert "rid-b" in result.output


def test_status_run_id_option_nonexistent_fails_loud(cwd_tmp, wf_path):
    """DEFECT-2 review MINOR#2：``--run-id <不存在>`` 走「无 tape」错误分支（与位置参数等价）。

    锁住「双形态在错误路径也等价」——防归一逻辑（rid = ...）在错误分支意外分叉。
    """
    runner = CliRunner()
    _bootstrap(runner, wf_path)  # 建 runs/ 但不创建 ghost-run 的 tape

    positional = runner.invoke(app, ["status", "ghost-run"])
    option = runner.invoke(app, ["status", "--run-id", "ghost-run"])
    assert positional.exit_code == 1, "位置参数形态：无 tape 应 exit 1"
    assert option.exit_code == 1, "--run-id 形态：无 tape 应 exit 1（与位置参数等价）"
    assert "ghost-run" in positional.output
    assert "ghost-run" in option.output


# ── FU-1：stop/open 加 --run-id option（spec §2.1 命令族统一，套 DEFECT-2 e763e9e）────
#
# stop 是**破坏性**（首次 stop 清 marker + emit cancelled），非只读——不能套 status 的
# 「同 run 调两次 byte-equal」。改用「两独立 run 各停一次」证双形态同构 observable。
# stop 无 --json；stop 的 nonexistent run 是**幂等 ok（exit 0）**非 fail-loud（exit 1）。


def _stop_observable(result) -> dict:
    """从 stop 结果抽取可比 observable：exit / ok / done / note。"""
    obs = {"exit": result.exit_code}
    if result.exit_code == 0:
        obs.update(json.loads(result.output.splitlines()[-1]))
    return obs


def test_stop_run_id_option_mirrors_positional(cwd_tmp, wf_path):
    """FU-1：``stop --run-id <id>`` 与位置参数 ``stop <id>`` 同构（spec §2.1）。

    stop 是破坏性，故用**两个独立 run** 各停一次（一 positional 一 option），断言
    observable（ok/done）一致 + 各自 marker 已清 + tape 末尾 workflow_cancelled。
    """
    runner = CliRunner()
    # 两独立 run（各 bootstrap 一次），分别用两种形态停。
    boot_a = _bootstrap(runner, wf_path)
    rid_a = boot_a["run_id"]
    pos = runner.invoke(app, ["stop", rid_a])
    assert pos.exit_code == 0, pos.output

    boot_b = _bootstrap(runner, wf_path)
    rid_b = boot_b["run_id"]
    opt = runner.invoke(app, ["stop", "--run-id", rid_b])
    assert opt.exit_code == 0, opt.output

    # 双形态 observable 同构（ok/done 一致；run_id 各自正确）
    pos_reply = json.loads(pos.output.splitlines()[-1])
    opt_reply = json.loads(opt.output.splitlines()[-1])
    assert pos_reply["ok"] is True and pos_reply["done"] is True
    assert opt_reply["ok"] is True and opt_reply["done"] is True
    assert pos_reply["run_id"] == rid_a
    assert opt_reply["run_id"] == rid_b

    # 各自 tape 末尾 workflow_cancelled + marker 清（破坏性 observable 一致）
    for boot in (boot_a, boot_b):
        lines = Path(boot["tape"]).read_text(encoding="utf-8").strip().split("\n")
        assert json.loads(lines[-1])["type"] == "workflow_cancelled"
    assert list(cwd_tmp.glob("runs/orca-*.json")) == []


def test_stop_positional_and_option_same_value_ok(cwd_tmp, wf_path):
    """FU-1：``stop <id> --run-id <同 id>`` → 视作一次（容错；用户复制粘贴常见）。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]
    result = runner.invoke(app, ["stop", run_id, "--run-id", run_id])
    assert result.exit_code == 0, result.output
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["ok"] is True and reply["run_id"] == run_id


def test_stop_positional_and_option_conflict_fails_loud(cwd_tmp, wf_path):
    """FU-1：``stop <a> --run-id <b>`` → fail loud（BadParameter，铁律 12）。

    不静默选其一——让用户看到两条路冲突。三命令共用 ``_merge_run_id``，此处锁 stop 路径。
    """
    runner = CliRunner()
    _bootstrap(runner, wf_path)
    result = runner.invoke(app, ["stop", "rid-a", "--run-id", "rid-b"])
    assert result.exit_code != 0
    assert "rid-a" in result.output
    assert "rid-b" in result.output


def test_stop_missing_run_id_fails_loud(cwd_tmp, wf_path):
    """FU-1：``stop`` 无 run_id（位置参数与 --run-id 都省略）→ exit 2（fail loud）。

    stop 位置参数已从必填改为可选（让 ``stop --run-id X`` 不再因「缺位置参数」exit 2），
    故 None 由**显式守卫**拦下（``raise BadParameter``）。stop 无 status 的「无参列全部」
    模式——None 必须 fail loud（ISSUE-3，保 exit 2 回归）。
    """
    runner = CliRunner()
    result = runner.invoke(app, ["stop"])
    assert result.exit_code == 2


def test_stop_run_id_option_nonexistent_is_idempotent_ok(cwd_tmp, wf_path):
    """FU-1：``stop --run-id <不存在>`` → 幂等 ok（note=no-tape，exit 0），与位置参数等价。

    stop 的 nonexistent run 走幂等清 marker 分支（非 fail-loud）——v8.py:535 的 option 变体。
    锁住「双形态在 no-tape 路径也等价」。
    """
    runner = CliRunner()
    _bootstrap(runner, wf_path)  # 建 runs/，但不创建 ghost-run 的 tape

    positional = runner.invoke(app, ["stop", "nonexistent-run"])
    option = runner.invoke(app, ["stop", "--run-id", "nonexistent-run"])
    assert positional.exit_code == 0, "位置参数形态：no-tape 应幂等 ok exit 0"
    assert option.exit_code == 0, "--run-id 形态：no-tape 应幂等 ok exit 0（与位置参数等价）"
    assert _stop_observable(positional).get("note") == "no-tape"
    assert _stop_observable(option).get("note") == "no-tape"


def test_open_run_id_option_routes_to_open_run(cwd_tmp, wf_path):
    """FU-1：``open --run-id <id>`` 把合流后的 run_id 传给 ``_open_run_inproc``（spec §2.1）。

    mock ``_open_run_inproc`` 避免真起 web server；断言收到的 run_id 与退出码透传。
    """
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]

    with mock.patch(
        "orca.iface.in_session.cli._open_run_inproc", return_value=0
    ) as m:
        result = runner.invoke(app, ["open", "--run-id", run_id])
    assert result.exit_code == 0, result.output
    m.assert_called_once()
    assert m.call_args.args[0] == run_id


def test_open_positional_regression(cwd_tmp, wf_path):
    """FU-1：``open <id>`` 位置参数仍可用（向后兼容）。mock 同上。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]

    with mock.patch(
        "orca.iface.in_session.cli._open_run_inproc", return_value=0
    ) as m:
        result = runner.invoke(app, ["open", run_id])
    assert result.exit_code == 0, result.output
    assert m.call_args.args[0] == run_id


def test_open_positional_and_option_same_value_ok(cwd_tmp, wf_path):
    """FU-1：``open <id> --run-id <同 id>`` → 视作一次（容错；与 stop 对称，命令面锁同值合流）。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]

    with mock.patch(
        "orca.iface.in_session.cli._open_run_inproc", return_value=0
    ) as m:
        result = runner.invoke(app, ["open", run_id, "--run-id", run_id])
    assert result.exit_code == 0, result.output
    assert m.call_args.args[0] == run_id


def test_open_positional_and_option_conflict_fails_loud(cwd_tmp, wf_path):
    """FU-1：``open <a> --run-id <b>`` → fail loud（BadParameter，共用 ``_merge_run_id``）。"""
    runner = CliRunner()
    _bootstrap(runner, wf_path)
    result = runner.invoke(app, ["open", "rid-a", "--run-id", "rid-b"])
    assert result.exit_code != 0
    assert "rid-a" in result.output
    assert "rid-b" in result.output


def test_open_no_run_id_uses_active_default(cwd_tmp, wf_path):
    """FU-1：``open`` 都省略 → 取活跃 run 默认（None 不 fail loud，与 stop 区分）。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]

    with mock.patch(
        "orca.iface.in_session.cli._open_run_inproc", return_value=0
    ) as m:
        result = runner.invoke(app, ["open"])
    assert result.exit_code == 0, result.output
    assert m.call_args.args[0] == run_id


# ── _merge_run_id helper 单测（FU-1 ISSUE-5，DRY 防线）──────────────────────────


def test_merge_run_id_both_none_returns_none():
    """都省略 → None（调用方按命令语义处理：status 列全部 / stop fail loud / open 默认）。"""
    from orca.iface.in_session.cli import _merge_run_id
    assert _merge_run_id(None, None) is None


def test_merge_run_id_positional_only():
    """仅位置参数 → 透传。"""
    from orca.iface.in_session.cli import _merge_run_id
    assert _merge_run_id("rid", None) == "rid"


def test_merge_run_id_option_only():
    """仅 --run-id → 透传。"""
    from orca.iface.in_session.cli import _merge_run_id
    assert _merge_run_id(None, "rid") == "rid"


def test_merge_run_id_same_value_tolerated():
    """同值 → 容错返该值（用户复制粘贴常见）。"""
    from orca.iface.in_session.cli import _merge_run_id
    assert _merge_run_id("rid", "rid") == "rid"


def test_merge_run_id_conflict_fails_loud():
    """异值 → BadParameter（铁律 12，含两个冲突值）。"""
    import typer
    from orca.iface.in_session.cli import _merge_run_id
    with pytest.raises(typer.BadParameter):
        _merge_run_id("a", "b")


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


# ── marker 终态清理（v5 step 2b：start 命令已删，相关 start/cc_hooks 测试随之移除）──


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


# ── outputs 模板求值 + inputs 从 tape 恢复（2026-07-09 补丁）────────────────────


OUTPUTS_WF_YAML = """\
name: outputs_wf
description: 带 outputs 模板的 workflow（in-session outputs 求值测试）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 step A 的输出。"
    routes:
      - to: $end
outputs:
  result: "A={{ a.output }}"
"""


def test_next_completes_with_outputs_template_evaluated(cwd_tmp, tmp_path):
    """``wf.outputs`` 声明模板 → in-session 跑完应**求值**（不再 fail loud）。

    回归 2026-07-09 补丁：``_final_outputs`` 从 fail loud 改为 ``render_template``
    求 ``wf.outputs``（与 ``Orchestrator._evaluate_outputs`` 同源）。
    """
    wf_path = tmp_path / "wf_outputs.yaml"
    wf_path.write_text(OUTPUTS_WF_YAML, encoding="utf-8")
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    reply = _next(runner, tape, run_id, "--output", "out_a")
    assert reply["done"] is True

    # workflow_completed.data.outputs = 模板求值结果（{{ a.output }} → "out_a"）
    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    wc = json.loads(lines[-1])
    assert wc["type"] == "workflow_completed"
    assert wc["data"]["outputs"] == {"result": "A=out_a"}


INPUTS_WF_YAML = """\
name: inputs_wf
description: 非 entry 节点引用 inputs（inputs 从 tape 恢复测试）。
entry: a
inputs:
  task:
    type: string
    required: true
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "step A。"
    routes:
      - to: b
  - name: b
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "基于输入 {{ inputs.task }} 继续。"
    routes:
      - to: $end
"""


def test_next_recovers_inputs_from_tape_without_inputs_arg(cwd_tmp, tmp_path):
    """``next`` 不传 ``--inputs`` → 非 entry 节点 ``{{ inputs.* }}`` 仍正确渲染。

    回归 2026-07-09 补丁：``advance_step`` 改从 tape（``workflow_started.data.inputs``）
    恢复 inputs（同 ``Orchestrator._inputs_from_tape``），模型不必每步重传 ``--inputs``，
    且修掉非 entry 节点 ``{{ inputs.* }}`` 依赖 CLI 重传的隐患。
    """
    wf_path = tmp_path / "wf_inputs.yaml"
    wf_path.write_text(INPUTS_WF_YAML, encoding="utf-8")
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path, "--inputs", '{"task":"hello"}')
    run_id, tape = boot["run_id"], boot["tape"]

    # next 只传 --output，不传 --inputs → 推进到 b（inputs 从 tape 恢复）
    reply = _next(runner, tape, run_id, "--output", "out_a")
    assert reply["done"] is False
    assert reply["node"] == "b"

    # node b 的 prompt 文件应含渲染后的 inputs.task（从 tape 恢复，非 undefined）
    b_prompt = (Path(tape).parent / run_id / "prompts" / "b.md").read_text(encoding="utf-8")
    assert "hello" in b_prompt               # inputs.task 已渲染
    assert "{{ inputs.task" not in b_prompt  # 模板已求值，无残留未渲染标记


OUTPUTS_BAD_WF_YAML = """\
name: outputs_bad_wf
description: outputs 模板引用存在节点的缺失字段（过 compile 校验、render 期 fail-loud）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 step A 的输出。"
    routes:
      - to: $end
outputs:
  result: "{{ a.output.nonexistent }}"
"""


def test_next_outputs_template_render_failure_fails_loud(cwd_tmp, tmp_path):
    """outputs 模板引用存在节点的缺失字段 → render 期 fail loud（``ERR_RENDER_ERROR`` → workflow_failed），不静默返 {}。

    注：引用**不存在的节点**会被 compile validator 在 bootstrap 期拦下；此处用存在节点
    ``a`` 的缺失字段路径，过 compile 校验、在 ``_final_outputs`` render 期触发。
    """
    wf_path = tmp_path / "wf_outputs_bad.yaml"
    wf_path.write_text(OUTPUTS_BAD_WF_YAML, encoding="utf-8")
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    result = runner.invoke(app, [
        "next", "--tape", tape, "--run-id", run_id, "--output", "out_a",
    ])
    assert result.exit_code == 1   # fail loud
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is True
    assert "failed" in reply["reason"]

    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    last = json.loads(lines[-1])
    assert last["type"] == "workflow_failed"
    assert last["data"]["kind"] == "render_error"
