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
    # ADR §4.5：retry_started.data 带 layer/kind/next_retry_at（E2E 闭环 Defect A/B）
    assert started.data["kind"] == "transport_process"  # spawn_error → TRANSPORT_PROCESS
    assert started.data["layer"] == "transport"         # 与 kind 一致（E2E 闭环 Defect B）
    assert started.data["reason"]
    # next_retry_at：delay > 0 时为 ISO 时间戳，delay = 0 时为 None（本测试 default delay=0）
    assert "next_retry_at" in started.data
    succeeded = next(e for e in tape.replay() if e.type == "retry_succeeded")
    assert succeeded.data["attempt_total"] == 2
    assert "retry_exhausted" not in types


def test_retry_started_next_retry_at_iso_when_delay_positive(tmp_path):
    """ADR §4.5 / E2E 闭环 Defect A：delay > 0 时 next_retry_at 必须是 ISO 时间戳。

    INTENT：用户能看到「下次重试在何时」（不缺字段）。orchestrator retry path 此前
    直接构造 dict 漏写 next_retry_at，E2E agent 实测发现。
    """
    bus, tape = _bus(tmp_path)
    # delay=0.1s > 0 → next_retry_at 必须是 ISO 字符串
    node = _retry_node(
        max_attempts=2, backoff="constant", initial_delay_seconds=0.1, jitter=False,
    )
    executor = _ScriptedExecutor([
        _fail("spawn_error", "fail"),
        _complete({"result": "ok"}),
    ])
    run_async(execute_with_retry(executor, node, _ctx(), bus))
    started = next(e for e in tape.replay() if e.type == "retry_started")
    assert started.data["next_retry_at"] is not None
    assert isinstance(started.data["next_retry_at"], str)
    assert "T" in started.data["next_retry_at"]  # ISO 8601 含 T


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
    monkeypatch.setattr(factory_mod, "make_executor", lambda node, agent_tools_server=None, bus=None, **kwargs: fake)

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
    monkeypatch.setattr(factory_mod, "make_executor", lambda node, agent_tools_server=None, bus=None, **kwargs: fake)

    from orca.run.orchestrator import Orchestrator
    orch = Orchestrator(wf, bus, inputs={}, run_id="r1")
    state = run_async(orch.run())

    assert state.status == "failed"
    types = _tape_types(tape)
    assert types.count("retry_started") == 2
    assert "retry_exhausted" in types
    assert "workflow_failed" in types


# ── 14. 单 Tape 契约不变量：无孤儿 retry_started ──────────────────────────────
#
# 每个 retry_started 必然有一个后继的终态事件（retry_succeeded 或 retry_exhausted）。
# 孤儿 retry_started = workflow 卡在「说要重试但永远没结论」—— 单 tape 唯一真相源契约
# （CLAUDE.md 底线）会被违反。本组在 tape 序列上做配对检查，覆盖成功/用尽两条路径。


def _assert_no_orphan_retry_started(tape: Tape) -> None:
    """断言 tape 上无孤儿 retry_started：最后一个 retry_* 事件必须是终态。

    retry 序列的契约是「N 个 retry_started 后必有恰好 1 个终态（retry_succeeded 或
    retry_exhausted）」——成功路径是 N started + 1 succeeded，用尽路径是 N started +
    1 exhausted。孤儿 = 序列以 retry_started 收尾（说要重试但永远没结论），违反单 tape
    唯一真相源（workflow 会卡住）。

    本断言检查：(a) 至少有 1 个终态事件；(b) 最后一个 retry_* 事件是终态（非 started）；
    (c) 最后一个终态在最后一个 started 之后。这三条共同保证 retry 序列闭环。
    """
    types = _tape_types(tape)
    retry_events = [(i, t) for i, t in enumerate(types) if t.startswith("retry_")]
    assert retry_events, "本断言仅用于有 retry 事件的 tape（调用方应先确认 retry 发生）"
    terminals = [(i, t) for i, t in retry_events if t != "retry_started"]
    assert terminals, (
        f"有 retry_started 但无任何终态（succeeded/exhausted）——孤儿，workflow 卡住。序列：{types}"
    )
    last_event_idx, last_event_type = retry_events[-1]
    assert last_event_type != "retry_started", (
        f"最后一个 retry_* 事件是 retry_started(idx={last_event_idx})，无终态收尾"
        f"——孤儿 started。序列：{types}"
    )
    last_started = max(i for i, t in retry_events if t == "retry_started")
    last_terminal = terminals[-1][0]  # terminals 末位 idx（retry_events 已按 idx 升序）
    assert last_started < last_terminal, (
        f"最后 retry_started(idx={last_started}) 在最后终态(idx={last_terminal}) 之后"
        f"——started 未闭环。序列：{types}"
    )


