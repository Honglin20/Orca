"""tests/iface/in_session/test_failure_sentinel.py —— in-session 失败哨兵 + 失败历史注入 SPEC 验收。

覆盖 SPEC ``docs/specs/2026-08-04-in-session-failure-sentinel-and-injection.md``（v3）：
  - AC1：合法失败哨兵 → ``recoverable`` 信封（``error_kind=agent_blocked, retry_count=1,
    retry_budget=2``），``node_failed.data`` 含 ``blocked_on`` + ``tried``（AC gap 1）。
  - AC2：连续第 2 次 recoverable → 重 arm prompt 顶部含「前序尝试失败」块，列出第 1+2 次；
    首 attempt（count=0）prompt 无该块。
  - AC4（并入 AC2）：首 arm + re-arm prompt 末尾均含教学脚注；脚注为 literal append，
    节点 agent.md 模板本身不含。
  - AC3：3 次混合 kind → 终态 + tape 顺序 ``nf→ns→workflow_failed`` + 升格 kind = 本次 kind；
    NC 重置 fixture（2×blocked → nc → 1×schema 不终态，N11）。
  - AC5：哨兵检测顺序（peek 在 ``if not schema`` 早返前）—— 有/无 schema 节点发哨兵均 → agent_blocked。
  - AC6：畸形哨兵（blocked_on 缺/空）→ 仍 agent_blocked + message 含 "malformed sentinel" + data 省 blocked_on。
  - AC7：output_schema 含 ``_sentinel`` 字段 → 不崩，peek 优先判 agent_blocked。
  - AC9：信封无 fresh/复用指令字段；StepResult 无 dispatch 副作用（grep StepResult 字段集）。
  - AC10：跨 session resume（构造 tape 2 次 nf）→ 新 session ``advance_step()`` 无 output
    → 重发 prompt 含历史（2 条）。
  - AC11：``consecutive_fail_count`` delegate；``_render_failure_history`` 缺字段 data 不崩；
    ``tried`` wrong-type（str/dict/int）→ coerce 不崩。
  - AC13：``consecutive_failures`` 4 fixture（简单连续 / nc 重置 / 跨 ws / 缺字段 data 不崩）。
  - AC15：ingest 限长截断（超长 blocked_on + 超 tried）→ ``node_failed.data`` 对应字段已截断。
  - AC12：grep 守门——``_step_io.py``/``cli.py``/``daemon.py`` 可执行代码无 ``agent_blocked`` 字面分支。
  - AC14：AST 守门——``events/replay.py`` 的 ``node_failed`` 分支不读 ``data.*`` 字段。

测试路径：``advance_step(... prompts_dir=None)`` inline 是单测主路径（决策逻辑），
项目惯例：``asyncio.run``（无 pytest-asyncio，对齐 tests/iface/in_session/test_node_memory.py）。
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re
from pathlib import Path

import pytest

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.iface.in_session._step_io import apply_step_result
from orca.run import step as step_mod
from orca.run.step import (
    ERR_AGENT_BLOCKED,
    ERR_OUTPUT_SCHEMA_MISMATCH,
    Emit,
    InSessionError,
    RecoverableInSessionError,
    StepResult,
    _FAILURE_SENTINEL_FOOTER,
    _coerce_str,
    _coerce_tried,
    _kind_breakdown,
    _node_failed_data,
    _parse_output,
    _render_failure_history,
    advance_step,
    consecutive_fail_count,
    consecutive_failures,
)
from orca.schema.workflow import AgentNode, Route, Workflow


# ── fixtures / helpers ─────────────────────────────────────────────────────


def _wf_with_schema() -> Workflow:
    """单节点 agent wf（a → $end），a 声明 output_schema 要求 {k: string}。

    ``recoverable_max_attempts=3``：SPEC 2026-08-11 §5——既有测试隐式编码阈值 3
    （升格 / retry_budget=2），fixture 显式设值保持行为不变。
    """
    return Workflow(
        name="sentinel_wf",
        entry="a",
        recoverable_max_attempts=3,
        nodes=[
            AgentNode(
                name="a",
                executor="opencode",
                model="d/d",
                prompt="do A",
                output_schema={"type": "object", "required": ["k"],
                               "properties": {"k": {"type": "string"}}},
                routes=[Route(to="$end")],
            )
        ],
    )


def _wf_no_schema() -> Workflow:
    """单节点 agent wf（a → $end），a **无 output_schema**（§4.4 核心场景）。"""
    return Workflow(
        name="sentinel_no_schema_wf",
        entry="a",
        nodes=[
            AgentNode(
                name="a",
                executor="opencode",
                model="d/d",
                prompt="do A freely",
                routes=[Route(to="$end")],
            )
        ],
    )


def _sentinel(blocked_on: str = "卡在 X", tried: list[str] | None = None,
              reason: str | None = None) -> str:
    """构造合法失败哨兵 JSON 字符串（子代理最终消息形态）。"""
    d = {"blocked_on": blocked_on, "_sentinel": "orca_node_failed_v1"}
    if tried is not None:
        d["tried"] = tried
    if reason is not None:
        d["reason"] = reason
    return json.dumps(d, ensure_ascii=False)


def _write_tape(path: Path, events: list[tuple[str, str | None, dict]]) -> Tape:
    """从 (type, node, data) 序列构造 tape（单测 fixture 用）。seq 自动递增。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for seq, (etype, node, data) in enumerate(events, start=1):
        lines.append(json.dumps({
            "seq": seq, "type": etype, "timestamp": 0.0,
            "node": node, "session_id": None, "data": data,
        }, ensure_ascii=False))
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return Tape(path, run_id="r-test", resume=True)


