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
            obj.get("api_error_status"),
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


# ── 8. 3 轮历史累积（防 off-by-one 漂移）────────────────────────────────────


def test_send_turn_three_turns_full_history_replayed(monkeypatch, handler, ctx):
    """3 轮 send_turn → 第 3 轮 spawn 的 prompt 含**全部**前 2 轮的 user + agent 文本，按序。

    INTENT（SPEC §6.2 核心契约的更强守护）：``test_send_turn_accumulates_history`` 只测 2 轮
    （第 2 轮 prompt 含第 1 轮）。但「累积」逻辑若是 off-by-one（如 ``history[:-1]`` 误切成
    ``history[1:]``、或循环上界写错），2 轮场景可能侥幸通过而 3 轮暴露。本测试 3 轮，断言：
      - 第 3 轮 prompt 含 turn-1 user + turn-1 agent + turn-2 user + turn-2 agent（全历史）；
      - 且按时间序出现（turn-1 内容在 turn-2 内容之前，防顺序错乱）。
    """
    factory = FakeRunnerFactory(reply_provider=lambda turn: f"reply {turn}")
    monkeypatch.setattr("orca.gates.dialog.CLIRunner", factory)

    dialog_id = run_async(handler.start_dialog("n", {"out": 1}, ctx))
    run_async(handler.send_turn(dialog_id, "问题一", ctx))
    run_async(handler.send_turn(dialog_id, "问题二", ctx))
    run_async(handler.send_turn(dialog_id, "问题三", ctx))

    assert len(factory.prompts) == 3
    third_prompt = factory.prompts[2]

    # 第 3 轮 prompt 必须含全部前两轮的 user + agent 文本
    assert "问题一" in third_prompt       # turn-1 user
    assert "reply 1" in third_prompt      # turn-1 agent
    assert "问题二" in third_prompt       # turn-2 user
    assert "reply 2" in third_prompt      # turn-2 agent
    assert "问题三" in third_prompt       # 本轮 user（turn-3）
    # 本轮 agent reply 还未产生（reply 3 是本轮的输出，不该在 prompt 里）
    assert "reply 3" not in third_prompt

    # 顺序守护：turn-1 内容出现在 turn-2 内容之前（防历史顺序错乱）
    assert third_prompt.index("问题一") < third_prompt.index("问题二"), (
        "历史应按时间序回放：turn-1 必须在 turn-2 之前"
    )


# ── 9. dialog 事件序列不变量（SPEC §6 事件契约）──────────────────────────────


def test_dialog_event_ordering_contract_on_tape(monkeypatch, handler, bus, ctx, tmp_path):
    """完整一轮 dialog 的 tape 事件序列满足 SPEC §6 不变量：
      - dialog_started 恰好 1 次，且在所有 dialog_message 之前；
      - dialog_message 严格 user/agent 交替，每组以 user 开头；
      - turn 号单调递增（1,1,2,2,...），同组 user/agent 共享 turn；
      - dialog_ended 恰好 1 次，在所有 message 之后。

    INTENT：单测覆盖了「单轮 send_turn 发 user+agent 两条」（test_send_turn_emits_user_then_agent_messages），
    但未守护**多轮**的事件序列不变量。若 turn 计数非单调、或 message 顺序错乱（如 agent 先于
    user）、或 dialog_started 漏发/重发，用户从 tape replay 看到的对话会断裂。本测试驱动
    2 轮 send_turn，断言完整序列契约。
    """
    factory = FakeRunnerFactory(reply_provider=lambda turn: f"a{turn}")
    monkeypatch.setattr("orca.gates.dialog.CLIRunner", factory)

    dialog_id = run_async(handler.start_dialog("cfg", {"k": "v"}, ctx))
    run_async(handler.send_turn(dialog_id, "q1", ctx))
    run_async(handler.send_turn(dialog_id, "q2", ctx))
    run_async(handler.end_dialog(dialog_id, ctx))

    events = _read_tape(tmp_path)
    types = [e["type"] for e in events]

    # dialog_started 恰 1 次，且是第一个 dialog_* 事件
    assert types.count("dialog_started") == 1
    first_dialog_idx = next(i for i, t in enumerate(types) if t.startswith("dialog_"))
    assert types[first_dialog_idx] == "dialog_started"

    # dialog_ended 恰 1 次，且是最后一个 dialog_* 事件
    assert types.count("dialog_ended") == 1
    last_dialog_idx = max(i for i, t in enumerate(types) if t.startswith("dialog_"))
    assert types[last_dialog_idx] == "dialog_ended"

    # message 序列：严格 user/agent 交替，user 开头，turn 单调
    messages = [e for e in events if e["type"] == "dialog_message"]
    assert len(messages) == 4  # 2 轮 × (user + agent)
    expected_roles = ["user", "agent", "user", "agent"]
    expected_turns = [1, 1, 2, 2]
    for i, (msg, exp_role, exp_turn) in enumerate(zip(messages, expected_roles, expected_turns)):
        assert msg["data"]["role"] == exp_role, (
            f"message[{i}] role 应为 {exp_role}，实际 {msg['data']['role']}"
        )
        assert msg["data"]["turn"] == exp_turn, (
            f"message[{i}] turn 应为 {exp_turn}，实际 {msg['data']['turn']}"
        )
    # turn 单调递增（按 user 消息的 turn 序列）
    user_turns = [m["data"]["turn"] for m in messages if m["data"]["role"] == "user"]
    assert user_turns == sorted(set(user_turns)), "turn 应单调递增"


