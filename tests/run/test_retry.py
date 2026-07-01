"""tests/run/test_retry.py —— execute_with_retry + orchestrator retry 集成（phase 11 §9.5）。

覆盖（计划 P0.3 验收，逐条断言「意图」而非仅「行为」）：
  - retry=None → 单次失败立即 raise（不进 retry 路径）
  - 首次成功 → 无 retry_* 事件（不噪声）
  - 第二次成功 → retry_started + retry_succeeded 配对 + output 正确
  - 用尽 → retry_exhausted + re-raise 最后 ExecError
  - retry_on 白名单过滤（error_type 不在白名单 → 不重试，立即 raise）
  - backoff：constant / exponential（cap max_delay）/ linear
  - jitter：seeded random → delay ∈ [0.8x, 1.2x]
  - was_interrupted 短路（用户 SIGINT 不重试）
  - http_429 分类（error_type 对齐表）
  - orchestrator 集成（fake executor fail-then-succeed → tape 含 retry_* + workflow_completed）

确定性约定：delay 测试用 ``jitter=False``；jitter 边界测试 ``random.seed`` 固定。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.exec.context import RunContext
from orca.exec.error import ExecError
from orca.exec.interface import Executor
from orca.run.executor_adapter import execute_and_emit
from orca.run.retry import _compute_delay, execute_with_retry
from orca.schema import AgentNode, Event, RetryPolicy


def run_async(coro):
    """本仓库约定：异步测试统一 asyncio.run（无 pytest-asyncio）。"""
    return asyncio.run(coro)


# ── 共享 fixtures / helpers ───────────────────────────────────────────────────


def _bus(tmp_path: Path, run_id: str = "r1") -> tuple[EventBus, Tape]:
    tape = Tape(tmp_path / "events.jsonl", run_id=run_id)
    return EventBus(tape), tape


def _ev(
    type_: str, data: dict | None = None, *, node: str = "n", session_id: str = "s1"
) -> Event:
    """构造占位 Event（seq=0，bus.emit 内部重分配）。"""
    return Event(
        seq=0,
        type=type_,  # type: ignore[arg-type]
        timestamp=0.0,
        node=node,
        session_id=session_id,
        data=data or {},
    )


def _ctx() -> RunContext:
    return RunContext(inputs={}, outputs={}, run_id="r1")


class _ScriptedExecutor(Executor):
    """按调用次数消费预设事件序列的假 executor。

    每次 ``exec`` 取下一个预设序列（fail / complete），完整 yield 出来。让 retry loop
    的「第 N 次 attempt 走第 N 个序列」可确定断言。

    用法::

        exe = _ScriptedExecutor([
            _fail("spawn_error", "boom"),   # attempt 1 失败
            _complete({"out": "ok"}),        # attempt 2 成功
        ])
    """

    def __init__(self, scripts: list[list[Event]], *, node_name: str = "n"):
        self._scripts = list(scripts)
        self._idx = 0
        self._node_name = node_name

    async def exec(self, node, ctx: RunContext) -> AsyncIterator[Event]:  # type: ignore[override]
        if self._idx >= len(self._scripts):
            raise AssertionError(
                f"_ScriptedExecutor 被调第 {self._idx + 1} 次但脚本只剩 "
                f"{len(self._scripts)} 个序列（retry 配置错？）"
            )
        events = self._scripts[self._idx]
        self._idx += 1
        for e in events:
            yield e


def _fail(
    error_type: str = "spawn_error",
    message: str = "失败",
    phase: str = "spawn",
    *,
    node_name: str = "n",
    was_interrupted: bool = False,
) -> list[Event]:
    """构造 [node_started, node_failed] 序列（attempt 失败）。"""
    data: dict[str, Any] = {"error_type": error_type, "message": message, "phase": phase}
    if was_interrupted:
        data["was_interrupted"] = True
    return [
        _ev("node_started", {}, node=node_name),
        _ev("node_failed", data, node=node_name),
    ]


def _complete(output: Any = "ok", *, node_name: str = "n", kind: str = "agent") -> list[Event]:
    """构造 [node_started, node_completed] 序列（attempt 成功）。"""
    return [
        _ev("node_started", {"kind": kind}, node=node_name),
        _ev("node_completed", {"output": output, "elapsed": 0.0}, node=node_name),
    ]


def _tape_types(tape: Tape) -> list[str]:
    return [e.type for e in tape.replay()]


def _retry_node(
    *,
    retry_on: Iterable[str] = ("spawn_error",),
    max_attempts: int = 3,
    backoff: str = "constant",
    initial_delay_seconds: float = 0.0,  # 测试默认 0 让 sleep 不阻塞
    max_delay_seconds: float = 60.0,
    jitter: bool = False,
    name: str = "n",
    prompt: str = "p",
) -> AgentNode:
    """构造带 RetryPolicy 的 AgentNode（默认 delay=0 让测试不阻塞）。"""
    return AgentNode(
        name=name,
        prompt=prompt,
        routes=[],
        retry=RetryPolicy(
            max_attempts=max_attempts,
            backoff=backoff,  # type: ignore[arg-type]
            initial_delay_seconds=initial_delay_seconds,
            max_delay_seconds=max_delay_seconds,
            retry_on=list(retry_on),  # type: ignore[arg-type]
            jitter=jitter,
        ),
    )


# ── 1. retry=None → 不进 retry 路径（向后兼容契约） ─────────────────────────


def test_retry_no_policy_no_retry(tmp_path):
    """retry=None → execute_and_emit 既有路径：单次失败立即 raise，无 retry_* 事件。

    意图：向后兼容不变量。无 retry 声明的 node 行为与 wave-1 完全一致。
    """
    bus, tape = _bus(tmp_path)
    node = AgentNode(name="n", prompt="p", routes=[])  # retry=None（默认）
    executor = _ScriptedExecutor([_fail("spawn_error", "boom")])

    with pytest.raises(ExecError) as ei:
        run_async(execute_and_emit(executor, node, _ctx(), bus))

    assert ei.value.error_type == "spawn_error"
    types = _tape_types(tape)
    assert "retry_started" not in types
    assert "retry_succeeded" not in types
    assert "retry_exhausted" not in types


# ── 2. 首次成功 → 无 retry_* 事件 ───────────────────────────────────────────


def test_retry_success_on_first_attempt_no_retry_event(tmp_path):
    """policy 存在但首次就成功 → 不发 retry_*（避免噪声）。

    意图：retry_* 事件只在「确实发生了重试」时出现，正常路径不污染 Tape。
    """
    bus, tape = _bus(tmp_path)
    node = _retry_node()
    executor = _ScriptedExecutor([_complete({"result": "ok"})])

    output, events = run_async(execute_with_retry(executor, node, _ctx(), bus))

    assert output == {"result": "ok"}
    types = [e.type for e in events]
    assert "retry_started" not in types
    assert "retry_succeeded" not in types
    assert "retry_exhausted" not in types
    # Tape 也无 retry_*
    tape_types = _tape_types(tape)
    assert not any(t.startswith("retry_") for t in tape_types)


# ── 3. 第二次成功 → retry_started + retry_succeeded 配对 ────────────────────


def test_retry_success_on_second_attempt_emits_started_and_succeeded(tmp_path):
    """attempt1 spawn_error（在白名单）→ retry_started → attempt2 成功 → retry_succeeded。

    意图：transient 失败后重试成功，用户能看到「重试了 + 成功了」配对事件。
    """
    bus, tape = _bus(tmp_path)
    node = _retry_node(max_attempts=3)
    executor = _ScriptedExecutor([
        _fail("spawn_error", "first attempt failed"),
        _complete({"result": "ok on retry"}),
    ])

    output, _events = run_async(execute_with_retry(executor, node, _ctx(), bus))

    assert output == {"result": "ok on retry"}
    types = _tape_types(tape)
    # retry_started 在 retry_succeeded 之前（顺序断言）
    assert types.index("retry_started") < types.index("retry_succeeded")
    started = next(e for e in tape.replay() if e.type == "retry_started")
    assert started.data["attempt"] == 2
    assert started.data["max_attempts"] == 3
    assert started.data["error_type"] == "spawn_error"
    assert started.data["node"] == "n"
    succeeded = next(e for e in tape.replay() if e.type == "retry_succeeded")
    assert succeeded.data["attempt_total"] == 2
    assert "retry_exhausted" not in types


# ── 4. 用尽 → retry_exhausted + re-raise ─────────────────────────────────────


def test_retry_exhausted_emits_exhausted_and_raises(tmp_path):
    """3 次都 spawn_error（白名单内）→ retry_started×2 + retry_exhausted + raise。

    意图：transient 失败持续 → 用尽后 fail loud（re-raise 让上层 workflow_failed），
    retry_started 数 == max_attempts - 1（最后一次失败后直接 exhausted，不再 started）。
    """
    bus, tape = _bus(tmp_path)
    node = _retry_node(max_attempts=3)
    executor = _ScriptedExecutor([
        _fail("spawn_error", "attempt 1"),
        _fail("spawn_error", "attempt 2"),
        _fail("spawn_error", "attempt 3"),
    ])

    with pytest.raises(ExecError) as ei:
        run_async(execute_with_retry(executor, node, _ctx(), bus))

    assert ei.value.error_type == "spawn_error"
    assert "attempt 3" in ei.value.message  # re-raise 的是最后一次
    types = _tape_types(tape)
    assert types.count("retry_started") == 2  # attempt 2 和 attempt 3 前各一次
    assert "retry_exhausted" in types
    exhausted = next(e for e in tape.replay() if e.type == "retry_exhausted")
    assert exhausted.data["attempts"] == 3
    assert exhausted.data["last_error_type"] == "spawn_error"
    assert "retry_succeeded" not in types


# ── 5. retry_on 白名单过滤 ───────────────────────────────────────────────────


def test_retry_on_whitelist_filters_errors(tmp_path):
    """error_type=NoResultEvent（result_parse）不在 retry_on=[spawn_error] → 不重试。

    意图：配置错误 / schema 错误重试也是错，浪费 token。fail loud 不重试。
    """
    bus, tape = _bus(tmp_path)
    node = _retry_node(retry_on=("spawn_error",), max_attempts=3)
    executor = _ScriptedExecutor([_fail("NoResultEvent", "config error", phase="result_parse")])

    with pytest.raises(ExecError) as ei:
        run_async(execute_with_retry(executor, node, _ctx(), bus))

    assert ei.value.error_type == "NoResultEvent"
    types = _tape_types(tape)
    assert "retry_started" not in types
    assert "retry_exhausted" not in types  # 白名单外不进 retry 路径，不 emit exhausted


# ── 6. backoff: constant ─────────────────────────────────────────────────────


def test_retry_backoff_constant(tmp_path, monkeypatch):
    """jitter=False + constant → 每次 delay 都 == initial_delay_seconds。

    意图：constant 策略不随 attempt 增长。
    """
    bus, _ = _bus(tmp_path)
    # initial_delay_seconds 非零以观测 delay 值（sleep 被 fake 接管，不真等）。
    node = _retry_node(
        max_attempts=3, backoff="constant", initial_delay_seconds=0.5,
    )
    executor = _ScriptedExecutor([
        _fail("spawn_error"),
        _fail("spawn_error"),
        _complete("ok"),
    ])

    # monkeypatch asyncio.sleep 捕获 delay（不真 sleep，加速测试 + 拿到值）。
    delays: list[float] = []

    async def _fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    output, _ = run_async(execute_with_retry(executor, node, _ctx(), bus))

    assert output == "ok"
    # attempt 1, 2 各失败一次 → 2 个 delay（到 attempt 2 和 3）。
    assert len(delays) == 2
    assert all(abs(d - 0.5) < 1e-9 for d in delays)  # constant：都相等


# ── 7. backoff: exponential caps at max_delay ───────────────────────────────


def test_retry_backoff_exponential_caps_at_max_delay(tmp_path, monkeypatch):
    """jitter=False + exponential + 小 max_delay → delay 不超 max_delay_seconds。

    意图：exponential 会指数增长，max_delay_seconds 是硬上限防爆炸。
    """
    bus, _ = _bus(tmp_path)
    # initial=10, exp: 10, 20, 40(→cap 25), 80(→cap 25)... max_delay=25
    node = _retry_node(
        max_attempts=4, backoff="exponential",
        initial_delay_seconds=10.0, max_delay_seconds=25.0,
    )
    executor = _ScriptedExecutor([
        _fail("spawn_error"), _fail("spawn_error"),
        _fail("spawn_error"), _complete("ok"),
    ])
    delays: list[float] = []

    async def _fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    run_async(execute_with_retry(executor, node, _ctx(), bus))

    # attempt 1 (base 10) → attempt 2 (base 20) → attempt 3 (base 40, cap 25)
    assert len(delays) == 3
    assert delays[0] == 10.0
    assert delays[1] == 20.0
    assert delays[2] == 25.0  # capped


def test_retry_backoff_linear_in_loop(tmp_path, monkeypatch):
    """jitter=False + linear → delay = initial × attempt（loop 路径，与 constant/exp 对称覆盖）。

    意图：linear 在 loop 内的 delay 序列（非仅 _compute_delay 单元），证明 backoff
    策略在真实 retry loop 里被正确应用 —— attempt 1→2 等 initial×1，2→3 等 initial×2。
    """
    bus, _ = _bus(tmp_path)
    node = _retry_node(
        max_attempts=3, backoff="linear", initial_delay_seconds=2.0, max_delay_seconds=1000.0,
    )
    executor = _ScriptedExecutor([
        _fail("spawn_error"),
        _fail("spawn_error"),
        _complete("ok"),
    ])
    delays: list[float] = []

    async def _fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    run_async(execute_with_retry(executor, node, _ctx(), bus))

    # attempt 1 失败 → delay = 2×1 = 2；attempt 2 失败 → delay = 2×2 = 4
    assert delays == [2.0, 4.0]


def test_retry_backoff_linear_unit():
    """_compute_delay linear：initial × attempt（单元断言）。"""
    p = RetryPolicy(
        backoff="linear", initial_delay_seconds=2.0, max_delay_seconds=1000.0, jitter=False,
    )
    assert _compute_delay(p, 1) == 2.0
    assert _compute_delay(p, 2) == 4.0
    assert _compute_delay(p, 3) == 6.0


# ── 8. jitter ∈ [0.8x, 1.2x] ─────────────────────────────────────────────────


def test_retry_jitter_within_20_percent():
    """jitter=True + seeded → delay ∈ [0.8×base, 1.2×base]。"""
    random.seed(42)
    p = RetryPolicy(
        backoff="constant", initial_delay_seconds=10.0, max_delay_seconds=1000.0, jitter=True,
    )
    # 多次采样验证边界（不同 random 值都应落在 ±20%）。
    for _ in range(50):
        d = _compute_delay(p, 1)
        assert 8.0 <= d <= 12.0, f"jitter delay {d} 超出 [0.8x, 1.2x]=[8, 12]"


def test_retry_jitter_zero_base_stays_zero():
    """base=0 时 jitter 后仍是 0（边界：0 × anything = 0）。"""
    p = RetryPolicy(
        backoff="constant", initial_delay_seconds=0.0, max_delay_seconds=1000.0, jitter=True,
    )
    random.seed(1)
    assert _compute_delay(p, 1) == 0.0


# ── 9. was_interrupted 短路 ──────────────────────────────────────────────────


def test_retry_does_not_retry_interrupted(tmp_path):
    """node_failed{was_interrupted:True} → 不重试，立即 raise，无 retry_* 事件。

    意图：用户 Ctrl+G 主动中断不属于 transient error，优先于 retry_on 白名单短路。
    即使 spawn_error 在 retry_on 白名单内，interrupted 也不重试。
    """
    bus, tape = _bus(tmp_path)
    node = _retry_node(retry_on=("spawn_error",), max_attempts=3)
    executor = _ScriptedExecutor([
        _fail("spawn_error", "user sigint", phase="interrupted", was_interrupted=True),
    ])

    with pytest.raises(ExecError):
        run_async(execute_with_retry(executor, node, _ctx(), bus))

    types = _tape_types(tape)
    assert "retry_started" not in types
    assert "retry_exhausted" not in types
    assert "retry_succeeded" not in types


def test_retry_interrupted_defensive_missing_field(tmp_path):
    """node_failed 不含 was_interrupted 字段 → 不崩（.get 默认 False）→ 正常 retry 路径。

    意图：retry loop 读取 was_interrupted 必须防御性（hard constraint），缺字段
    不应 KeyError 崩 retry 逻辑，回退到正常 retry_on 判定。
    """
    bus, _ = _bus(tmp_path)
    node = _retry_node(max_attempts=2)
    # node_failed data 不含 was_interrupted key（模拟旧 executor / 边界）
    no_interrupt_field = [
        _ev("node_started", {}, node="n"),
        _ev("node_failed", {"error_type": "spawn_error", "message": "m", "phase": "spawn"}, node="n"),
        # 第二次成功
    ]
    executor = _ScriptedExecutor([no_interrupt_field, _complete("recovered")])

    output, _ = run_async(execute_with_retry(executor, node, _ctx(), bus))
    # 缺字段 → 默认 False → 进入 retry_on 判定 → spawn_error 在白名单 → 重试 → 成功
    assert output == "recovered"


# ── 10. http_429 分类（error_type 对齐表） ──────────────────────────────────


def test_retry_http_429_classification(tmp_path):
    """error_type=http_429 + retry_on=[http_429] → 重试。

    意图：记录 SPEC §9.5.2 error_type 对齐表 —— http_429 是独立的 retry_on 取值，
    与 api_error 区分（限流 vs 通用 API 错误），白名单精确匹配。
    """
    bus, tape = _bus(tmp_path)
    node = _retry_node(retry_on=("http_429",), max_attempts=2)
    executor = _ScriptedExecutor([
        _fail("http_429", "rate limited", phase="stream"),
        _complete({"after_backoff": True}),
    ])

    output, _ = run_async(execute_with_retry(executor, node, _ctx(), bus))

    assert output == {"after_backoff": True}
    types = _tape_types(tape)
    assert "retry_started" in types
    assert "retry_succeeded" in types
    started = next(e for e in tape.replay() if e.type == "retry_started")
    assert started.data["error_type"] == "http_429"


def test_retry_api_error_not_matched_when_only_429_in_whitelist(tmp_path):
    """error_type=api_error 但 retry_on=[http_429] → 不重试（白名单精确，不模糊匹配）。

    意图：error_type 对齐表的另一面 —— api_error 与 http_429 是不同取值，不能
    因为「都是 API 错」就互相覆盖。用户想重试限流但 fail-fast 通用 API 错时
    必须能精确控制。
    """
    bus, _ = _bus(tmp_path)
    node = _retry_node(retry_on=("http_429",), max_attempts=3)
    executor = _ScriptedExecutor([_fail("api_error", "generic api err", phase="stream")])

    with pytest.raises(ExecError) as ei:
        run_async(execute_with_retry(executor, node, _ctx(), bus))
    assert ei.value.error_type == "api_error"


# ── 10b. error_type 对齐表：真实 ClaudeExecutor error_type 命中 retry_on ─────
#
# ClaudeExecutor 实际产出 error_type=CliExitNonZero/ExecTimeout/ClaudeStreamError
# （via phase_to_error_type），而 retry_on 白名单用语义短名（spawn_error/timeout/...）。
# _classify_for_retry 是桥接层（SPEC §9.5.2 对齐表），让用户写 retry_on:[spawn_error]
# 真能命中 error_type=CliExitNonZero。这组测试证明对齐层正确。


def test_retry_classifies_cli_exit_nonzero_as_spawn_error(tmp_path):
    """ClaudeExecutor phase=spawn → error_type=CliExitNonZero → retry_key=spawn_error。

    意图：真实 executor 产出 CliExitNonZero（非 spawn_error），retry_on=[spawn_error]
    经 _classify_for_retry 桥接后命中重试。这是「retry 实际可用」的核心契约。
    """
    bus, tape = _bus(tmp_path)
    node = _retry_node(retry_on=("spawn_error",), max_attempts=2)
    executor = _ScriptedExecutor([
        _fail("CliExitNonZero", "claude exit 1", phase="spawn"),
        _complete("recovered"),
    ])

    output, _ = run_async(execute_with_retry(executor, node, _ctx(), bus))
    assert output == "recovered"
    assert "retry_started" in _tape_types(tape)
    # retry_started 事件保留原始 error_type（诊断价值），retry_key 只用于白名单匹配
    started = next(e for e in tape.replay() if e.type == "retry_started")
    assert started.data["error_type"] == "CliExitNonZero"


def test_retry_classifies_exec_timeout_as_timeout(tmp_path):
    """ClaudeExecutor phase=timeout → error_type=ExecTimeout → retry_key=timeout。"""
    bus, tape = _bus(tmp_path)
    node = _retry_node(retry_on=("timeout",), max_attempts=2)
    executor = _ScriptedExecutor([
        _fail("ExecTimeout", "timed out", phase="timeout"),
        _complete("ok"),
    ])

    run_async(execute_with_retry(executor, node, _ctx(), bus))
    assert "retry_succeeded" in _tape_types(tape)


def test_retry_classifies_stream_rate_limit_as_http_429(tmp_path):
    """ClaudeStreamError + message 含 'rate_limit' → retry_key=http_429（非 api_error）。

    意图：SPEC §9.5.2 对齐表 —— 限流细分到 http_429，让 retry_on=[http_429] 只重试限流、
    retry_on=[api_error] 只重试通用 API 错。message 关键词触发细分。
    """
    bus, tape = _bus(tmp_path)
    node = _retry_node(retry_on=("http_429",), max_attempts=2)
    executor = _ScriptedExecutor([
        _fail("ClaudeStreamError", "Error: rate_limit exceeded", phase="stream"),
        _complete("after backoff"),
    ])

    output, _ = run_async(execute_with_retry(executor, node, _ctx(), bus))
    assert output == "after backoff"
    assert "retry_started" in _tape_types(tape)


def test_retry_classifies_stream_generic_as_api_error(tmp_path):
    """ClaudeStreamError + message 无限流关键词 → retry_key=api_error（非 http_429）。

    意图：retry_on=[api_error] 命中通用 API 错，但不命中限流（限流走 http_429）。
    """
    bus, tape = _bus(tmp_path)
    node = _retry_node(retry_on=("api_error",), max_attempts=2)
    executor = _ScriptedExecutor([
        _fail("ClaudeStreamError", "internal server error", phase="stream"),
        _complete("ok"),
    ])

    run_async(execute_with_retry(executor, node, _ctx(), bus))
    assert "retry_succeeded" in _tape_types(tape)


def test_retry_classifies_no_result_event_not_in_whitelist(tmp_path):
    """ClaudeExecutor phase=result_parse → error_type=NoResultEvent → 不重试（配置错）。

    意图：result_parse 是配置错（exit 0 但无 result 事件），重试也是错。NoResultEvent
    不在 _classify_for_retry 映射表 → 原样透传 → 不在任何 retry_on 白名单 → fail loud。
    """
    bus, _ = _bus(tmp_path)
    node = _retry_node(
        retry_on=("spawn_error", "timeout", "api_error", "http_429"), max_attempts=3,
    )
    executor = _ScriptedExecutor([_fail("NoResultEvent", "no result", phase="result_parse")])

    with pytest.raises(ExecError) as ei:
        run_async(execute_with_retry(executor, node, _ctx(), bus))
    assert ei.value.error_type == "NoResultEvent"


# ── 11. max_attempts=1 → 等价无 retry ────────────────────────────────────────


def test_retry_max_attempts_one_no_retry_loops(tmp_path):
    """max_attempts=1 → 单次失败直接 exhausted（无 retry_started，attempt 额度为 0）。

    意图：max_attempts=1 是合法边界 —— 行为等价「不重试」但仍走 retry 路径
    （emit retry_exhausted 保留可观测性，调用方明确声明了 policy）。
    """
    bus, tape = _bus(tmp_path)
    node = _retry_node(max_attempts=1)
    executor = _ScriptedExecutor([_fail("spawn_error", "only attempt")])

    with pytest.raises(ExecError):
        run_async(execute_with_retry(executor, node, _ctx(), bus))

    types = _tape_types(tape)
    assert "retry_started" not in types  # 没有第二次 attempt，不发 started
    assert "retry_exhausted" in types  # 用尽（1 次也算用尽）


# ── 12. RetryPolicy schema 校验（fail loud 在加载期） ──────────────────────


def test_retry_policy_rejects_max_attempts_below_one():
    """max_attempts < 1 → ValidationError（schema 层 fail loud）。

    意图：max_attempts=0 会让 retry loop range(1,1) 空跑撞「不可达」分支，错误信息
    误导。schema 层 ge=1 让配置错在 workflow 加载期暴露，而非运行期撞不变量。
    """
    with pytest.raises(ValidationError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValidationError):
        RetryPolicy(max_attempts=-1)


def test_retry_policy_rejects_negative_delay():
    """initial_delay_seconds / max_delay_seconds < 0 → ValidationError。

    意图：负 delay 会产生负 sleep（asyncio.sleep 立即返回，但语义错且掩盖配置错）。
    """
    with pytest.raises(ValidationError):
        RetryPolicy(initial_delay_seconds=-0.1)
    with pytest.raises(ValidationError):
        RetryPolicy(max_delay_seconds=-1.0)


def test_retry_policy_accepts_zero_delay():
    """delay=0 合法（测试场景 / 即时重试）—— ge=0 而非 gt=0。"""
    p = RetryPolicy(initial_delay_seconds=0.0, max_delay_seconds=0.0)
    assert p.initial_delay_seconds == 0.0
    assert _compute_delay(p, 1) == 0.0


# ── 13. 生命周期违约 → fail loud ────────────────────────────────────────────


def test_retry_lifecycle_violation_raises(tmp_path):
    """executor 既不 completed 也不 failed → raise（生命周期违约，fail loud）。

    意图：retry loop 不静默吞 executor 违约 —— 复用 execute_and_emit 的 fail loud 语义。
    """
    bus, _ = _bus(tmp_path)
    node = _retry_node(max_attempts=2)
    executor = _ScriptedExecutor([
        [_ev("node_started", {}, node="n")],  # 只有 started，无 terminal
    ])

    with pytest.raises(ExecError, match="生命周期违约"):
        run_async(execute_with_retry(executor, node, _ctx(), bus))


# ── 13. 集成：orchestrator + retry → tape 含 retry_* + workflow_completed ────


def _retry_wf_yaml(tmp_path) -> str:
    """单 agent node + retry 的最小 workflow YAML。"""
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump({
        "name": "retry_demo",
        "entry": "fetch",
        "nodes": [
            {
                "name": "fetch", "kind": "agent", "prompt": "fetch data",
                "routes": [{"to": "$end"}],
                "retry": {
                    "max_attempts": 3,
                    "backoff": "constant",
                    "initial_delay_seconds": 0.0,
                    "retry_on": ["spawn_error"],
                    "jitter": False,
                },
            },
        ],
        "outputs": {"result": "{{ fetch.output }}"},
    }), encoding="utf-8")
    return str(p)


def test_orchestrator_retry_integration_fail_then_succeed(tmp_path, monkeypatch):
    """orchestrator + retry：fake executor fail-then-succeed → workflow_completed + retry_*。

    意图：端到端契约 —— retry 事件流经 orchestrator 写 Tape，workflow 最终成功，
    outputs 含最终成功 attempt 的 output。monkeypatch make_executor 注入 fake。
    """
    from orca.compile import load_workflow
    from orca.exec import factory as factory_mod

    wf = load_workflow(_retry_wf_yaml(tmp_path))
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)

    # executor 产出的事件 node 字段必须 = 真实 node 名 "fetch"（reducer 据 node 投影 context）。
    fake = _ScriptedExecutor([
        _fail("spawn_error", "transient", node_name="fetch"),
        _complete({"data": "recovered"}, node_name="fetch"),
    ])
    # orchestrator._dispatch 内 `from orca.exec.factory import make_executor` 每次调用
    # 重绑模块属性，故 patch factory 模块级符号即可生效。
    monkeypatch.setattr(factory_mod, "make_executor", lambda node, agent_tools_server=None: fake)

    from orca.run.orchestrator import Orchestrator
    orch = Orchestrator(wf, bus, inputs={}, run_id="r1")
    state = run_async(orch.run())

    assert state.status == "completed"
    types = _tape_types(tape)
    assert "retry_started" in types
    assert "retry_succeeded" in types
    assert "workflow_completed" in types
    # 最终成功 attempt 的 output 进 state.context（reducer 据 node_completed 投影）
    assert state.context["fetch"] == {"data": "recovered"}


def test_orchestrator_retry_exhausted_workflow_failed(tmp_path, monkeypatch):
    """orchestrator + retry 用尽 → workflow_failed（error_type=透传 spawn_error）。"""
    from orca.compile import load_workflow
    from orca.exec import factory as factory_mod

    wf = load_workflow(_retry_wf_yaml(tmp_path))
    tape = Tape(tmp_path / "events.jsonl", run_id="r1")
    bus = EventBus(tape)

    fake = _ScriptedExecutor([
        _fail("spawn_error", "a1", node_name="fetch"),
        _fail("spawn_error", "a2", node_name="fetch"),
        _fail("spawn_error", "a3", node_name="fetch"),
    ])
    monkeypatch.setattr(factory_mod, "make_executor", lambda node, agent_tools_server=None: fake)

    from orca.run.orchestrator import Orchestrator
    orch = Orchestrator(wf, bus, inputs={}, run_id="r1")
    state = run_async(orch.run())

    assert state.status == "failed"
    types = _tape_types(tape)
    assert types.count("retry_started") == 2
    assert "retry_exhausted" in types
    assert "workflow_failed" in types