def _apply(bus: EventBus, result: StepResult, wf: Workflow) -> None:
    """asyncio.run 包装：把 StepResult.emits 落 tape。"""
    asyncio.run(apply_step_result(bus, result, wf=wf, run_id="r-test"))


def _new_run(tmp_path: Path, wf: Workflow) -> tuple[Tape, EventBus, StepResult]:
    """bootstrap 一个 wf（advance_step inline），返 (tape, bus, entry result)。"""
    tape = Tape(tmp_path / "tape.jsonl", run_id="r-test", resume=True)
    bus = EventBus(tape)
    r0 = advance_step(tape, wf, run_id="r-test", prompts_dir=None)
    _apply(bus, r0, wf)
    return tape, bus, r0


# ── AC1：合法哨兵 → recoverable + data 含 blocked_on+tried ───────────────────


def test_ac1_legal_sentinel_recoverable_with_blocked_on_and_tried(tmp_path):
    """AC1（核心）：合法失败哨兵 → run 不终态；``node_failed.data`` 含 blocked_on+tried。

    StepResult：``recoverable=True, done=False, retry_count=1, retry_budget=2,
    error_kind=agent_blocked``；emits = ``[node_failed, node_started]``（无 workflow_failed）。
    """
    wf = _wf_with_schema()
    tape, bus, _ = _new_run(tmp_path, wf)

    sent = _sentinel(blocked_on="缺前置模型文件", tried=["ls models/", "find . -name '*.pt'"])
    result = advance_step(tape, wf, output=sent, run_id="r-test", prompts_dir=None)

    assert result.done is False
    assert result.recoverable is True
    assert result.node == "a"
    assert result.retry_count == 1
    assert result.retry_budget == 2
    assert result.error_kind == ERR_AGENT_BLOCKED
    types = [e.type for e in result.emits]
    assert types == ["node_failed", "node_started"]
    # AC gap 1：data 含 blocked_on + tried（= 哨兵原值）
    nf_data = result.emits[0].data
    assert nf_data["kind"] == ERR_AGENT_BLOCKED
    assert nf_data["error_type"] == ERR_AGENT_BLOCKED
    assert nf_data["phase"] == "agent_self_report"
    assert nf_data["blocked_on"] == "缺前置模型文件"
    assert nf_data["tried"] == ["ls models/", "find . -name '*.pt'"]


def test_ac1_then_correct_output_advances(tmp_path):
    """AC1 续：recoverable 后主 session 重派 → 合法 output 推进到 $end。"""
    wf = _wf_with_schema()
    tape, bus, _ = _new_run(tmp_path, wf)

    r1 = advance_step(tape, wf, output=_sentinel(), run_id="r-test", prompts_dir=None)
    assert r1.recoverable
    _apply(bus, r1, wf)

    r2 = advance_step(tape, wf, output='{"k": "ok"}', run_id="r-test", prompts_dir=None)
    assert r2.done is True
    assert r2.reason == "completed"


# ── AC2 + AC4：历史注入（含本次）+ 教学脚注恒在 + additive ─────────────────


def test_ac2_ac4_first_arm_no_history_but_has_footer(tmp_path):
    """AC2/AC4：首 attempt（count=0）prompt 无「前序尝试失败」块；但末尾**含**教学脚注。

    脚注为 literal append，节点 prompt 模板本身不含脚注串（additive，不改任务指令语义）。
    """
    wf = _wf_with_schema()
    tape = Tape(tmp_path / "tape.jsonl", run_id="r-test", resume=True)
    bus = EventBus(tape)
    r0 = advance_step(tape, wf, run_id="r-test", prompts_dir=None)

    # 首 arm prompt 含教学脚注（末尾）
    assert r0.prompt and r0.prompt.rstrip().endswith(_FAILURE_SENTINEL_FOOTER)
    # 首 arm 无失败历史块
    assert "前序尝试失败" not in r0.prompt
    # additive：节点模板本身不含脚注串
    node_a = next(n for n in wf.nodes if n.name == "a")
    assert _FAILURE_SENTINEL_FOOTER not in node_a.prompt