# ── 10. end_dialog 无轮次（total_turns:0 干净退出）────────────────────────────


def test_end_dialog_without_any_turn_emits_zero_turns(monkeypatch, handler, bus, ctx, tmp_path):
    """start_dialog → 立即 end_dialog（无 send_turn）→ dialog_ended{total_turns:0}，无 phantom message。

    INTENT：用户打开 dialog 后立刻关掉（看了一眼 agent output 就结束）是合法路径。本测试守护：
      - 不崩（start/end 都是合法调用）；
      - dialog_ended.total_turns == 0（无 phantom turn 被记）；
      - tape 无任何 dialog_message 事件（没发过 turn）。
    若 end_dialog 误把「未 send 过」当「第 0 轮」记一条 phantom message，或 turn_count 初值错，
    此测试会失败。
    """
    factory = FakeRunnerFactory(reply_provider=lambda turn: "x")
    monkeypatch.setattr("orca.gates.dialog.CLIRunner", factory)

    dialog_id = run_async(handler.start_dialog("n", {"o": 1}, ctx))
    run_async(handler.end_dialog(dialog_id, ctx))

    events = _read_tape(tmp_path)
    ended = [e for e in events if e["type"] == "dialog_ended"]
    assert len(ended) == 1
    assert ended[0]["data"]["total_turns"] == 0
    # 无任何 dialog_message（没发过 turn）
    messages = [e for e in events if e["type"] == "dialog_message"]
    assert messages == [], f"无 send_turn 却出现 dialog_message：{messages}"


# ── 11. dialog_message 文本保真（含边界字符，无截断/转义破坏）─────────────────


def test_dialog_message_text_fidelity_with_edge_characters(monkeypatch, handler, bus, ctx, tmp_path):
    """agent reply 含特殊字符（引号 / 换行 / Unicode / 大括号）→ tape 上的 dialog_message.text
    与 send_turn 返回值**逐字一致**（无截断、无 JSON 转义破坏、无大括号被 prompt 模板吞）。

    INTENT：``_build_dialog_prompt`` 用 ``str.replace`` 注入历史文本，``{agent_output}`` /
    ``{history}`` / ``{user_text}`` 占位。若 reply 文本含 ``{`` ``}``，且注入顺序有 bug（如先
    replace user_text 再 replace agent_output，reply 里的 ``{agent_output}`` 会被二次替换），
    文本会被污染。本测试用含 ``{}`` / 引号 / 换行 / emoji 的 reply，断言 tape.text == 返回值。
    """
    tricky_reply = '含 "引号" 和 {大括号} 以及\n换行 + emoji 🎉 and {user_text}'
    factory = FakeRunnerFactory(reply_provider=lambda turn: tricky_reply)
    monkeypatch.setattr("orca.gates.dialog.CLIRunner", factory)

    dialog_id = run_async(handler.start_dialog("n", {"o": 1}, ctx))
    returned = run_async(handler.send_turn(dialog_id, "问一个边界问题", ctx))

    # 返回值与预设一致
    assert returned == tricky_reply
    # tape 上的 agent message.text 与返回值逐字一致（无截断/污染）
    events = _read_tape(tmp_path)
    agent_msgs = [
        e for e in events
        if e["type"] == "dialog_message" and e["data"]["role"] == "agent"
    ]
    assert len(agent_msgs) == 1
    assert agent_msgs[0]["data"]["text"] == tricky_reply, (
        "agent reply 在 tape 上被截断或污染（应与返回值逐字一致）"
    )
