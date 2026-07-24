"""tests/iface/in_session/test_daemon.py —— ``InSessionDaemon`` 守门测试（v5 §8 step 5b）。

InSessionDaemon 此前**零覆盖**（spec-reviewer issue5/6）。本模块建脚手架，重点守门 step 5b
两件真活：

1. **batch emit（spec-reviewer Q1「真活」裁定）**：``daemon.next()`` 成功路径用
   ``emit_batch``（单次 write 原子化），**非**逐条 ``emit``。spy bus 断言 ``emit_batch``
   被调、``emit`` 在成功路径不被调。
2. **错误信封统一（spec-reviewer issue1/2 + 字段陷阱 B4/B7）**：daemon 失败路径用
   ``fail_in_session`` → 读 ``InSessionError.error_kind``（取代旧 isinstance 塌缩成
   ``in_session_error``）。断言：
     - tape 末条 ``workflow_failed`` 的 ``data["kind"]`` == 具体 kind（字段名 ``kind``）。
     - 回复信封 ``reply["error_kind"]`` == 同值（**字段名 ``error_kind``**，新）。
     - ``reply`` / ``data`` **均不得出现** ``"in_session_error"`` 字面量（反向断言，塌缩消除）。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from orca.compile import load_workflow
from orca.iface.in_session.daemon import InSessionDaemon


# ── fixtures ────────────────────────────────────────────────────────────────


# entry 节点声明 output_schema（type:object + required:[k]）—— 用于构造 output_schema_mismatch。
DAEMON_WF_YAML_WITH_SCHEMA = """\
name: daemon_test_wf
description: daemon 守门测试 wf（entry 带 output_schema）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 step A 的输出。"
    output_schema:
      type: object
      required: [k]
      properties:
        k: {type: string}
    routes:
      - to: $end
"""


# 无 output_schema 的单节点 wf —— 用于成功路径 + batch emit spy（裸 output 任意串即可）。
DAEMON_WF_YAML_PLAIN = """\
name: daemon_plain_wf
description: daemon 成功路径测试 wf（无 output_schema）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 step A 的输出。"
    routes:
      - to: $end