def test_ac2_ac4_re_arm_contains_history_inclusive_and_footer(tmp_path):
    """AC2/AC4：第 1 次 recoverable → 重 arm prompt 顶部含「前序尝试失败」块（1 条=本次）；
    末尾含教学脚注。第 2 次 recoverable → 历史块列出第 1+2 次（含本次，R-N2）。"""
    wf = _wf_with_schema()
    tape, bus, _ = _new_run(tmp_path, wf)

    # 1st 失败哨兵
    r1 = advance_step(
        tape, wf,
        output=_sentinel(blocked_on="B1", tried=["t1"]),
        run_id="r-test", prompts_dir=None,
    )
    assert r1.recoverable and r1.retry_count == 1
    # 重 arm prompt：顶部含历史块（1 条=本次），末尾含脚注
    assert r1.prompt is not None
    assert "前序尝试失败" in r1.prompt
    # 本次是历史块首条（含 blocked_on=B1）
    assert "B1" in r1.prompt
    assert "Attempt 1" in r1.prompt
    assert r1.prompt.rstrip().endswith(_FAILURE_SENTINEL_FOOTER)
    _apply(bus, r1, wf)

    # 2nd 失败哨兵
    r2 = advance_step(
        tape, wf,
        output=_sentinel(blocked_on="B2", tried=["t2"]),
        run_id="r-test", prompts_dir=None,
    )
    assert r2.recoverable and r2.retry_count == 2
    assert r2.prompt is not None
    # 历史块列出第 1+2 次（含本次 B2）
    assert "Attempt 1" in r2.prompt and "Attempt 2" in r2.prompt
    assert "B1" in r2.prompt and "B2" in r2.prompt
    # MINOR 3：ordering——Attempt 1 在 Attempt 2 前；history 块在节点 prompt 之前（prepend）
    assert r2.prompt.index("Attempt 1") < r2.prompt.index("Attempt 2")
    assert r2.prompt.index("前序尝试失败") < r2.prompt.index("do A")  # history 在节点 prompt 前
    assert r2.prompt.rstrip().endswith(_FAILURE_SENTINEL_FOOTER)


# ── AC3：升格 + 混合 kind + NC 重置 fixture ────────────────────────────────


def test_ac3_three_mixed_kinds_escalate_with_last_kind(tmp_path):
    """AC3：2×schema + 1×agent_blocked 连续 → 终态；升格 ``workflow_failed.data.kind``
    = 本次 kind（agent_blocked，非固定 schema_mismatch）。tape 顺序 ``nf→ns→workflow_failed``。"""
    wf = _wf_with_schema()
    tape, bus, _ = _new_run(tmp_path, wf)

    # 1st: schema mismatch（非 JSON）
    r1 = advance_step(tape, wf, output="BAD_NOT_JSON", run_id="r-test", prompts_dir=None)
    assert r1.recoverable and r1.error_kind == ERR_OUTPUT_SCHEMA_MISMATCH
    _apply(bus, r1, wf)

    # 2nd: schema mismatch（JSON 缺字段）
    r2 = advance_step(tape, wf, output='{"wrong": 1}', run_id="r-test", prompts_dir=None)
    assert r2.recoverable and r2.error_kind == ERR_OUTPUT_SCHEMA_MISMATCH
    _apply(bus, r2, wf)

    # 3rd: agent_blocked（哨兵）→ 升格，本次 kind
    r3 = advance_step(
        tape, wf,
        output=_sentinel(blocked_on="第三次卡住"),
        run_id="r-test", prompts_dir=None,
    )
    assert r3.done is True
    assert not r3.recoverable
    # m4/m6：升格 kind = 本次 kind（agent_blocked），非固定 schema_mismatch
    assert r3.error_kind == ERR_AGENT_BLOCKED
    types = [e.type for e in r3.emits]
    assert types == ["node_failed", "node_started", "workflow_failed"]
    assert r3.emits[2].data["kind"] == ERR_AGENT_BLOCKED
    # kind_breakdown 在 reason 中（output_schema_mismatch×2, agent_blocked×1）
    assert "output_schema_mismatch×2" in r3.reason
    assert "agent_blocked×1" in r3.reason


def test_ac3_new_run_does_not_inherit_prior_failure_count(tmp_path):
    """AC3 N11（reviewer MAJOR 1 修正）：新 run 不继承旧 run 的失败计数（cross-run 隔离）。

    注：NC reset **谓词本身**（遇 node_completed 归零）由
    ``test_ac13_consecutive_failures_reset_by_other_node_completed`` 直击验证。
    单节点 wf（a → $end）在 node_completed 后即终态，无法在同 run 内再失败；
    故本测试改测「cross-run 计数隔离」——新 run（新 Tape 文件）首次失败 retry_count=1，
    不被旧 run streak 污染（SSOT 在 tape 文件，新文件 = 新计数）。
    """
    wf = _wf_with_schema()
    tape, bus, _ = _new_run(tmp_path, wf)

    # 2× agent_blocked
    r1 = advance_step(tape, wf, output=_sentinel("b1"), run_id="r-test", prompts_dir=None)
    assert r1.recoverable and r1.retry_count == 1
    _apply(bus, r1, wf)
    r2 = advance_step(tape, wf, output=_sentinel("b2"), run_id="r-test", prompts_dir=None)
    assert r2.recoverable and r2.retry_count == 2
    _apply(bus, r2, wf)
    assert consecutive_fail_count(tape, "a") == 2

    # 正确 output → node_completed → 终态
    r3 = advance_step(tape, wf, output='{"k": "v"}', run_id="r-test", prompts_dir=None)
    assert r3.done is True and r3.reason == "completed"
    _apply(bus, r3, wf)

    # 新 run（新 Tape 文件）：1× schema_mismatch 不终态（count 从 0 重新累计）
    tape2, bus2, _ = _new_run(tmp_path / "sub", wf)
    r4 = advance_step(tape2, wf, output="BAD", run_id="r-test", prompts_dir=None)
    assert r4.recoverable and r4.retry_count == 1, (
        f"新 run 首次失败 retry_count 应为 1（不跨 run 污染），实得 {r4.retry_count}"
    )