def test_retry_success_path_no_orphan_retry_started(tmp_path):
    """成功路径：retry_started 后必有 retry_succeeded，无孤儿（单 tape 契约不变量）。

    INTENT：transient 失败 → 重试 → 成功，tape 上每个 started 都配对 succeeded。
    防御未来重构（如把 retry_started 移到不同位置）打破「started 必有结论」契约。
    """
    bus, tape = _bus(tmp_path)
    node = _retry_node(max_attempts=3)
    executor = _ScriptedExecutor([
        _fail("spawn_error", "attempt 1"),
        _fail("spawn_error", "attempt 2"),
        _complete({"ok": True}),
    ])
    output, _ = run_async(execute_with_retry(executor, node, _ctx(), bus))
    assert output == {"ok": True}
    _assert_no_orphan_retry_started(tape)


def test_retry_exhausted_path_no_orphan_retry_started(tmp_path):
    """用尽路径：retry_started×N + retry_exhausted（1 个终态覆盖全部 started）。

    INTENT：max_attempts=3 → 2 个 retry_started（attempt 2、3 前）+ 1 retry_exhausted。
    配对契约：started 数 == succeeded+exhausted 数（这里 = 0 + 1 = 1）。
    **注意**：与成功路径不同，用尽路径是「多个 started 共享一个 exhausted 终态」——
    exhausted 标记整个 retry 序列结束，故配对检查用「终态总数」而非「逐 started 配对」。
    """
    bus, tape = _bus(tmp_path)
    node = _retry_node(max_attempts=3)
    executor = _ScriptedExecutor([
        _fail("spawn_error", "a1"),
        _fail("spawn_error", "a2"),
        _fail("spawn_error", "a3"),
    ])
    with pytest.raises(ExecError):
        run_async(execute_with_retry(executor, node, _ctx(), bus))
    # 用尽路径：2 started + 1 exhausted（exhausted 是整个序列的唯一终态）。
    types = _tape_types(tape)
    assert types.count("retry_started") == 2
    assert types.count("retry_exhausted") == 1
    # 最后一个事件是 retry_exhausted（终态写在最末）
    assert types[-1] == "retry_exhausted"


# ── 15. tape 顺序不变量：每 attempt 的 node_failed → retry_started → node_started ──


def test_retry_tape_ordering_node_failed_then_started_then_next_attempt(tmp_path):
    """tape 顺序：node_failed → retry_started → node_started（下一 attempt）严格有序。

    INTENT：用户/壳从 tape 重放看到的「失败 → 决定重试 → 开始下一轮」时序必须正确，
    反序（先 started 后 failed）会让 LogStream 渲染混乱 + 诊断误导。两个 attempt 各校验。
    """
    bus, tape = _bus(tmp_path)
    node = _retry_node(max_attempts=3)
    executor = _ScriptedExecutor([
        _fail("spawn_error", "a1"),
        _fail("spawn_error", "a2"),
        _complete("ok"),
    ])
    run_async(execute_with_retry(executor, node, _ctx(), bus))

    types = _tape_types(tape)
    # 两轮重试 → 两组 (node_failed, retry_started, node_started)
    # 第 1 轮：attempt1 的 node_failed(idx of last node_failed before first retry_started)
    # 找所有 retry_started 位置，校验其前是 node_failed、其后是 node_started
    started_positions = [i for i, t in enumerate(types) if t == "retry_started"]
    assert len(started_positions) == 2
    for sp in started_positions:
        assert types[sp - 1] == "node_failed", (
            f"retry_started(idx={sp}) 前应是 node_failed，实际 {types[sp-1]}。序列：{types}"
        )
        assert types[sp + 1] == "node_started", (
            f"retry_started(idx={sp}) 后应是 node_started（下一 attempt），"
            f"实际 {types[sp+1]}。序列：{types}"
        )


# ── 16. _classify_for_retry 直接单元覆盖（SPEC §9.5.2 error_type 对齐表全表）─────
#
# 现有测试经 execute_with_retry loop 间接验证映射；本组直接对 _classify_for_retry 单元化
# 全表覆盖，让对齐表的每一行都有可读的、独立的契约断言（loop 测试混在一起，单点失败难定位）。


def test_classify_for_retry_full_alignment_table():
    """_classify_for_retry 全表直接断言（SPEC §9.5.2 error_type 对齐表）。

    每行：executor 产出的 error_type → retry_on 白名单取值。
    ClaudeStreamError 细分：message 含限流关键词 → http_429，否则 api_error。
    """
    from orca.run.retry import _classify_for_retry

    # spawn 链
    assert _classify_for_retry("CliExitNonZero", {"message": "exit 1"}) == "spawn_error"
    # timeout 链
    assert _classify_for_retry("ExecTimeout", {"message": "60s"}) == "timeout"
    # stream 链：generic（无限流关键词）→ api_error
    assert _classify_for_retry(
        "ClaudeStreamError", {"message": "internal server error"}
    ) == "api_error"
    # stream 链：限流关键词命中 → http_429（每个关键词都验）
    for kw in ("rate_limit", "overloaded", "429", "api_retry", "529"):
        assert _classify_for_retry(
            "ClaudeStreamError", {"message": f"got {kw} from upstream"}
        ) == "http_429", f"ClaudeStreamError+{kw!r} 应归 http_429"
    # validator_failed / 自定义 → 原样透传（让 retry_on=[validator_failed] 直接命中）
    assert _classify_for_retry("validator_failed", {}) == "validator_failed"
    assert _classify_for_retry("SomeNewErrorType", {}) == "SomeNewErrorType"
    # 空串 → 空串（不在任何白名单，不重试）
    assert _classify_for_retry("", {}) == ""


