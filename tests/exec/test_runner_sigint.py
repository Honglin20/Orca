"""test_runner_sigint.py —— CLIRunner.send_sigint + ClaudeExecutor SIGINT-as-interrupt（phase 11 §4.2）。

覆盖：
  - CLIRunner.send_sigint：proc 存活时发 SIGINT + 置 was_interrupted=True；
    未启动 / 已退出时幂等返回 False。
  - ClaudeExecutor：runner.was_interrupted=True → emit node_failed{was_interrupted:true}，
    **不** raise ExecError（当 interrupt 不当 spawn error）。
  - ClaudeExecutor：spawn 前 emit prompt_rendered（preview=末尾 ~200 字符）。
"""

from __future__ import annotations

import asyncio
import json
import signal
from typing import Any

import pytest

from orca.exec.claude.executor import ClaudeExecutor
from orca.exec.context import RunContext
from orca.exec.runner import CLIRunner, SpawnConfig


# ── CLIRunner.send_sigint ────────────────────────────────────────────────────


def _result_line(text: str = "ok", is_error: bool = False) -> str:
    return json.dumps({
        "type": "result", "result": text, "usage": {},
        "total_cost_usd": 0.0, "is_error": is_error,
    })


def test_send_sigint_when_not_started_returns_false():
    """未 stream() 过（_proc=None）→ send_sigint 幂等返回 False。"""
    runner = CLIRunner(SpawnConfig(cli_path="echo", flags=(), prompt="x", prompt_channel="stdin"))
    assert runner.send_sigint() is False
    assert runner.was_interrupted is False


def test_send_sigint_signals_live_subprocess_and_sets_flag():
    """proc 存活时 send_sigint 发 SIGINT + 置 was_interrupted=True。

    用一个长睡子进程（sleep 30）做 target，send_sigint 后子进程应被 SIGINT 唤醒退出。
    """

    async def scenario():
        # sleep 30 作为目标子进程；CLIRunner 会 readline 它的 stdout（sleep 不输出，故阻塞）。
        cfg = SpawnConfig(
            cli_path="sleep", flags=("30",), prompt="", prompt_channel="stdin",
        )
        runner = CLIRunner(cfg)
        stream_task = asyncio.create_task(_drain(runner))
        await asyncio.sleep(0.1)  # 让 stream() spawn 完 proc

        ok = runner.send_sigint()
        assert ok is True
        assert runner.was_interrupted is True

        # 等 stream 结束（SIGINT 让 sleep 退出，readline EOF）
        await asyncio.wait_for(stream_task, timeout=5.0)

    run_async(scenario())


async def _drain(runner: CLIRunner) -> None:
    async for _ in runner.stream():
        pass


def run_async(coro):
    return asyncio.run(coro)


def test_send_sigint_after_exit_returns_false():
    """proc 已退出（returncode 非 None）→ send_sigint 幂等返回 False，不置 flag。"""

    async def scenario():
        # echo 立即退出
        cfg = SpawnConfig(cli_path="echo", flags=("hi",), prompt="", prompt_channel="stdin")
        runner = CLIRunner(cfg)
        async for _ in runner.stream():
            pass
        # stream 结束后 _proc 已清空（_finalize 置 None）
        assert runner.send_sigint() is False
        assert runner.was_interrupted is False

    run_async(scenario())


# ── ClaudeExecutor SIGINT-as-interrupt ───────────────────────────────────────


class _SigintFakeRunner:
    """CLIRunner 替身：模拟被用户 SIGINT（was_interrupted=True + 非零退出）。"""

    def __init__(self, lines=None, *, was_interrupted: bool = False, exit_code: int = 0):
        self._lines = list(lines) if lines is not None else []
        self._on_result = None
        self.exit_code = exit_code
        self.timed_out = False
        self.elapsed = 0.1
        self.stderr = ""
        self.was_interrupted = was_interrupted

    async def stream(self):
        for line in self._lines:
            self._maybe_fire(line)
            yield line

    def _maybe_fire(self, line: str) -> None:
        if self._on_result is None:
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        if isinstance(obj, dict) and obj.get("type") == "result":
            self._on_result(
                obj.get("result", ""), obj.get("usage") or {},
                obj.get("total_cost_usd") or 0.0, bool(obj.get("is_error", False)),
            )


def test_claude_executor_sigint_is_interrupted_not_error(tmp_path, monkeypatch):
    """was_interrupted=True → node_failed{was_interrupted:true}，不 raise ExecError。

    SPEC §4.2：用户主动中断不是 transient error，executor 把它表达为 node_failed 让
    orchestrator 在 node 边界决定 continue/skip/abort。
    """
    from orca.exec.claude import executor as exec_mod
    from orca.profiles import get_profile
    from orca.schema import AgentNode

    # fake runner：被 SIGINT，无 result 行（中断前未写完）
    fake = _SigintFakeRunner(lines=[], was_interrupted=True, exit_code=-2)
    monkeypatch.setattr(exec_mod, "CLIRunner", lambda cfg, on_result=None: fake)
    monkeypatch.chdir(tmp_path)

    executor = ClaudeExecutor(profile=get_profile("claude"))
    node = AgentNode(name="a", prompt="do thing")
    ctx = RunContext(inputs={}, outputs={}, run_id="r1")

    async def scenario():
        events = [e async for e in executor.exec(node, ctx)]
        return events

    events = run_async(scenario())
    types = [e.type for e in events]
    # node_started → prompt_rendered → node_failed{was_interrupted}（不 raise，无 error 事件）
    assert "node_started" in types
    assert "prompt_rendered" in types
    failed = next(e for e in events if e.type == "node_failed")
    assert failed.data["was_interrupted"] is True
    assert failed.data["error_type"] == "Interrupted"
    # 关键：不 emit error 事件（不是 transient error）
    assert "error" not in types


def test_claude_executor_emits_prompt_rendered_before_spawn(tmp_path, monkeypatch):
    """spawn 前发 prompt_rendered，preview 是 prompt 末尾 ~200 字符（SPEC §2.2 B5）。"""
    from orca.exec.claude import executor as exec_mod
    from orca.profiles import get_profile
    from orca.schema import AgentNode

    long_prompt = "BASE " + "x" * 300  # > 200 字符
    fake = _SigintFakeRunner(lines=[_result_line("ok")], was_interrupted=False, exit_code=0)
    monkeypatch.setattr(exec_mod, "CLIRunner", lambda cfg, on_result=None: fake)
    monkeypatch.chdir(tmp_path)

    executor = ClaudeExecutor(profile=get_profile("claude"))
    node = AgentNode(name="a", prompt=long_prompt)
    ctx = RunContext(inputs={}, outputs={}, run_id="r1")

    async def scenario():
        return [e async for e in executor.exec(node, ctx)]

    events = run_async(scenario())
    pr = next(e for e in events if e.type == "prompt_rendered")
    preview = pr.data["preview"]
    assert len(preview) <= 200
    assert preview.endswith("x" * (200 - len("BASE ")))  # 末尾 200 字符
    assert pr.data["node"] == "a"