def test_ac3_reverse_kind_order_escalates_with_last_kind(tmp_path):
    """AC3（reviewer MINOR 8）：反向 kind 序 1×blocked + 2×schema → LAST=schema_mismatch。

    防升格 kind 被硬编码为 ERR_OUTPUT_SCHEMA_MISMATCH 时因顺序巧合而通过——反向序
    断言 LAST kind = output_schema_mismatch（本次 kind），与 ``test_ac3_three_mixed_kinds_escalate_with_last_kind``
    互补（彼时 LAST=agent_blocked）。
    """
    wf = _wf_with_schema()
    tape, bus, _ = _new_run(tmp_path, wf)

    # 1× agent_blocked
    r1 = advance_step(tape, wf, output=_sentinel("blocked first"), run_id="r-test", prompts_dir=None)
    assert r1.recoverable and r1.error_kind == ERR_AGENT_BLOCKED
    _apply(bus, r1, wf)
    # 2× schema mismatch
    r2 = advance_step(tape, wf, output="BAD_JSON", run_id="r-test", prompts_dir=None)
    assert r2.recoverable and r2.error_kind == ERR_OUTPUT_SCHEMA_MISMATCH
    _apply(bus, r2, wf)
    # 3rd schema mismatch → 升格，LAST kind = output_schema_mismatch
    r3 = advance_step(tape, wf, output='{"wrong": 1}', run_id="r-test", prompts_dir=None)
    assert r3.done is True
    assert r3.error_kind == ERR_OUTPUT_SCHEMA_MISMATCH, (
        "反向序升格 LAST kind 应为 output_schema_mismatch（本次 kind）"
    )
    assert r3.emits[2].data["kind"] == ERR_OUTPUT_SCHEMA_MISMATCH
    assert "agent_blocked×1" in r3.reason and "output_schema_mismatch×2" in r3.reason


# ── AC5：哨兵检测顺序（peek 在 if not schema 早返前）────────────────────────


def test_ac5_sentinel_priority_over_schema_validation(tmp_path):
    """AC5：节点**有** output_schema 且 agent 发哨兵 → agent_blocked（不走 schema 校验）。"""
    wf = _wf_with_schema()
    tape, bus, _ = _new_run(tmp_path, wf)
    # 哨兵不合 schema（缺 k 字段）但应优先判 agent_blocked，不报 schema mismatch
    result = advance_step(
        tape, wf,
        output=_sentinel(blocked_on="主动报告受阻"),
        run_id="r-test", prompts_dir=None,
    )
    assert result.recoverable
    assert result.error_kind == ERR_AGENT_BLOCKED


def test_ac5_sentinel_detected_for_schemaless_node(tmp_path):
    """AC5（核心）：节点**无** output_schema 且 agent 发哨兵 → agent_blocked。

    peek 必须在 ``if not schema: return raw`` 早返**之前**——否则无 schema 节点哨兵静默失效。
    """
    wf = _wf_no_schema()
    tape, bus, _ = _new_run(tmp_path, wf)
    result = advance_step(
        tape, wf,
        output=_sentinel(blocked_on="无 schema 也报失败"),
        run_id="r-test", prompts_dir=None,
    )
    assert result.recoverable
    assert result.error_kind == ERR_AGENT_BLOCKED
    assert result.emits[0].data["blocked_on"] == "无 schema 也报失败"


def test_ac5_schemaless_node_non_sentinel_output_passes_through(tmp_path):
    """AC5 对照：无 schema 节点 + 非 JSON 纯文本（非哨兵）→ 当成功推进（raw 文本）。"""
    wf = _wf_no_schema()
    tape, bus, _ = _new_run(tmp_path, wf)
    result = advance_step(
        tape, wf, output="just plain text, not json",
        run_id="r-test", prompts_dir=None,
    )
    # 无 schema + 非哨兵 → raw 推进到 $end
    assert result.done is True
    assert result.reason == "completed"


# ── AC6：畸形哨兵 fail-loud ─────────────────────────────────────────────────


@pytest.mark.parametrize("wrong_sentinel", [
    "orca_node_failed_v2",      # 版本号错
    "orca_ask_user_v1",         # ask-user 哨兵（不同域，不应触发 failure 路径）
    "node_failed",              # 缺 orca_ 前缀
    "",                         # 空串
])
def test_ac6_sentinel_wrong_value_does_not_trigger_agent_blocked(tmp_path, wrong_sentinel):
    """AC6 安全契约（reviewer MAJOR 2）：``_sentinel`` **精确匹配**才触发 agent_blocked。

    错误值（v2 / ask-user / 缺前缀 / 空）→ **不**触发 failure 哨兵路径。无 schema 节点 →
    raw 透传当成功推进（不进 recoverable）。守住「精确匹配」防弱化为 contains/truthy。
    """
    wf = _wf_no_schema()
    tape, bus, _ = _new_run(tmp_path, wf)
    payload = json.dumps({"blocked_on": "x", "_sentinel": wrong_sentinel})
    result = advance_step(tape, wf, output=payload, run_id="r-test", prompts_dir=None)
    # 无 schema + 非匹配哨兵 → raw 透传 → done（推进到 $end），**不** recoverable
    assert not result.recoverable, (
        f"_sentinel={wrong_sentinel!r} 不应触发 agent_blocked（精确匹配契约），"
        f"实得 recoverable={result.recoverable}"
    )
    assert result.error_kind != ERR_AGENT_BLOCKED


