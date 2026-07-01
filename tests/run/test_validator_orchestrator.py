"""tests/run/test_validator_orchestrator.py —— Orchestrator + validator loop（phase 11 §9.6.5 / §11.6）。

覆盖 orchestrator 的 ``_dispatch_with_validator`` 循环（断言 INTENT）：
  - validator=None → 不校验（向后兼容，validate_output 不被调）
  - 首次校验通过 → validator_started + validator_passed + output 返回
  - 失败后重试通过 → validator_failed(retrying=True) → validator_started → validator_passed
  - 用尽 → validator_failed(retrying=False) + ExecError(phase="validator")
  - validator 与 retry 预算独立（transient 失败由 execute_with_retry 重试，validator 失败由
    validator loop 重试，两者不共享计数 —— SPEC §11.6 deviation）

确定性：fake executor（_ScriptedExecutor）+ monkeypatch ``validate_output``（不 spawn 真 claude）。
monkeypatch make_executor 注入 fake executor（与 tests/run/test_retry.py 同 seam）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import pytest
import yaml

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.exec.context import RunContext
from orca.exec.error import ExecError
from orca.exec.interface import Executor
from orca.run.executor_adapter import execute_and_emit
from orca.schema import AgentNode, Event, RetryPolicy, ValidatorConfig


def run_async(coro):
    return asyncio.run(coro)


# ── 共享 helpers（与 tests/run/test_retry.py 同构，tests 非包故就地复制）──────────


def _ev(
    type_: str, data: dict | None = None, *, node: str = "agent", session_id: str = "s1"
) -> Event:
    return Event(
        seq=0,
        type=type_,  # type: ignore[arg-type]
        timestamp=0.0,
        node=node,
        session_id=session_id,
        data=data or {},
    )


def _complete(output: Any = "ok", *, node_name: str = "agent") -> list[Event]:
    """[node_started, node_completed] 序列（成功）。"""
    return [
        _ev("node_started", {"kind": "agent"}, node=node_name),
        _ev("node_completed", {"output": output, "elapsed": 0.0}, node=node_name),
    ]


def _fail(
    error_type: str = "spawn_error", message: str = "fail", phase: str = "spawn",
    *, node_name: str = "agent",
) -> list[Event]:
    """[node_started, node_failed] 序列（失败）。"""
    return [
        _ev("node_started", {}, node=node_name),
        _ev("node_failed", {"error_type": error_type, "message": message, "phase": phase}, node=node_name),
    ]


class _ScriptedExecutor(Executor):
    """按调用次数消费预设事件序列（每次 exec 取下一个序列）。"""

    def __init__(self, scripts: list[list[Event]], *, node_name: str = "agent"):
        self._scripts = list(scripts)
        self._idx = 0
        self._node_name = node_name
        # spy：记录每次 exec 收到的 ctx.user_guidance（让测试断言 guidance 累积）
        self.ctx_guidance_history: list[tuple[str, ...]] = []

    async def exec(self, node, ctx: RunContext) -> AsyncIterator[Event]:  # type: ignore[override]
        self.ctx_guidance_history.append(ctx.user_guidance)
        if self._idx >= len(self._scripts):
            raise AssertionError(
                f"_ScriptedExecutor 被调第 {self._idx + 1} 次但脚本只剩 "
                f"{len(self._scripts)} 个序列"
            )
        events = self._scripts[self._idx]
        self._idx += 1
        for e in events:
            yield e


def _tape_types(tape: Tape) -> list[str]:
    return [e.type for e in tape.replay()]


def _tape_payloads(tape: Tape, type_: str) -> list[dict]:
    return [e.data for e in tape.replay() if e.type == type_]


def _validator_wf_yaml(
    tmp_path: Path,
    *,
    retry: bool = False,
    criteria: str | None = None,
    max_retries: int = 1,
) -> str:
    """单 agent node workflow（route 到 $end；可选 retry + validator）。

    与 tests/run/test_retry.py::_retry_wf_yaml 同构：route 必须到 $end，否则 NoRouteMatch。
    validator 字段可选（criteria=None 时不加 → 测试 no-config 路径）。
    """
    p = tmp_path / "wf.yaml"
    node: dict[str, Any] = {
        "name": "agent", "kind": "agent", "prompt": "产出 JSON",
        "routes": [{"to": "$end"}],
    }
    if retry:
        node["retry"] = {
            "max_attempts": 3, "backoff": "constant",
            "initial_delay_seconds": 0.0, "retry_on": ["spawn_error"], "jitter": False,
        }
    if criteria is not None:
        node["validator"] = {"criteria": criteria, "max_retries": max_retries}
    p.write_text(yaml.safe_dump({
        "name": "vwf", "entry": "agent",
        "nodes": [node],
        "outputs": {"result": "{{ agent.output }}"},
    }), encoding="utf-8")
    return str(p)


def _load_wf_with_validator(
    tmp_path: Path, *, criteria: str = "校验标准", max_retries: int = 1, retry: bool = False,
) -> Any:
    """加载单 agent node workflow，node 上带 validator 字段（compile 层解析 ValidatorConfig）。"""
    from orca.compile import load_workflow

    return load_workflow(_validator_wf_yaml(
        tmp_path, retry=retry, criteria=criteria, max_retries=max_retries,
    ))


def _patch_validate(monkeypatch, verdicts: list[tuple[bool, list[str]]]):
    """把 ``orca.exec.validator.validate_output`` 替换成按序返回 verdicts 的 fake。

    verdicts: [(passed, issues), ...] —— 第 N 次调用返回第 N 个 verdict。
    返回 fake 对象，含 .call_count 供断言。
    """
    from orca.exec import validator as validator_mod

    state = {"idx": 0}

    async def fake_validate(output, config, profile, *, model=None):
        if state["idx"] >= len(verdicts):
            raise AssertionError(
                f"fake_validate 被调第 {state['idx'] + 1} 次但 verdicts 只剩 {len(verdicts)} 个"
            )
        verdict = verdicts[state["idx"]]
        state["idx"] += 1
        return verdict

    monkeypatch.setattr(validator_mod, "validate_output", fake_validate)
    fake_validate.call_count = lambda: state["idx"]  # type: ignore[attr-defined]
    return fake_validate


def _patch_make_executor(monkeypatch, fake: Executor):
    """把 orchestrator._dispatch 内的 make_executor 替换成返回 fake。"""
    from orca.exec import factory as factory_mod

    monkeypatch.setattr(
        factory_mod, "make_executor",
        lambda node, agent_tools_server=None, bus=None: fake,
    )


# ── 1. validator=None → 不校验（向后兼容契约）──────────────────────────────────


def test_validator_no_config_no_validation(tmp_path, monkeypatch):
    """validator=None → validate_output 不被调，走既有 execute 路径。

    INTENT：向后兼容不变量。无 validator 声明的 node 行为与 wave-2 完全一致。
    """
    from orca.run.orchestrator import Orchestrator

    # workflow 不带 validator（criteria=None）
    from orca.compile import load_workflow
    wf = load_workflow(_validator_wf_yaml(tmp_path, criteria=None))

    tape = Tape(tmp_path / "e.jsonl", run_id="r1")
    bus = EventBus(tape)
    fake = _ScriptedExecutor([_complete({"ok": True}, node_name="agent")])
    _patch_make_executor(monkeypatch, fake)

    # 即便 validate_output 被 patch 了，validator=None 时也不应被调
    patched = _patch_validate(monkeypatch, [(True, [])])  # 提供一个 verdict 但不该被消费

    orch = Orchestrator(wf, bus, inputs={}, run_id="r1")
    state = run_async(orch.run())

    assert state.status == "completed"
    assert patched.call_count() == 0, "validator=None 时 validate_output 不应被调"
    types = _tape_types(tape)
    assert not any(t.startswith("validator_") for t in types), "validator=None 时无 validator_* 事件"


# ── 2. 首次校验通过 → validator_started + validator_passed + output 返回 ────────


def test_validator_passed_first_try(tmp_path, monkeypatch):
    """agent output 首次校验通过 → emit validator_started + validator_passed，无 retry。

    INTENT：happy path —— 用户看到「校验开始 → 通过」，agent output 直接被接受。
    """
    from orca.run.orchestrator import Orchestrator

    wf = _load_wf_with_validator(tmp_path)
    tape = Tape(tmp_path / "e.jsonl", run_id="r1")
    bus = EventBus(tape)
    fake = _ScriptedExecutor([_complete({"model_class": "SimpleNet"}, node_name="agent")])
    _patch_make_executor(monkeypatch, fake)
    _patch_validate(monkeypatch, [(True, [])])

    orch = Orchestrator(wf, bus, inputs={}, run_id="r1")
    state = run_async(orch.run())

    assert state.status == "completed"
    types = _tape_types(tape)
    assert types.count("validator_started") == 1
    assert types.count("validator_passed") == 1
    assert "validator_failed" not in types
    # output 进 state.context（reducer 据 node_completed 投影）
    assert state.context["agent"] == {"model_class": "SimpleNet"}
    # 顺序：validator_started 在 validator_passed 前
    assert types.index("validator_started") < types.index("validator_passed")
    # executor 只被调一次（无重试）
    assert fake._idx == 1


# ── 3. 失败后重试通过 → validator_failed(retrying=True) → validator_started → validator_passed ─


def test_validator_failed_then_passed_with_retry(tmp_path, monkeypatch):
    """第 1 次 output 校验失败，第 2 次通过 → validator_failed(retrying=True) + 重跑 + passed。

    INTENT：validator 的核心价值 —— 失败后把 issues 作 guidance 反馈给 agent 重跑，直到通过
    或用尽。本测试 max_retries=1（总尝试 2 次）：第 1 次失败 + 第 2 次成功。
    """
    from orca.run.orchestrator import Orchestrator

    wf = _load_wf_with_validator(tmp_path, max_retries=1)
    tape = Tape(tmp_path / "e.jsonl", run_id="r1")
    bus = EventBus(tape)
    # 两次 exec 产出不同 output（第 1 次不合规，第 2 次合规）
    fake = _ScriptedExecutor([
        _complete({"model_class": "123abc"}, node_name="agent"),  # 不合规
        _complete({"model_class": "SimpleNet"}, node_name="agent"),  # 合规
    ])
    _patch_make_executor(monkeypatch, fake)
    _patch_validate(monkeypatch, [
        (False, ["model_class 是 123abc 非法标识符"]),  # 第 1 次校验失败
        (True, []),  # 第 2 次校验通过
    ])

    orch = Orchestrator(wf, bus, inputs={}, run_id="r1")
    state = run_async(orch.run())

    assert state.status == "completed"
    types = _tape_types(tape)
    # 2 次校验 → 2 个 validator_started + 1 个 validator_failed(retrying=True) + 1 个 validator_passed
    assert types.count("validator_started") == 2
    assert types.count("validator_failed") == 1
    assert types.count("validator_passed") == 1
    # validator_failed 的 retrying=True（还有预算）
    failed_payloads = _tape_payloads(tape, "validator_failed")
    assert failed_payloads[0]["retrying"] is True
    assert failed_payloads[0]["issues"] == ["model_class 是 123abc 非法标识符"]
    # 最终 output 是第 2 次（合规的）
    assert state.context["agent"] == {"model_class": "SimpleNet"}


def test_validator_failed_issues_accumulate_as_guidance(tmp_path, monkeypatch):
    """validator 失败后，issues 作为 guidance 拼进下次 agent spawn 的 ctx。

    INTENT：guidance 反馈机制 —— orchestrator 把 issues 经 ``ctx.with_guidance`` 累积，
    下次 spawn 的 prompt 末尾会带 ``[User Guidance]`` 段（render_prompt 拼）。本测试 spy
    executor 收到的 ctx.user_guidance，断言第 2 次 spawn 时 guidance 含上次的 issues。
    """
    from orca.run.orchestrator import Orchestrator

    wf = _load_wf_with_validator(tmp_path, max_retries=1)
    tape = Tape(tmp_path / "e.jsonl", run_id="r1")
    bus = EventBus(tape)
    fake = _ScriptedExecutor([
        _complete({"x": "bad"}, node_name="agent"),
        _complete({"x": "good"}, node_name="agent"),
    ])
    _patch_make_executor(monkeypatch, fake)
    _patch_validate(monkeypatch, [
        (False, ["issue-A"]),
        (True, []),
    ])

    orch = Orchestrator(wf, bus, inputs={}, run_id="r1")
    run_async(orch.run())

    # 第 1 次 spawn：无 guidance（user_guidance=()）
    assert fake.ctx_guidance_history[0] == ()
    # 第 2 次 spawn：guidance 含上次 issues（前缀 "上次输出未通过校验：" + issues join）
    assert len(fake.ctx_guidance_history[1]) == 1
    guidance_text = fake.ctx_guidance_history[1][0]
    assert "上次输出未通过校验" in guidance_text
    assert "issue-A" in guidance_text


# ── 4. 用尽 → validator_failed(retrying=False) + ExecError(phase="validator") ────


def test_validator_exhausted_raises(tmp_path, monkeypatch):
    """总是失败 + max_retries=1 → 2 次尝试 → validator_failed(retrying=False) + ExecError。

    INTENT：用尽路径 fail loud（铁律 4）。最终 emit validator_failed(retrying=False) 标记
    「不再重试」，raise ExecError(phase="validator") → workflow_failed。
    """
    from orca.run.orchestrator import Orchestrator

    wf = _load_wf_with_validator(tmp_path, max_retries=1)
    tape = Tape(tmp_path / "e.jsonl", run_id="r1")
    bus = EventBus(tape)
    fake = _ScriptedExecutor([
        _complete({"x": "bad1"}, node_name="agent"),
        _complete({"x": "bad2"}, node_name="agent"),
    ])
    _patch_make_executor(monkeypatch, fake)
    _patch_validate(monkeypatch, [
        (False, ["issue-1"]),
        (False, ["issue-2"]),
    ])

    orch = Orchestrator(wf, bus, inputs={}, run_id="r1")
    state = run_async(orch.run())

    # ExecError(phase="validator") → workflow_failed
    assert state.status == "failed"
    types = _tape_types(tape)
    # 2 次校验都失败
    assert types.count("validator_started") == 2
    assert types.count("validator_failed") == 2
    # 第 1 个 failed: retrying=True（还有 1 次预算）；第 2 个 failed: retrying=False（用尽）
    failed_payloads = _tape_payloads(tape, "validator_failed")
    assert failed_payloads[0]["retrying"] is True
    assert failed_payloads[1]["retrying"] is False
    assert "workflow_failed" in types


def test_validator_max_retries_zero_single_attempt(tmp_path, monkeypatch):
    """max_retries=0 → 只校验 1 次，失败立即放弃（retrying=False）。

    INTENT：max_retries=0 是合法边界（ge=0）—— 不重跑 agent，校验失败即 fail。本测试守护
    「attempts_left = max_retries + 1 = 1」的算术：单次失败直接 emit failed(retrying=False)。
    """
    from orca.run.orchestrator import Orchestrator

    wf = _load_wf_with_validator(tmp_path, max_retries=0)
    tape = Tape(tmp_path / "e.jsonl", run_id="r1")
    bus = EventBus(tape)
    fake = _ScriptedExecutor([_complete({"x": "bad"}, node_name="agent")])
    _patch_make_executor(monkeypatch, fake)
    _patch_validate(monkeypatch, [(False, ["issue"])])

    orch = Orchestrator(wf, bus, inputs={}, run_id="r1")
    state = run_async(orch.run())

    assert state.status == "failed"
    types = _tape_types(tape)
    # 只校验 1 次
    assert types.count("validator_started") == 1
    assert types.count("validator_failed") == 1
    failed_payloads = _tape_payloads(tape, "validator_failed")
    assert failed_payloads[0]["retrying"] is False  # 用尽（无预算）
    # executor 只被调一次（无重跑）
    assert fake._idx == 1


# ── 5. validator 与 retry 预算独立（SPEC §11.6 deviation）──────────────────────


def test_validator_independent_of_retry_budget(tmp_path, monkeypatch):
    """node 同时声明 retry + validator → 两个 loop 独立预算，不共享计数。

    INTENT（SPEC §11.6 deviation）：transient 失败（spawn_error）由 execute_with_retry 重试
    （retry 预算 = max_attempts），validator 失败由 validator loop 重试（validator 预算 =
    max_retries+1）。两者正交：execute_with_retry 在 _execute_agent 内部跑，validator 在
    其外层包一层。本测试：第 1 次 exec transient 失败（retry 重试）→ 第 2 次 exec 成功但
    校验失败（validator 重试）→ 第 3 次 exec 成功且校验通过。retry_started + validator_failed
    都出现，预算各自消耗。

    场景编排（retry.max_attempts=3, validator.max_retries=1）：
      - execute_with_retry attempt 1: spawn_error → retry_started → attempt 2
      - execute_with_retry attempt 2: 成功 output-A（但 validator 说 fail）
        → validator_failed(retrying=True) → _execute_agent 再调一次
      - _execute_agent 第 2 次调 → execute_with_retry attempt 1: 成功 output-B → validator pass
    """
    from orca.run.orchestrator import Orchestrator

    wf = _load_wf_with_validator(tmp_path, max_retries=1, retry=True)
    tape = Tape(tmp_path / "e.jsonl", run_id="r1")
    bus = EventBus(tape)
    # executor 序列（_execute_agent 调 2 次，每次内部 execute_with_retry 消费若干 attempt）：
    # 第 1 次 _execute_agent → execute_with_retry 消费 [fail, complete-A]（attempt 1 fail, attempt 2 ok）
    # 第 2 次 _execute_agent → execute_with_retry 消费 [complete-B]（attempt 1 ok）
    fake = _ScriptedExecutor([
        _fail("spawn_error", "transient", node_name="agent"),  # _execute_agent #1 attempt 1
        _complete({"x": "A"}, node_name="agent"),  # _execute_agent #1 attempt 2（retry succeeded）
        _complete({"x": "B"}, node_name="agent"),  # _execute_agent #2 attempt 1
    ])
    _patch_make_executor(monkeypatch, fake)
    # validator：第 1 次（output-A）失败，第 2 次（output-B）通过
    _patch_validate(monkeypatch, [
        (False, ["A 不合规"]),
        (True, []),
    ])

    orch = Orchestrator(wf, bus, inputs={}, run_id="r1")
    state = run_async(orch.run())

    assert state.status == "completed"
    types = _tape_types(tape)
    # retry loop 消耗 1 次（attempt 1 spawn_error → retry_started → attempt 2 ok → retry_succeeded）
    assert "retry_started" in types, "transient 失败应触发 execute_with_retry 的 retry_started"
    assert "retry_succeeded" in types, "retry 后成功应 emit retry_succeeded"
    # validator loop 消耗 1 次（output-A 校验失败 → validator_failed(retrying=True) → 重跑 → output-B 通过）
    assert types.count("validator_started") == 2, "validator 校验 2 次（A 失败、B 通过）"
    assert types.count("validator_failed") == 1
    assert types.count("validator_passed") == 1
    failed_payloads = _tape_payloads(tape, "validator_failed")
    assert failed_payloads[0]["retrying"] is True
    # 最终 output 是 B
    assert state.context["agent"] == {"x": "B"}
    # retry_started 仅 1 个（transient 失败由 retry loop 处理，validator 失败不触发 retry loop）
    assert types.count("retry_started") == 1


# ── 6. validator_started criteria_preview = criteria[:100] ─────────────────────


def test_validator_started_criteria_preview_truncated(tmp_path, monkeypatch):
    """validator_started.data.criteria_preview = criteria 前 100 字符（SPEC §9.6.3）。

    INTENT：长 criteria 在 LogStream 显示时截短到 100 字符（防爆宽）。本测试用 150 字符
    criteria 断言 preview 恰好前 100。
    """
    from orca.run.orchestrator import Orchestrator

    long_criteria = "校验标准" + "X" * 150  # > 100 字符
    wf = _load_wf_with_validator(tmp_path, criteria=long_criteria, max_retries=0)
    tape = Tape(tmp_path / "e.jsonl", run_id="r1")
    bus = EventBus(tape)
    fake = _ScriptedExecutor([_complete({"x": 1}, node_name="agent")])
    _patch_make_executor(monkeypatch, fake)
    _patch_validate(monkeypatch, [(True, [])])

    orch = Orchestrator(wf, bus, inputs={}, run_id="r1")
    run_async(orch.run())

    started_payloads = _tape_payloads(tape, "validator_started")
    assert len(started_payloads) == 1
    preview = started_payloads[0]["criteria_preview"]
    assert preview == long_criteria[:100]
    assert len(preview) == 100


# ── 7. validator_passed issues 恒空（SPEC §9.6.3 payload 契约）─────────────────


def test_validator_passed_issues_always_empty(tmp_path, monkeypatch):
    """validator_passed.data.issues 恒 []（即使 validator claude 返回了 issues）。

    INTENT（SPEC §9.6.3）：passed=True 时 issues 必为 []。validate_output 内部已归一化
    （passed=True → 返回 (True, [])），orchestrator emit 时也固定 []。本测试守护这一契约。
    """
    from orca.run.orchestrator import Orchestrator

    wf = _load_wf_with_validator(tmp_path)
    tape = Tape(tmp_path / "e.jsonl", run_id="r1")
    bus = EventBus(tape)
    fake = _ScriptedExecutor([_complete({"x": 1}, node_name="agent")])
    _patch_make_executor(monkeypatch, fake)
    # 即便 validate 返回 passed=True 但带 issues（不应发生，但防御），orchestrator emit 时归一
    _patch_validate(monkeypatch, [(True, [])])

    orch = Orchestrator(wf, bus, inputs={}, run_id="r1")
    run_async(orch.run())

    passed_payloads = _tape_payloads(tape, "validator_passed")
    assert len(passed_payloads) == 1
    assert passed_payloads[0]["issues"] == []


def test_validator_passed_normalizes_dirty_issues_to_empty(tmp_path, monkeypatch):
    """validate_output 误返回 passed=True + stale issues → orchestrator emit 时归一为 []。

    INTENT（SPEC §9.6.3 不变量锁定）：validate_output 内部已归一（passed=True 时返回 []），
    orchestrator emit validator_passed 时也硬编码 ``"issues": []``（不取 validate 返回值）。
    本测试故意喂 ``(True, ["stale issue"])`` 脏输入给 fake validate，断言 tape 里
    validator_passed.issues 仍是 [] —— 锁定归一化逻辑，防未来误改成取 validate 返回值。

    回归保护：若有人把 orchestrator 的 emit 改成 ``"issues": issues``，此测试会失败。
    """
    from orca.run.orchestrator import Orchestrator

    wf = _load_wf_with_validator(tmp_path)
    tape = Tape(tmp_path / "e.jsonl", run_id="r1")
    bus = EventBus(tape)
    fake = _ScriptedExecutor([_complete({"x": 1}, node_name="agent")])
    _patch_make_executor(monkeypatch, fake)
    # 脏输入：passed=True 但带 stale issues（validate_output 内部已过滤，但 orchestrator
    # 是 emit 的最后一道防线，硬编码 [] 才是契约保证）
    _patch_validate(monkeypatch, [(True, ["stale issue that shouldn't appear"])])

    orch = Orchestrator(wf, bus, inputs={}, run_id="r1")
    run_async(orch.run())

    passed_payloads = _tape_payloads(tape, "validator_passed")
    assert len(passed_payloads) == 1
    # 关键：issues 恒 []，不透传 validate 的脏 issues
    assert passed_payloads[0]["issues"] == []


# ── 8. 多次失败 issues 累积进 guidance（tuple 追加，非覆盖）────────────────────


def test_validator_multi_failure_guidance_accumulates(tmp_path, monkeypatch):
    """max_retries=2 + 连续 2 次失败 → 第 3 次 spawn 的 ctx.user_guidance 有 2 条（累积非覆盖）。

    INTENT（RunContext.with_guidance 累积语义）：validator loop 每次 failed 把 issues 作
    guidance 经 ``loop_ctx = loop_ctx.with_guidance(text)`` 派生新 ctx。with_guidance 是
    ``+ (text,)`` tuple 追加（非覆盖）。本测试 spy executor 收到的 ctx.user_guidance，
    断言第 3 次 spawn 时 guidance tuple 长度 == 2（issue-1 + issue-2），守护累积语义。

    回归保护：若有人误改成 ``loop_ctx = ctx.with_guidance(text)``（用原 ctx 而非 loop_ctx，
    丢历史 guidance），此测试会失败（第 3 次 spawn 只有 1 条 guidance）。
    """
    from orca.run.orchestrator import Orchestrator

    wf = _load_wf_with_validator(tmp_path, max_retries=2)
    tape = Tape(tmp_path / "e.jsonl", run_id="r1")
    bus = EventBus(tape)
    fake = _ScriptedExecutor([
        _complete({"x": "bad1"}, node_name="agent"),
        _complete({"x": "bad2"}, node_name="agent"),
        _complete({"x": "bad3"}, node_name="agent"),
    ])
    _patch_make_executor(monkeypatch, fake)
    _patch_validate(monkeypatch, [
        (False, ["issue-1"]),
        (False, ["issue-2"]),
        (True, []),  # 第 3 次终于通过
    ])

    orch = Orchestrator(wf, bus, inputs={}, run_id="r1")
    state = run_async(orch.run())

    assert state.status == "completed"
    # 第 1 次 spawn：无 guidance
    assert fake.ctx_guidance_history[0] == ()
    # 第 2 次 spawn：1 条 guidance（issue-1）
    assert len(fake.ctx_guidance_history[1]) == 1
    assert "issue-1" in fake.ctx_guidance_history[1][0]
    # 第 3 次 spawn：2 条 guidance（issue-1 + issue-2，累积非覆盖）
    assert len(fake.ctx_guidance_history[2]) == 2
    all_guidance = " ".join(fake.ctx_guidance_history[2])
    assert "issue-1" in all_guidance
    assert "issue-2" in all_guidance


# ── 9. validator fail-safe 端到端可观测（SPEC §9.6.6）──────────────────────────


def test_validator_llm_crash_fail_safe_continues_workflow_e2e(tmp_path, monkeypatch):
    """validator LLM 自身崩（spawn 抛异常）→ fail-safe 当作 passed → workflow 继续 + completed。

    INTENT（SPEC §9.6.6 可观测端到端，非仅单元）：``validate_output`` 单元测试
    （tests/exec/test_validator.py::test_validate_output_*_fail_safe）已断言函数返回 (True, [])。
    但**没有**测试证明 orchestrator 的 ``_dispatch_with_validator`` loop 在 validator 「假装崩」时
    真的让 workflow 继续到 completed（而非卡住 / raise / workflow_failed）。本测试用真实
    ``validate_output``（不 monkeypatch 返回值），仅 monkeypatch ``CLIRunner`` 让它抛异常，
    驱动整个 loop：agent 产出 → validator_started → validator spawn 崩 → fail-safe →
    validator_passed → node_completed → workflow_completed。

    这是 fail-safe 契约的端到端证据（不阻塞 workflow）—— 单元返回值 (True,[]) 不足以证明
    orchestrator loop 真的据此继续。
    """
    from orca.run.orchestrator import Orchestrator
    from orca.exec import validator as validator_mod

    wf = _load_wf_with_validator(tmp_path, max_retries=1)
    tape = Tape(tmp_path / "e.jsonl", run_id="r1")
    bus = EventBus(tape)
    fake = _ScriptedExecutor([_complete({"model_class": "SimpleNet"}, node_name="agent")])
    _patch_make_executor(monkeypatch, fake)

    # 让真 validate_output 内部的 CLIRunner 在 stream() 抛异常（模拟 validator claude
    # binary 不存在 / subprocess 启动失败）。注意：真 CLIRunner.__init__ 不 spawn（只存
    # cfg），spawn 在 stream() 内 create_subprocess_exec —— 那才是 fail-safe try/except
    # 覆盖的真实失败点。本 factory 在 stream() 抛，复刻真实路径。
    class _CrashingRunner:
        def __init__(self, cfg, on_result=None):
            self.exit_code = 127
            self.stderr = "[Errno 2] No such file: claude"
            self.timed_out = False
            self.was_interrupted = False
            self.elapsed = 0.0

        async def stream(self):
            raise FileNotFoundError("[Errno 2] No such file: claude")
            yield  # noqa: unreachable —— 让 stream 是 async generator

    monkeypatch.setattr(validator_mod, "CLIRunner", _CrashingRunner)

    orch = Orchestrator(wf, bus, inputs={}, run_id="r1")
    state = run_async(orch.run())

    # fail-safe → workflow 继续（不阻塞）
    assert state.status == "completed", (
        f"validator LLM 崩应 fail-safe 让 workflow 继续，实际 status={state.status}"
    )
    types = _tape_types(tape)
    # validator_started emit 了（loop 进入校验）
    assert types.count("validator_started") == 1
    # fail-safe 路径 emit validator_passed（passed=True 归一化）—— 不 emit validator_failed
    assert types.count("validator_passed") == 1
    assert types.count("validator_failed") == 0, (
        "validator LLM 自身崩应 fail-safe（passed），不应 emit validator_failed"
    )
    # agent 的 output 被 workflow 接受（不因 validator 崩而丢）
    assert state.context["agent"] == {"model_class": "SimpleNet"}