"""


@pytest.fixture
def wf_with_schema(tmp_path: Path) -> Path:
    p = tmp_path / "wf_schema.yaml"
    p.write_text(DAEMON_WF_YAML_WITH_SCHEMA, encoding="utf-8")
    return p


@pytest.fixture
def wf_plain(tmp_path: Path) -> Path:
    p = tmp_path / "wf_plain.yaml"
    p.write_text(DAEMON_WF_YAML_PLAIN, encoding="utf-8")
    return p


def _make_daemon(wf_path: Path, tmp_path: Path, run_id: str = "r-daemon-test") -> InSessionDaemon:
    """构造 daemon（flock + pid + tape，resume=True）。tape 落 tmp_path 隔离每个测试。"""
    wf = load_workflow(wf_path)
    tape_path = tmp_path / f"{run_id}.jsonl"
    return InSessionDaemon(wf, tape_path, run_id)


def _tape_events(tape_path: Path) -> list[dict]:
    """读 tape jsonl 全量事件（每行一个 JSON 对象）。"""
    text = tape_path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    return [json.loads(ln) for ln in text.split("\n")]


def _next(daemon: InSessionDaemon) -> dict:
    """同步驱动 ``daemon.next()``（项目惯例：直接 asyncio.run，无 pytest-asyncio）。"""
    return asyncio.run(daemon.next())


# ── 成功路径 + batch emit spy（spec-reviewer Q1「真活」）──────────────────────


def test_daemon_success_path_emits_batch_and_completes(wf_plain, tmp_path):
    """成功路径：bootstrap（无 output）→ entry 起来；observe(output) → next → completed。

    单节点 wf：bootstrap 起 entry，observe 任意 output（无 schema）→ next emit
    [node_completed, route_taken, workflow_completed] → done。
    """
    daemon = _make_daemon(wf_plain, tmp_path)
    try:
        # 1. bootstrap（无 pending output）→ 起 entry 节点
        reply0 = _next(daemon)
        assert reply0["done"] is False
        assert reply0["node"] == "a"
        assert reply0["prompt"]  # inline 交付（daemon 不传 prompts_dir）

        # tape 2 行：workflow_started + node_started
        events0 = _tape_events(daemon.tape_path)
        assert [e["type"] for e in events0] == ["workflow_started", "node_started"]

        # 2. observe output → next → workflow 完成
        daemon.observe("任意产出（无 schema，裸串 OK）")
        reply1 = _next(daemon)
        assert reply1["done"] is True

        # tape 追加 nc + rt + workflow_completed（共 5 行）
        events1 = _tape_events(daemon.tape_path)
        types = [e["type"] for e in events1]
        assert types == [
            "workflow_started", "node_started",
            "node_completed", "route_taken", "workflow_completed",
        ]
    finally:
        daemon.cleanup()


def test_daemon_success_uses_emit_batch_not_per_emit(wf_plain, tmp_path):
    """spec-reviewer Q1（batch emit 真活）：daemon.next() 成功路径调 ``emit_batch``，非逐条 emit。

    spy ``bus.emit_batch`` / ``bus.emit``：bootstrap（2 emits）应触发 emit_batch 一次（2 items）、
    emit 零次。这守门「SIGTERM 半截 tape」修复——逐条 emit 时 N 与 N+1 之间落信号会留半截 tape；
    emit_batch 单次 write 原子化消除该窗口。
    """
    daemon = _make_daemon(wf_plain, tmp_path)
    batch_calls: list[int] = []  # 记录每次 emit_batch 的 items 数
    emit_calls: list[tuple] = []

    orig_batch = daemon.bus.emit_batch
    orig_emit = daemon.bus.emit

    async def spy_batch(items):
        batch_calls.append(len(items))
        return await orig_batch(items)

    async def spy_emit(*a, **kw):
        emit_calls.append(a)
        return await orig_emit(*a, **kw)

    daemon.bus.emit_batch = spy_batch
    daemon.bus.emit = spy_emit
    try:
        _next(daemon)  # bootstrap：emit_batch 一次（2 items：ws + ns），emit 零次
        assert batch_calls == [2], (
            f"bootstrap 应 emit_batch 一次（2 items），实得 batch_calls={batch_calls}"
        )
        assert emit_calls == [], (
            f"成功路径不得逐条 emit（应走 emit_batch），实得 emit_calls={emit_calls}"
        )
    finally:
        daemon.cleanup()


# ── recoverable 路径：output_schema_mismatch 不判死 run（SPEC 2026-07-23 §3）────
# daemon 路径经 advance_step 自动复用 recoverable（SPEC §6 / 计划 S6）：
# advance_step 自捕 RecoverableInSessionError → 返 StepResult(recoverable=True)（不外抛）
# → daemon.next 的 except InSessionError 不触发 → apply_step_result 返 recoverable 信封。


def test_daemon_recoverable_envelope_carries_error_kind(wf_with_schema, tmp_path):
    """daemon recoverable 路径：output 畸形 → run 存活 + ``output_schema_mismatch`` 信封。

    SPEC 2026-07-23 §3：output_schema_mismatch 不再判死。断言（字段名陷阱 B4/B7）：
      (a) tape ``node_failed.data["kind"]`` == ``"output_schema_mismatch"``（字段名 ``kind``）。
      (b) 回复信封 ``reply["error_kind"]`` == ``"output_schema_mismatch"``（信封字段 ``error_kind``）。
      (c) ``reply`` / ``data`` 中**均不得出现** ``"in_session_error"`` 字面量（塌缩消除）。
      (d) ``reply["done"]`` is False（run 存活）；``reply["recoverable"]`` is True。
    """
    daemon = _make_daemon(wf_with_schema, tmp_path)
    try:
        # bootstrap 起 entry（带 output_schema 的节点 a）
        reply0 = _next(daemon)
        assert reply0["done"] is False
        assert reply0["node"] == "a"

        # observe 畸形 output（非 JSON）→ advance_step 自捕 RecoverableInSessionError → recoverable 信封
        daemon.observe("NOT_JSON")
        reply = _next(daemon)

        # (d) run 存活（不终态）
        assert reply["done"] is False
        assert reply["recoverable"] is True
        assert reply["retry_count"] == 1
        # (b) 信封 error_kind 字段携带具体分类
        assert reply["error_kind"] == "output_schema_mismatch", (
            f"信封 error_kind 应为 output_schema_mismatch，实得 {reply.get('error_kind')!r}"
        )
        assert "output_schema" in reply["reason"] or "非 JSON" in reply["reason"]

        # (c) 反向断言：信封不得出现塌缩值 "in_session_error"
        _assert_no_in_session_error(reply, "recoverable 信封 reply")

        # (a) tape 末两条 = [node_failed, node_started]（重 arm；无 workflow_failed）
        events = _tape_events(daemon.tape_path)
        assert events[-1]["type"] == "node_started"
        assert events[-2]["type"] == "node_failed"
        data = events[-2]["data"]
        assert data["kind"] == "output_schema_mismatch", (
            f"tape node_failed.data.kind 应为 output_schema_mismatch，实得 {data.get('kind')!r}"
        )
        # (c) 反向断言：tape data 不得出现塌缩值
        _assert_no_in_session_error(data, "tape node_failed.data")
    finally:
        daemon.cleanup()


def test_daemon_recoverable_then_correct_output_completes(wf_with_schema, tmp_path):
    """AC1 daemon 闭环（code-reviewer 🟡#3）：坏 → 正解 → workflow 完成。

    守 ``daemon.next()`` 缓存清理（``self._pending_output = None``）+ advance_step 重 arm 后
    接正解 output 的衔接 —— 防缓存未清导致死循环 / 防重 arm 后接 output 不能推进的回归。
    """
    daemon = _make_daemon(wf_with_schema, tmp_path)
    try:
        _next(daemon)  # bootstrap 起 entry a
        # 1st bad → recoverable（缓存清）
        daemon.observe("NOT_JSON")
        r1 = _next(daemon)
        assert r1["recoverable"] is True
        assert r1["retry_count"] == 1

        # 2nd: 正解 output（满足 schema {k: string}）→ 完成
        daemon.observe('{"k": "v"}')
        r2 = _next(daemon)
        assert r2["done"] is True, f"正解 output 后应推进到 $end，实得 {r2}"

        # tape 序列：ws + ns + nf + ns + nc + rt + workflow_completed
        events = _tape_events(daemon.tape_path)
        types = [e["type"] for e in events]
        assert types == [
            "workflow_started", "node_started",
            "node_failed", "node_started",                # 1st bad 重 arm
            "node_completed", "route_taken", "workflow_completed",
        ]
    finally:
        daemon.cleanup()


def test_daemon_recoverable_escalation_3x_surfaces_error_kind(wf_with_schema, tmp_path):
    """AC2 daemon 升格（code-reviewer 🟡#1+#2，配 ``_step_io`` parity 修复）：

    连续 3 次 recoverable 失败 → daemon.next 返 ``{done:true, error_kind:output_schema_mismatch}``。
    守住 ``apply_step_result`` 在 ``result.done + result.error_kind`` 时 surface error_kind
    （修复前 daemon 升格终态信封丢 error_kind，cli/daemon parity bug）。
    """
    daemon = _make_daemon(wf_with_schema, tmp_path)
    try:
        _next(daemon)  # bootstrap

        # 1st + 2nd bad → recoverable（run 存活）
        daemon.observe("BAD1")
        r1 = _next(daemon)
        assert r1["recoverable"] is True and r1["retry_count"] == 1
        daemon.observe("BAD2")
        r2 = _next(daemon)
        assert r2["recoverable"] is True and r2["retry_count"] == 2

        # 3rd bad → 升格 → done:true + error_kind:output_schema_mismatch
        daemon.observe("BAD3")
        r3 = _next(daemon)
        assert r3["done"] is True, f"3 次失败应升格 done:true，实得 {r3}"
        assert r3.get("error_kind") == "output_schema_mismatch", (
            f"daemon 升格信封必须 surface error_kind（cli/daemon parity），实得 {r3.get('error_kind')!r}"
        )
        assert "exhausted" in (r3.get("reason") or "")

        # tape 含 3 条 node_failed + 1 条 workflow_failed（E8：nf→ns→workflow_failed 末轮）
        events = _tape_events(daemon.tape_path)
        nf_count = sum(1 for e in events if e["type"] == "node_failed")
        assert nf_count == 3, f"升格应 emit 3 条 node_failed，实得 {nf_count}"
        last = events[-1]
        assert last["type"] == "workflow_failed"
        assert last["data"]["kind"] == "output_schema_mismatch"
    finally:
        daemon.cleanup()


def _assert_no_in_session_error(obj: dict, label: str) -> None:
    """反向断言：obj 的值中不含 ``"in_session_error"`` 字面量（塌缩消除守门）。

    扫 obj 自身 + 嵌套 dict/list 的所有 str 值。
    """
    found: list[str] = []

    def _scan(o):
        if isinstance(o, str):
            if o == "in_session_error":
                found.append(o)
        elif isinstance(o, dict):
            for v in o.values():
                _scan(v)
        elif isinstance(o, list):
            for v in o:
                _scan(v)

    _scan(obj)
    assert not found, (
        f"{label} 不得出现 'in_session_error' 字面量（旧 isinstance 塌缩值，5b 已消除）：{found}"
    )


# ── 终态幂等：已完成 run 再 next 不 emit（advance_step branch 1）──────────────


def test_daemon_next_after_terminal_is_idempotent(wf_plain, tmp_path):
    """已完成 run 再调 next → ``{done:True, reason:"already_completed"}``，不 emit。"""
    daemon = _make_daemon(wf_plain, tmp_path)
    try:
        _next(daemon)  # bootstrap
        daemon.observe("out_a")
        done_reply = _next(daemon)
        assert done_reply["done"] is True

        # 再调 next（无 pending output）→ advance_step branch 1（已终态）→ done, no emit
        events_before = len(_tape_events(daemon.tape_path))
        reply = _next(daemon)
        assert reply["done"] is True
        assert "already_completed" in (reply.get("reason") or "")
        events_after = len(_tape_events(daemon.tape_path))
        assert events_after == events_before, "终态后再 next 不得 emit"
    finally:
        daemon.cleanup()


def test_daemon_observe_none_on_running_node_is_noop(wf_plain, tmp_path):
    """运行中节点 observe(None) → next → advance_step branch 4（idempotent replay, emits=[]）。

    ``apply_step_result`` 对 ``emits=[]`` 调 ``emit_batch([])``（bus no-op）——本测试守门
    「空批不写 tape」+ 「重发同一 pending 节点 prompt」（code-reviewer Round 2 m2：daemon 侧
    非终态幂等 replay 路径补测，验 5b 新 helper 的空 emits 处理）。
    """
    daemon = _make_daemon(wf_plain, tmp_path)
    try:
        reply0 = _next(daemon)  # bootstrap → entry a 起来
        assert reply0["node"] == "a"

        # observe(None) → next 无 output → branch 4 idempotent replay（emits=[]）
        daemon.observe(None)
        events_before = len(_tape_events(daemon.tape_path))
        reply = _next(daemon)
        assert reply["done"] is False
        assert reply["node"] == "a"  # 重发同一 pending 节点
        assert reply["prompt"]  # prompt 仍在
        # tape 不增（emit_batch([]) no-op）
        assert len(_tape_events(daemon.tape_path)) == events_before, (
            "idempotent replay（emits=[]）不得写 tape"
        )
    finally:
        daemon.cleanup()
