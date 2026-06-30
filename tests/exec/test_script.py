"""tests/exec/test_script.py —— ScriptExecutor（真 subprocess，无害命令，SPEC §7.7 / 计划 D.3）。

覆盖：
  - echo 成功 → node_completed.output.stdout
  - exit 1 → **node_completed**（非零不 fail loud，SPEC §4.6 业务语义）
  - timeout → node_failed(phase=timeout)
  - parse_json=True + 合法 JSON → output.json
  - parse_json=True + 非 JSON → output.json=None（降级不阻断）
  - Jinja2 command 渲染
"""

from __future__ import annotations

import asyncio

import pytest

from orca.exec.context import RunContext
from orca.exec.script import ScriptExecutor
from orca.schema import Event, ScriptNode


def _run(coro):
    return asyncio.run(coro)


async def _collect(node, ctx) -> list[Event]:
    exe = ScriptExecutor()
    return [ev async for ev in exe.exec(node, ctx)]


def _ctx(inputs=None, outputs=None) -> RunContext:
    return RunContext(inputs=inputs or {}, outputs=outputs or {}, run_id="r1")


# ── echo 成功 ────────────────────────────────────────────────────────────────


def test_echo_success():
    node = ScriptNode(name="s", command="echo hello")
    events = _run(_collect(node, _ctx()))
    assert events[0].type == "node_started"
    completed = events[-1]
    assert completed.type == "node_completed"
    assert completed.data["output"]["stdout"].strip() == "hello"
    assert completed.data["output"]["exit_code"] == 0


# ── 非零退出码不 fail loud（业务语义）────────────────────────────────────────


def test_nonzero_exit_not_fail_loud():
    """exit 1 → node_completed（非 node_failed），output.exit_code=1（SPEC §4.6）。

    脚本退出码是业务结果（如 evaluator 的 0=pass/1=fail），由路由判断，executor 不阻断。
    用 ``false``（POSIX 标准 builtin，恒返回 1）避免 shell ``exit`` 在 ``create_subprocess_shell``
    的差异。
    """
    node = ScriptNode(name="s", command="false")
    events = _run(_collect(node, _ctx()))
    types = [e.type for e in events]
    assert "node_failed" not in types
    assert types[-1] == "node_completed"
    assert events[-1].data["output"]["exit_code"] == 1


# ── timeout fail loud ────────────────────────────────────────────────────────


def test_timeout_fail_loud():
    """timeout=0.5 + sleep 10 → node_failed(phase=timeout)（SPEC §4.6 / §7.7）。"""
    node = ScriptNode(name="s", command="sleep 10", timeout=0.5)
    events = _run(_collect(node, _ctx()))
    failed = [e for e in events if e.type == "node_failed"]
    assert len(failed) == 1
    assert failed[0].data["phase"] == "timeout"
    assert failed[0].data["error_type"] == "ExecTimeout"
    # error 事件双发
    assert any(e.type == "error" and e.data["phase"] == "timeout" for e in events)


# ── parse_json ───────────────────────────────────────────────────────────────


def test_parse_json_success():
    """parse_json=True + stdout 是合法 JSON → output.json 解析结果（SPEC §4.6）。"""
    node = ScriptNode(name="s", command='echo \'{"a": 1, "b": 2}\'', parse_json=True)
    events = _run(_collect(node, _ctx()))
    assert events[-1].data["output"]["json"] == {"a": 1, "b": 2}


def test_parse_json_failure_degrades_to_none():
    """parse_json=True + stdout 非 JSON → output.json=None（降级不阻断，SPEC §4.6）。

    关键：不 fail loud（业务可经 output.json is None 判断）。
    """
    node = ScriptNode(name="s", command='echo "not json"', parse_json=True)
    events = _run(_collect(node, _ctx()))
    assert events[-1].type == "node_completed"  # 不是 node_failed
    assert events[-1].data["output"]["json"] is None


# ── Jinja2 command 渲染 ──────────────────────────────────────────────────────


def test_jinja2_command_rendered():
    """command 含 {{ inputs.x }} → 渲染后执行（SPEC §4.6 / §7.9）。"""
    node = ScriptNode(name="s", command="echo {{ inputs.msg }}")
    events = _run(_collect(node, _ctx(inputs={"msg": "rendered-msg"})))
    assert events[-1].data["output"]["stdout"].strip() == "rendered-msg"


def test_jinja2_command_render_failure_fail_loud():
    """command 引用未定义变量 → node_failed(phase=render)（SPEC §6）。"""
    node = ScriptNode(name="s", command="echo {{ undefined_thing }}")
    events = _run(_collect(node, _ctx()))
    failed = [e for e in events if e.type == "node_failed"]
    assert len(failed) == 1
    assert failed[0].data["phase"] == "render"


# ── 生命周期 + session_id 一致 ───────────────────────────────────────────────


def test_lifecycle_and_session_id_consistent():
    node = ScriptNode(name="s", command="echo ok")
    events = _run(_collect(node, _ctx()))
    assert events[0].type == "node_started"
    assert events[-1].type == "node_completed"
    sids = {e.session_id for e in events}
    assert len(sids) == 1
    assert next(iter(sids)) is not None