def test_ac6_sentinel_non_string_value_does_not_trigger(tmp_path):
    """AC6 安全契约续：``_sentinel`` 非 string 值（int/null/bool）→ 不触发。"""
    wf = _wf_no_schema()
    tape, bus, _ = _new_run(tmp_path, wf)
    for wrong in [123, None, True]:
        payload = json.dumps({"blocked_on": "x", "_sentinel": wrong})
        result = advance_step(tape, wf, output=payload, run_id="r-test", prompts_dir=None)
        assert not result.recoverable, (
            f"_sentinel={wrong!r}（非 str）不应触发 agent_blocked"
        )


def test_ac6_malformed_sentinel_missing_blocked_on(tmp_path):
    """AC6：``_sentinel`` 匹配但 ``blocked_on`` 缺 → 仍 agent_blocked；message 含
    "malformed sentinel"；data 省 blocked_on 字段（不存 None）。"""
    wf = _wf_with_schema()
    tape, bus, _ = _new_run(tmp_path, wf)
    # blocked_on 缺，仅 _sentinel
    malformed = json.dumps({"_sentinel": "orca_node_failed_v1"})
    result = advance_step(tape, wf, output=malformed, run_id="r-test", prompts_dir=None)

    assert result.recoverable
    assert result.error_kind == ERR_AGENT_BLOCKED
    nf_data = result.emits[0].data
    assert "malformed sentinel" in nf_data["message"]
    # N5：data 省 blocked_on 字段（不存 None）
    assert "blocked_on" not in nf_data
    assert "tried" not in nf_data


def test_ac6_malformed_sentinel_empty_blocked_on(tmp_path):
    """AC6 子分支：blocked_on 为空串 → 仍 agent_blocked（不当成功放行）。"""
    wf = _wf_no_schema()
    tape, bus, _ = _new_run(tmp_path, wf)
    malformed = json.dumps({"blocked_on": "", "_sentinel": "orca_node_failed_v1"})
    result = advance_step(tape, wf, output=malformed, run_id="r-test", prompts_dir=None)

    assert result.recoverable
    assert result.error_kind == ERR_AGENT_BLOCKED
    assert "malformed sentinel" in result.emits[0].data["message"]
    assert "blocked_on" not in result.emits[0].data


def test_ac6_malformed_sentinel_falls_back_to_reason(tmp_path):
    """AC6 子分支：blocked_on 缺但 reason 存在 → message 取 reason（不报 malformed）。"""
    wf = _wf_no_schema()
    tape, bus, _ = _new_run(tmp_path, wf)
    malformed = json.dumps({
        "reason": "only reason given",
        "_sentinel": "orca_node_failed_v1",
    })
    result = advance_step(tape, wf, output=malformed, run_id="r-test", prompts_dir=None)
    assert result.recoverable
    # message = blocked_on or reason or "malformed..." → 取 reason
    assert "only reason given" in result.emits[0].data["message"]
    assert "malformed" not in result.emits[0].data["message"]


# ── AC7：保留键冲突不崩 ─────────────────────────────────────────────────────


def test_ac7_output_schema_containing_sentinel_field_does_not_crash(tmp_path):
    """AC7：节点 output_schema 恰好含 ``_sentinel`` 字段定义 → 不崩；哨兵 peek 优先判
    agent_blocked（compile 硬校验 deferred §9 R2）。"""
    wf = Workflow(
        name="reserved_key_wf",
        entry="a",
        nodes=[
            AgentNode(
                name="a",
                executor="opencode",
                model="d/d",
                prompt="do A",
                # 故意把 _sentinel 放进 schema（用户 yaml 写错的边界场景）
                output_schema={
                    "type": "object",
                    "required": ["_sentinel"],
                    "properties": {"_sentinel": {"type": "string"}},
                },
                routes=[Route(to="$end")],
            )
        ],
    )
    tape, bus, _ = _new_run(tmp_path, wf)
    # 哨兵 _sentinel 值是 "orca_node_failed_v1"，schema 要求 string —— schema 校验会过
    # 但 peek 先判 agent_blocked（优先级），不进 schema 校验
    result = advance_step(
        tape, wf,
        output=_sentinel(blocked_on="保留键冲突场景"),
        run_id="r-test", prompts_dir=None,
    )
    assert result.recoverable
    assert result.error_kind == ERR_AGENT_BLOCKED


# ── AC9：不侵 in-session 自由度 ─────────────────────────────────────────────


