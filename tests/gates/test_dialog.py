"""tests/gates/test_dialog.py —— DialogHandler 单元（phase 11 §6 / §6.2）。

覆盖（断言 INTENT 而非仅行为）：
  - ``start_dialog`` → emit ``dialog_started{node, session_id, initial_prompt}`` + 返回 dialog_id
  - ``send_turn`` → emit ``dialog_message(role=user)`` 再 ``dialog_message(role=agent)``，
    返回 agent reply；turn 计数递增
  - **历史累积**（SPEC §6.2 核心）：第 2 轮 send_turn 的 spawn prompt 含**第 1 轮的 user 文本
    AND 第 1 轮的 agent reply**（重 spawn + 拼全历史，无 in-process session）
  - ``end_dialog`` → emit ``dialog_ended{node, total_turns, conclusion}``
  - fail loud：未知 dialog_id 的 send_turn → raise（不静默丢一轮）
  - fail loud：spawn 失败（exit_code != 0）→ raise
  - SpawnConfig 用 profile.resolve_cli_path()（spy argv，不硬编码 "claude"）—— review C5

确定性：mock CLIRunner（不 spawn 真 claude），monkeypatch ``orca.gates.dialog.CLIRunner``。
FakeRunner 与 tests/exec/test_validator.py 同构（tests 非包故就地复制）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.exec.context import RunContext
from orca.gates.dialog import DialogHandler
from orca.profiles import get_profile


def run_async(coro):
    """统一异步入口（asyncio.run，本仓库约定）。"""
    return asyncio.run(coro)


# ── 共享 FakeRunner（与 tests/exec/test_validator.py 同构）──────────────────


class FakeRunner:
    """CLIRunner 替身：按预设行 yield，暴露 exit_code/elapsed/stderr。

    捕获每次构造时传入的 SpawnConfig（含 .prompt）→ 累积到 prompts 列表，
    供测试断言「历史是否拼进 prompt」（SPEC §6.2 history 累积测试的核心）。
    """

    def __init__(
        self,
        lines=None,
        *,
        exit_code: int = 0,
        timed_out: bool = False,
        elapsed: float = 0.1,
        stderr: str = "",
        raise_on_stream: BaseException | None = None,
    ) -> None:
        self._lines = list(lines) if lines is not None else []
        self._on_result = None
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.elapsed = elapsed
        self.stderr = stderr
        self.was_interrupted = False
        self.raise_on_stream = raise_on_stream
        # 每次 factory(cfg) 调用都记一份：测试可看「第 N 轮 spawn 的 prompt 是什么」。
        self.last_cfg: Any = None

    async def stream(self) -> AsyncIterator[str]:
        if self.raise_on_stream is not None:
            raise self.raise_on_stream
        for line in self._lines:
            self._maybe_fire_on_result(line)
            yield line

    def _maybe_fire_on_result(self, line: str) -> None:
        if self._on_result is None:
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(obj, dict) or obj.get("type") != "result":
            return
        self._on_result(
            obj.get("result", ""),
            obj.get("usage") or {},
            obj.get("total_cost_usd") or 0.0,
            bool(obj.get("is_error", False)),
        )


class FakeRunnerFactory:
    """每次 spawn 产一个新 FakeRunner，但所有 cfg.prompt 累积到 self.prompts。

    DialogHandler 每轮 send_turn 都新建一个 CLIRunner（重 spawn），故需「每调用一次 factory
    产一个 fresh FakeRunner + 记下这次 prompt」——而非单实例复用（单实例的 last_cfg 会被
    下一轮覆盖，无法断言历史累积）。
    """

    def __init__(self, reply_provider):
        # reply_provider: callable(turn_index_1based) -> str，决定每轮 agent 回什么。
        self._reply_provider = reply_provider
        self.prompts: list[str] = []  # 按调用序累积每轮 spawn 的 prompt
        self.runners: list[FakeRunner] = []

    def __call__(self, cfg, on_result=None):
        self.prompts.append(cfg.prompt)
        turn_idx = len(self.prompts)  # 1-based
        reply = self._reply_provider(turn_idx)
        runner = FakeRunner(lines=[_result_line(reply)])
        runner._on_result = on_result
        runner.last_cfg = cfg
        self.runners.append(runner)
        return runner


def _result_line(result_text: str, *, is_error: bool = False) -> str:
    """构造 claude stream-json 的 result 行（CLIRunner._maybe_fire_on_result 检测 type=result）。"""
    return json.dumps({
        "type": "result",
        "result": result_text,
        "is_error": is_error,
        "usage": {},
        "total_cost_usd": 0.0,
    })


# ── 共享 fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def bus(tmp_path):
    """真 EventBus + Tape（emit 写文件，测试可 replay 看 dialog_* 事件落盘）。"""
    tape = Tape(tmp_path / "dialog.jsonl", run_id="r1")
    return EventBus(tape)


@pytest.fixture
def profile():
    return get_profile("claude")


@pytest.fixture
def ctx():
    return RunContext(inputs={}, outputs={}, run_id="r1")


@pytest.fixture
def handler(profile, bus):
    return DialogHandler(profile, bus)


def _read_tape(tmp_path):
    """读 tape 文件全部行（replay dialog_* 事件落盘）。"""
    lines = (tmp_path / "dialog.jsonl").read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines if line]


# ── 1. start_dialog → emit dialog_started + 返回 dialog_id ──────────────────


def test_start_dialog_emits_dialog_started(handler, bus, ctx, tmp_path):
    """start_dialog → dialog_started{node, session_id, initial_prompt} 写 tape + 返回非空 id。"""
    output = {"model_class": "SimpleNet", "weights_path": "/abs/w.pth"}
    dialog_id = run_async(handler.start_dialog("generator", output, ctx))

    # dialog_id 非空（uuid hex）
    assert dialog_id and isinstance(dialog_id, str)

    # tape 含一条 dialog_started 事件
    events = _read_tape(tmp_path)
    started = [e for e in events if e["type"] == "dialog_started"]
    assert len(started) == 1
    payload = started[0]["data"]
    assert payload["node"] == "generator"
    assert payload["session_id"] == dialog_id  # dialog_id 即 session 标识
    # initial_prompt 是 agent_output 的 JSON 摘要（含 model_class）
    assert "SimpleNet" in payload["initial_prompt"]


# ── 2. send_turn → emit user then agent message + 返回 reply + turn 递增 ────


def test_send_turn_emits_user_then_agent_messages(
    monkeypatch, handler, bus, ctx, tmp_path,
):
    """send_turn → dialog_message(role=user) 先于 dialog_message(role=agent)，
    返回 agent reply 文本，turn 计数递增到 1。"""
    factory = FakeRunnerFactory(reply_provider=lambda turn: f"reply #{turn}")
    monkeypatch.setattr("orca.gates.dialog.CLIRunner", factory)

    dialog_id = run_async(handler.start_dialog("cfg", {"k": "v"}, ctx))
    reply = run_async(handler.send_turn(dialog_id, "为什么 dataset 是 NOT_FOUND？", ctx))

    # 返回的是 agent reply 文本
    assert reply == "reply #1"

    # tape 里 dialog_message 事件顺序：user 在前，agent 在后
    events = _read_tape(tmp_path)
    messages = [e for e in events if e["type"] == "dialog_message"]
    assert len(messages) == 2
    assert messages[0]["data"]["role"] == "user"
    assert messages[0]["data"]["text"] == "为什么 dataset 是 NOT_FOUND？"
    assert messages[0]["data"]["turn"] == 1
    assert messages[1]["data"]["role"] == "agent"
    assert messages[1]["data"]["text"] == "reply #1"
    assert messages[1]["data"]["turn"] == 1  # 同一轮 user/agent 共享 turn 号


# ── 3. send_turn 历史累积（SPEC §6.2 核心）──────────────────────────────────


def test_send_turn_accumulates_history(monkeypatch, handler, ctx):
    """第 2 轮 send_turn 的 spawn prompt 含**第 1 轮 user 文本 AND 第 1 轮 agent reply**。

    SPEC §6.2：每轮重 spawn claude + 拼全历史（无 in-process session）。这是 dialog 正确性的
    核心——若历史没拼进去，第 2 轮 claude 看不到第 1 轮问答，对话断裂。
    """
    factory = FakeRunnerFactory(reply_provider=lambda turn: f"answer {turn}")
    monkeypatch.setattr("orca.gates.dialog.CLIRunner", factory)

    dialog_id = run_async(handler.start_dialog("n", {"out": 1}, ctx))
    run_async(handler.send_turn(dialog_id, "第一个问题", ctx))
    run_async(handler.send_turn(dialog_id, "第二个问题", ctx))

    # 两次 spawn，两次 prompt
    assert len(factory.prompts) == 2
    first_prompt = factory.prompts[0]
    second_prompt = factory.prompts[1]

    # 第 1 轮 prompt：含 agent_output + 第 1 轮 user 文本 + 标记「首轮无历史」
    assert "第一个问题" in first_prompt
    assert "首轮" in first_prompt  # 历史占位「（首轮，尚无历史）」

    # 第 2 轮 prompt：含 agent_output + 第 1 轮 user + 第 1 轮 agent reply + 第 2 轮 user
    assert "第一个问题" in second_prompt  # 历史 user
    assert "answer 1" in second_prompt    # 历史 agent reply
    assert "第二个问题" in second_prompt  # 本轮 user


# ── 4. end_dialog → emit dialog_ended ──────────────────────────────────────


def test_end_dialog_emits_ended(monkeypatch, handler, bus, ctx, tmp_path):
    """end_dialog → dialog_ended{node, total_turns, conclusion=user_ended} 写 tape。"""
    factory = FakeRunnerFactory(reply_provider=lambda turn: f"r{turn}")
    monkeypatch.setattr("orca.gates.dialog.CLIRunner", factory)

    dialog_id = run_async(handler.start_dialog("agent_x", {"o": 1}, ctx))
    run_async(handler.send_turn(dialog_id, "q1", ctx))
    run_async(handler.send_turn(dialog_id, "q2", ctx))
    run_async(handler.end_dialog(dialog_id, ctx))

    events = _read_tape(tmp_path)
    ended = [e for e in events if e["type"] == "dialog_ended"]
    assert len(ended) == 1
    payload = ended[0]["data"]
    assert payload["node"] == "agent_x"
    assert payload["total_turns"] == 2  # 两轮 send_turn
    assert payload["conclusion"] == "user_ended"


def test_end_dialog_unknown_id_is_noop(handler, ctx):
    """end_dialog 未知 dialog_id → no-op（不 raise，幂等清理路径）。"""
    # 不应 raise（end 可能被调多次）
    run_async(handler.end_dialog("nonexistent-id", ctx))


# ── 5. fail loud：未知 dialog_id / spawn 失败 ───────────────────────────────


def test_send_turn_unknown_dialog_id_raises(handler, ctx):
    """未知 dialog_id 的 send_turn → raise KeyError（fail loud，属调用方 bug）。"""
    with pytest.raises(KeyError):
        run_async(handler.send_turn("nonexistent", "q", ctx))


def test_send_turn_spawn_failure_raises(monkeypatch, handler, ctx):
    """spawn 失败（exit_code != 0）→ raise RuntimeError（fail loud，不静默丢一轮）。"""
    def failing_factory(cfg, on_result=None):
        runner = FakeRunner(lines=[], exit_code=127, stderr="command not found: claude")
        runner._on_result = on_result
        runner.last_cfg = cfg
        return runner

    monkeypatch.setattr("orca.gates.dialog.CLIRunner", failing_factory)

    dialog_id = run_async(handler.start_dialog("n", {"o": 1}, ctx))
    with pytest.raises(RuntimeError, match="dialog claude spawn 失败"):
        run_async(handler.send_turn(dialog_id, "q", ctx))


# ── 6. SpawnConfig 用 profile.resolve_cli_path()（review C5）─────────────────


def test_dialog_spawn_config_uses_profile_cli_path(
    monkeypatch, handler, ctx,
):
    """spawn 的 SpawnConfig.cli_path 来自 profile.resolve_cli_path()，不硬编码 "claude"。

    与 validator 同 review C5：兼容 ccr 中转（``ORCA_CLAUDE_CLI=ccr code``）。
    """
    factory = FakeRunnerFactory(reply_provider=lambda turn: "x")
    monkeypatch.setattr("orca.gates.dialog.CLIRunner", factory)
    monkeypatch.setenv("ORCA_CLAUDE_CLI", "ccr code")  # env 覆盖

    dialog_id = run_async(handler.start_dialog("n", {"o": 1}, ctx))
    run_async(handler.send_turn(dialog_id, "q", ctx))

    assert len(factory.runners) == 1
    cfg = factory.runners[0].last_cfg
    # cli_path 应是 env 覆盖后的值（shlex 拆分前原始串），非硬编码 "claude"
    assert cfg.cli_path == "ccr code"


# ── 7. RunContext.with_dialog_turn 累积（D1 字段的契约验证）─────────────────


def test_with_dialog_turn_appends_and_returns_new_instance(ctx):
    """with_dialog_turn → 新实例，原 ctx 不变（frozen 语义），dialog_history 累积。"""
    new_ctx = ctx.with_dialog_turn("user", "你好", 1)
    assert len(ctx.dialog_history) == 0  # 原实例不变
    assert len(new_ctx.dialog_history) == 1
    entry = new_ctx.dialog_history[0]
    assert entry == {"role": "user", "text": "你好", "turn": 1}

    # 再追加一条 agent
    new_ctx2 = new_ctx.with_dialog_turn("agent", "你好，有什么帮您", 1)
    assert len(new_ctx2.dialog_history) == 2
    assert new_ctx2.dialog_history[1]["role"] == "agent"


def test_with_dialog_turn_empty_text_noop(ctx):
    """空 / 全空白 text 不追加（防 dialog_history 留空轮次）。"""
    assert ctx.with_dialog_turn("user", "", 1) is ctx  # 返回原实例
    assert ctx.with_dialog_turn("user", "   ", 1) is ctx