def test_classify_for_retry_message_keyword_is_case_insensitive_and_substring():
    """限流关键词判定：大小写不敏感 + 子串匹配（message.lower() 后 in 判定）。

    INTENT：claude 流的 error message 大小写/格式不可控（如 "Rate_Limit" / "OVERLOADED"），
    _API_ERROR_KEYS 的子串匹配必须容忍。这是 http_429 细分可靠性的边界保障。
    """
    from orca.run.retry import _classify_for_retry

    # 大小写混合
    assert _classify_for_retry(
        "ClaudeStreamError", {"message": "Rate_Limit_Exceeded"}
    ) == "http_429"
    assert _classify_for_retry(
        "ClaudeStreamError", {"message": "OVERLOADED -- retry later"}
    ) == "http_429"
    # 子串（非整词）
    assert _classify_for_retry(
        "ClaudeStreamError", {"message": "error code 429 too many requests"}
    ) == "http_429"
    # message 缺失 / None → 不命中关键词 → api_error（不崩）
    assert _classify_for_retry("ClaudeStreamError", {}) == "api_error"
    assert _classify_for_retry("ClaudeStreamError", {"message": None}) == "api_error"


def test_classify_for_retry_api_error_keyword_subclassifies_when_text_contains_it():
    """边界：ClaudeStreamError 的 message 含字面 'api_error' → 归 http_429（非 api_error）。

    INTENT：``_API_ERROR_KEYS`` 包含 ``"api_error"`` 自身（见 retry.py:78），故一条
    message 含 'api_error' 的 ClaudeStreamError 会被细分为 http_429，而非 api_error。
    这是当前实现的真实语义（关键词列表驱动，非 error_type 字面值）——本测试记录此行为，
    防止未来误改关键词列表后悄悄改变 http_429/api_error 边界。如果实现方有意移除
    'api_error' 关键词，请同步更新本测试（不是 bug，是契约文档）。
    """
    from orca.run.retry import _API_ERROR_KEYS, _classify_for_retry

    # 契约文档：'api_error' 在关键词列表里
    assert "api_error" in _API_ERROR_KEYS
    # 故 message 含 'api_error' → http_429
    assert _classify_for_retry(
        "ClaudeStreamError", {"message": "upstream api_error"}
    ) == "http_429"


# ── 17. max_attempts=1 等价 retry=None（同失败面，回归保护）────────────────────


def test_retry_max_attempts_one_equivalent_to_no_retry_same_failure_surface(tmp_path):
    """max_attempts=1 与 retry=None 在「单次失败」路径上失败面一致（回归保护）。

    INTENT：SPEC §9.5.6「max_attempts=1 等价无 retry」。差异仅在 retry_exhausted 事件
    有无（policy 显式声明的可观测标记）；其余失败面（单次 node_failed、ExecError 抛出、
    error_type 透传）必须一致。若未来 max_attempts=1 退化为不发 exhausted 或多跑一次，
    本测试会失败。
    """
    # ── retry=None 路径（execute_and_emit）──────────────────────────────────
    bus_none, tape_none = _bus(tmp_path / "none")
    node_none = AgentNode(name="n", prompt="p", routes=[])  # retry=None
    executor_none = _ScriptedExecutor([_fail("spawn_error", "boom")])
    err_none: ExecError | None = None
    try:
        run_async(execute_and_emit(executor_none, node_none, _ctx(), bus_none))
    except ExecError as e:
        err_none = e
    assert err_none is not None
    types_none = _tape_types(tape_none)
    assert types_none.count("node_failed") == 1
    assert not any(t.startswith("retry_") for t in types_none)  # 无任何 retry_*

    # ── max_attempts=1 路径（execute_with_retry）──────────────────────────
    bus_one, tape_one = _bus(tmp_path / "one")
    node_one = _retry_node(max_attempts=1)
    executor_one = _ScriptedExecutor([_fail("spawn_error", "boom")])
    err_one: ExecError | None = None
    try:
        run_async(execute_with_retry(executor_one, node_one, _ctx(), bus_one))
    except ExecError as e:
        err_one = e
    assert err_one is not None

    # 失败面一致：单次 node_failed + 同 error_type + 同 message
    types_one = _tape_types(tape_one)
    assert types_one.count("node_failed") == 1, "max_attempts=1 应只跑一次（单 node_failed）"
    assert types_one.count("node_started") == 1
    assert err_one.error_type == err_none.error_type == "spawn_error"
    assert err_one.message == err_none.message == "boom"
    # 唯一差异：max_attempts=1 多一个 retry_exhausted（policy 显式声明的可观测标记）
    assert types_one.count("retry_exhausted") == 1
    # 关键回归保护：max_attempts=1 绝不 emit retry_started（没有第二次 attempt）
    assert types_one.count("retry_started") == 0