def test_ac9_envelope_no_dispatch_or_fresh_reuse_fields(tmp_path):
    """AC9：recoverable 信封**不含**「复用/fresh」指令字段；``_recover_step_result`` 不调用
    任何 dispatch。``StepResult`` 字段集不含 dispatch 相关字段（单测断言）。"""
    wf = _wf_with_schema()
    tape, bus, _ = _new_run(tmp_path, wf)
    result = advance_step(
        tape, wf,
        output=_sentinel(blocked_on="freedom check"),
        run_id="r-test", prompts_dir=None,
    )
    # 信封字段集（StepResult）—— 不含 dispatch / fresh / reuse 等指令字段
    forbidden = {"dispatch", "fresh", "reuse", "fresh_agent", "reuse_agent", "spawn"}
    stepresult_fields = set(inspect.signature(StepResult).parameters.keys())
    assert not (stepresult_fields & forbidden), (
        f"StepResult 不应含 dispatch/fresh/reuse 指令字段，实得交集 {stepresult_fields & forbidden}"
    )
    # ``_recover_step_result`` 源码不含 dispatch / spawn 调用
    src = inspect.getsource(step_mod._recover_step_result)
    assert "dispatch" not in src.lower(), "_recover_step_result 不应调用 dispatch"
    assert "spawn" not in src.lower(), "_recover_step_result 不应调用 spawn"


# ── AC10：cross-session resume（幂等重发分支注入历史）──────────────────────


def test_ac10_cross_session_resume_injects_tape_history(tmp_path):
    """AC10：构造 tape（2 次 nf）→ 新 session ``advance_step()`` 无 output 走幂等重发分支
    → 重发 prompt **已含** tape 重建的失败历史（2 条 prior，含本次=无新失败时全部 prior）。

    取代 2026-07-23 §5(A)3「主 session 手动注入」—— SSOT 在 tape。
    """
    wf = _wf_with_schema()
    tape, bus, _ = _new_run(tmp_path, wf)

    # 模拟 session 1：2 次 agent_blocked 失败落 tape
    r1 = advance_step(tape, wf, output=_sentinel("prior1"), run_id="r-test", prompts_dir=None)
    _apply(bus, r1, wf)
    r2 = advance_step(tape, wf, output=_sentinel("prior2"), run_id="r-test", prompts_dir=None)
    _apply(bus, r2, wf)
    assert consecutive_fail_count(tape, "a") == 2

    # 模拟 session 2（跨 session）：**新建 Tape 对象**从磁盘重放（证 SSOT 在 tape 文件，
    # 非内存对象）。新 session 调 ``advance_step()`` 无 output。
    # V2-2：count=2（已含落 tape 的失败），retry_count=count=2、retry_budget=N-count=1
    tape_fresh = Tape(tmp_path / "tape.jsonl", run_id="r-test", resume=True)
    r_resend = advance_step(tape_fresh, wf, run_id="r-test", prompts_dir=None)
    assert r_resend.done is False
    assert r_resend.node == "a"
    assert r_resend.emits == []  # 幂等重发不 emit
    assert r_resend.prompt is not None
    # prompt 顶部含 tape 重建的失败历史（2 条 prior）
    assert "前序尝试失败" in r_resend.prompt
    assert "Attempt 1" in r_resend.prompt and "Attempt 2" in r_resend.prompt
    assert "prior1" in r_resend.prompt and "prior2" in r_resend.prompt


def test_ac10_idempotent_resend_count_zero_no_history(tmp_path):
    """AC10 边界：``advance_step()`` 无 output 且 count=0（无历史）→ 重发 prompt 无历史块
    （仍有教学脚注）。"""
    wf = _wf_with_schema()
    tape, bus, _ = _new_run(tmp_path, wf)
    # 没 fail 过，直接幂等重发
    r = advance_step(tape, wf, run_id="r-test", prompts_dir=None)
    assert r.done is False
    assert r.prompt is not None
    assert "前序尝试失败" not in r.prompt
    # 仍有脚注
    assert r.prompt.rstrip().endswith(_FAILURE_SENTINEL_FOOTER)


# ── AC11：DRY delegate + robustness + 类型 coerce ───────────────────────────


def test_ac11_consecutive_fail_count_delegates_to_consecutive_failures(tmp_path):
    """AC11：``consecutive_fail_count`` delegate ``len(consecutive_failures(...))`` —— DRY。"""
    src = inspect.getsource(consecutive_fail_count)
    assert "len(consecutive_failures(" in src, (
        "consecutive_fail_count 必须 delegate len(consecutive_failures(...))（DRY 单实现）"
    )


def test_ac11_render_failure_history_missing_fields_does_not_crash():
    """AC11：``_render_failure_history`` 对缺字段 data 不崩（防御 ``.get()``，AC13 同理）。"""
    # 完全空 dict
    out = _render_failure_history([{}], retry_count=1, retry_budget=2, max_attempts=3)
    assert out is not None
    assert "Attempt 1" in out
    # 缺 kind 但有 message
    out2 = _render_failure_history(
        [{"message": "partial"}], retry_count=1, retry_budget=2, max_attempts=3,
    )
    assert "partial" in out2
    # agent_blocked 但缺 blocked_on（fallback message）
    out3 = _render_failure_history(
        [{"kind": ERR_AGENT_BLOCKED, "message": "fb"}], retry_count=1, retry_budget=2, max_attempts=3,
    )
    assert "blocked_on: fb" in out3


@pytest.mark.parametrize("wrong_type", ["a single string", {"k": "v"}, 123, True, None])
def test_ac11_coerce_tried_wrong_type_does_not_crash(wrong_type):
    """AC11：``tried`` wrong-type（str/dict/int/bool/None）→ coerce 为 list 不崩、不乱码。"""
    out = _coerce_tried(wrong_type)
    if wrong_type is None:
        assert out is None
    else:
        assert isinstance(out, list)
        assert len(out) == 1
        assert isinstance(out[0], str)


def test_ac11_coerce_tried_empty_list_returns_none():
    """AC11（reviewer MINOR 6）：空 list → None（``_node_failed_data`` 据此省 tried 字段）。"""
    assert _coerce_tried([]) is None


def test_ac11_coerce_tried_list_of_non_str_elements():
    """AC11（reviewer MINOR 6）：list 含非 str 元素（int/dict/list）→ 元素 ``str()`` 化。"""
    out = _coerce_tried([1, {"k": "v"}, [1, 2]])
    assert out == ["1", "{'k': 'v'}", "[1, 2]"]


def test_ac11_coerce_str_none_and_empty_returns_none():
    """AC11 子分支：``_coerce_str`` None/空串 → None；非 str → str()；超长截断。"""
    assert _coerce_str(None) is None
    assert _coerce_str("") is None
    assert _coerce_str(123) == "123"
    assert _coerce_str("ok") == "ok"
    assert len(_coerce_str("x" * 500)) == 200


# ── AC13：``consecutive_failures`` 直接覆盖 4 fixture ───────────────────────


def test_ac13_consecutive_failures_simple_run(tmp_path):
    """AC13 fixture (i)：简单连续 node_failed(a) → 2 条 data。"""
    tape = _write_tape(tmp_path / "t.jsonl", [
        ("workflow_started", None, {}),
        ("node_started", "a", {}),
        ("node_failed", "a", {"kind": ERR_AGENT_BLOCKED, "blocked_on": "b1"}),
        ("node_started", "a", {}),
        ("node_failed", "a", {"kind": ERR_OUTPUT_SCHEMA_MISMATCH, "message": "m"}),
        ("node_started", "a", {}),
    ])
    records = consecutive_failures(tape, "a")
    assert len(records) == 2
    assert records[0]["blocked_on"] == "b1"
    assert records[1]["kind"] == ERR_OUTPUT_SCHEMA_MISMATCH


def test_ac13_consecutive_failures_reset_by_other_node_completed(tmp_path):
    """AC13 fixture (ii)：被他节点 node_completed 重置 → 只计末尾 streak。"""
    tape = _write_tape(tmp_path / "t.jsonl", [
        ("workflow_started", None, {}),
        ("node_failed", "a", {"kind": ERR_AGENT_BLOCKED}),  # 旧 streak
        ("node_completed", "b", {"output": "ok"}),          # 他节点 nc → 重置
        ("node_failed", "a", {"kind": ERR_AGENT_BLOCKED, "blocked_on": "new"}),  # 新 streak（=1）
    ])
    records = consecutive_failures(tape, "a")
    assert len(records) == 1
    assert records[0]["blocked_on"] == "new"


def test_ac13_consecutive_failures_crosses_workflow_started_boundary(tmp_path):
    """AC13 fixture (iii)：跨 workflow_started 边界 —— ws 不是重置点。"""
    tape = _write_tape(tmp_path / "t.jsonl", [
        ("workflow_started", None, {}),
        ("node_failed", "a", {"kind": ERR_AGENT_BLOCKED}),
        ("workflow_started", None, {}),   # ws 不重置
        ("node_failed", "a", {"kind": ERR_AGENT_BLOCKED}),
    ])
    records = consecutive_failures(tape, "a")
    assert len(records) == 2


def test_ac13_consecutive_failures_missing_data_does_not_crash(tmp_path):
    """AC13 fixture (iv)：缺字段 data（部分字段缺失，非整体 None）→ 返原 dict，
    消费方 ``.get()`` 防御。``_render_failure_history`` / ``_kind_breakdown`` 不崩。

    注：Tape 的 Event pydantic schema 强制 ``data: dict``（不接受 None），故「整体 None」
    经 Tape 不可能发生；本 fixture 测「字段缺失」（如只传 ``{}`` 或 ``{"kind": ...}``）。
    """
    tape = _write_tape(tmp_path / "t.jsonl", [
        ("workflow_started", None, {}),
        ("node_failed", "a", {}),  # 完全空 dict
        ("node_failed", "a", {"kind": ERR_AGENT_BLOCKED}),  # 缺 blocked_on/tried/message
    ])
    records = consecutive_failures(tape, "a")
    assert len(records) == 2
    # 消费方防御：``_kind_breakdown`` 对空 dict 不崩（``?`` fallback）
    breakdown = _kind_breakdown(records)
    assert "agent_blocked×1" in breakdown  # 第 1 条 {} → ``?``；第 2 条 → agent_blocked
    # ``_render_failure_history`` 对缺字段不崩
    out = _render_failure_history(records, retry_count=1, retry_budget=2, max_attempts=3)
    assert out is not None and "Attempt 1" in out and "Attempt 2" in out


# ── AC15：ingest 限长截断 ───────────────────────────────────────────────────


def test_ac15_long_blocked_on_and_tried_truncated(tmp_path):
    """AC15：超长 blocked_on（500 字符）+ 超 tried（10 项 × 长串）→ ``node_failed.data``
    对应字段已截断（blocked_on ≤200；tried ≤5 项 × 每项 ≤120 字符）。"""
    wf = _wf_no_schema()
    tape, bus, _ = _new_run(tmp_path, wf)

    long_blocked = "B" * 500
    long_tried = [("T" * 200) for _ in range(10)]  # 10 项 × 200 字符
    sent = json.dumps({
        "blocked_on": long_blocked,
        "tried": long_tried,
        "_sentinel": "orca_node_failed_v1",
    })
    result = advance_step(tape, wf, output=sent, run_id="r-test", prompts_dir=None)
    assert result.recoverable
    nf_data = result.emits[0].data
    # blocked_on 截断到 200
    assert len(nf_data["blocked_on"]) == 200
    # tried 截断到 5 项 × 120 字符
    assert len(nf_data["tried"]) == 5
    assert all(len(it) == 120 for it in nf_data["tried"])


def test_ac15_long_reason_truncated_when_blocked_on_absent(tmp_path):
    """AC15（reviewer MINOR 2）：超长 reason（500 字符）+ blocked_on 缺 → reason 截断到 200
    后作为 message（SPEC §4.1：reason ≤200；message 优先级 = blocked_on or reason or malformed）。"""
    wf = _wf_no_schema()
    tape, bus, _ = _new_run(tmp_path, wf)
    long_reason = "R" * 500
    sent = json.dumps({"reason": long_reason, "_sentinel": "orca_node_failed_v1"})
    result = advance_step(tape, wf, output=sent, run_id="r-test", prompts_dir=None)
    assert result.recoverable
    nf_data = result.emits[0].data
    # blocked_on 缺 → reason fallback；reason 截断到 200 = message
    assert "blocked_on" not in nf_data
    assert len(nf_data["message"]) == 200
    assert nf_data["message"] == "R" * 200


# ── AC12：cli/daemon/_step_io 零改守门（grep 可执行代码无 agent_blocked 字面分支）──


def _src_without_comments_and_docstrings(path: Path) -> str:
    """剥 Python 源码的 comment / docstring（AC12 N6：排除注释后的「可执行代码」）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # 移除 docstring（模块 / 函数 / 类体首条 Expr-str）
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body.pop(0)
    import ast as _ast
    return _ast.unparse(tree)


@pytest.mark.parametrize("rel", [
    "orca/iface/in_session/_step_io.py",
    "orca/iface/in_session/cli.py",
    "orca/iface/in_session/daemon.py",
])
def test_ac12_no_agent_blocked_literal_branch_in_executable_code(rel):
    """AC12（N6）：``_step_io.py``/``cli.py``/``daemon.py`` 的**可执行代码**（排除 docstring/
    comment）无 ``agent_blocked`` 字面分支——generic 透传，零改前提守门。"""
    repo_root = Path(__file__).resolve().parents[3]
    src = _src_without_comments_and_docstrings(repo_root / rel)
    # 可执行代码中 ``agent_blocked`` 字面出现 = 有人加了 kind 分支（违反 C2/C3/C4 generic 前提）
    assert "agent_blocked" not in src, (
        f"{rel} 可执行代码不得含 'agent_blocked' 字面分支（C2/C3/C4 generic 前提，AC12 守门）"
    )


# ── AC14：C1 回归守门（AST：events/replay.py 的 node_failed 分支不读 data.*）──


def test_ac14_replay_node_failed_branch_does_not_read_data_fields():
    """AC14（N9）：AST 断言 ``events/replay.py`` 的 ``node_failed`` 分支不读 ``data.*`` 字段。

    守 C1 不变量：reducer 对 node_failed 只置 ``node_status[node]=failed``，**不读 data 任何字段**
    → data 是开放 dict，可 additive 扩展（agent_blocked 加 blocked_on/tried 不影响 reducer）。
    """
    repo_root = Path(__file__).resolve().parents[3]
    src = (repo_root / "orca/events/replay.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 找到所有 ``if t == "node_failed":`` 分支，检查其 body 不读 data.*
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        # 比较 ``t == "node_failed"`` 或 ``"node_failed" == t``
        is_nf_branch = False
        test = node.test
        if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) \
                and test.left.id == "t":
            for cmp in test.comparators:
                if isinstance(cmp, ast.Constant) and cmp.value == "node_failed":
                    is_nf_branch = True
        elif isinstance(test, ast.Compare) and isinstance(test.left, ast.Constant) \
                and test.left.value == "node_failed":
            is_nf_branch = True
        if not is_nf_branch:
            continue

        # 检查 body 中无 ``data[...]`` / ``data.get(...)`` / ``data.xxx`` 形式
        for sub in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            if isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name) \
                    and sub.value.id == "data":
                pytest.fail(f"node_failed 分支读了 data[...]（违反 C1 不变量）：{ast.dump(sub)}")
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                    and isinstance(sub.func.value, ast.Name) \
                    and sub.func.value.id == "data" and sub.func.attr == "get":
                pytest.fail(f"node_failed 分支调了 data.get(...)（违反 C1 不变量）：{ast.dump(sub)}")
            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                    and sub.value.id == "data":
                pytest.fail(f"node_failed 分支读了 data.{sub.attr}（违反 C1 不变量）：{ast.dump(sub)}")
