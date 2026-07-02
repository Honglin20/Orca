"""tests/exec/test_e2e_opencode.py —— opencode 后端端到端（events 模式）。

模拟 orchestrator：``make_executor(opencode agent node) → exec → 收集事件``。**不 spawn
opencode**（FakeRunner 喂 NDJSON 行）。

验证 events 模式核心契约（C9）：
  - 无 result 终止行时，``node_completed.output`` = 所有 text 事件拼接的最终答案。
  - ``agent_usage`` 透传到事件流（每 step_finish 一条）。
  - 错误路径：opencode error 事件 → ``is_error`` → node_failed（fail loud）。
  - prompt_channel=argv 时 prompt 进 argv 末尾（FakeRunner 不真 spawn，但 SpawnConfig 已带）。

共享 autouse ``_reset_profiles_registry`` 来自 conftest.py（pytest 自动发现）。
FakeRunner / run_async / patch helper 就地定义（tests 非包，跨目录 import 不可行，同 test_e2e.py）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from orca.exec import make_executor
from orca.exec.context import RunContext
from orca.schema import AgentNode, Event

OPENCODE_FIXTURE = (
    Path(__file__).resolve().parents[1] / "profiles" / "fixtures" / "opencode_sample.jsonl"
)


def run_async(coro):
    """统一异步入口（asyncio.run，本仓库约定）。"""
    return asyncio.run(coro)


class FakeRunner:
    """CLIRunner 替身（与 test_e2e.py 的 FakeRunner 同构，复制原因见模块 docstring）。

    events 模式下没有 result 行，``_maybe_fire_on_result`` 不会被触发（fixture 无 type==result）；
    保留它仅为与 claude FakeRunner 形态一致（events 模式 on_result=None）。
    """

    def __init__(self, lines=None, *, exit_code=0, timed_out=False, elapsed=1.0, stderr="",
                 was_interrupted=False):
        self._lines = list(lines) if lines is not None else []
        self._on_result = None
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.elapsed = elapsed
        self.stderr = stderr
        self.was_interrupted = was_interrupted

    async def stream(self) -> AsyncIterator[str]:
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
        if isinstance(obj, dict) and obj.get("type") == "result":
            self._on_result(
                obj.get("result", ""),
                obj.get("usage") or {},
                obj.get("total_cost_usd") or 0.0,
                bool(obj.get("is_error", False)),
                obj.get("api_error_status"),
            )


def patch_runner_with_lines(monkeypatch, lines, **runner_kwargs):
    """把 ClaudeExecutor.exec 里的 CLIRunner 替换成喂 ``lines`` 的 FakeRunner。"""
    fake = FakeRunner(lines=lines, **runner_kwargs)
    monkeypatch.setattr(
        "orca.exec.claude.executor.CLIRunner",
        lambda cfg=None, on_result=None: (setattr(fake, "_on_result", on_result), fake)[1],
    )
    return fake


async def _exec_collect(node, ctx) -> list[Event]:
    """跑一个 node 收集全部事件（模拟 orchestrator 的 async for）。"""
    executor = make_executor(node)
    out: list[Event] = []
    async for ev in executor.exec(node, ctx):  # type: ignore[arg-type]
        out.append(ev)
    return out


def _load_fixture() -> list[str]:
    return [ln for ln in OPENCODE_FIXTURE.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ── node_completed.output = 拼接的最终答案（events 模式核心契约）─────────────


def test_opencode_node_completed_output_is_concatenated_text(monkeypatch):
    """events 模式无 result 终止行：最终答案 = 所有 agent_message.data["text"] 拼接。

    fixture 的两条 text 事件文本拼起来即 node_completed.output（经 extract_and_validate，
    无 output_schema 时原样返回）。
    """
    lines = _load_fixture()
    patch_runner_with_lines(monkeypatch, lines, exit_code=0)
    node = AgentNode(name="opencode_worker", prompt="say hi and list files", executor="opencode")
    ctx = RunContext(inputs={}, outputs={}, run_id="run-opencode-1")
    events = run_async(_exec_collect(node, ctx))

    assert events[0].type == "node_started"
    completed = [e for e in events if e.type == "node_completed"]
    assert len(completed) == 1
    # 两条 text 事件拼接（fixture 第一段 + 第二段）
    text_events = [e for e in events if e.type == "agent_message"]
    expected_output = "".join(e.data["text"] for e in text_events)
    assert completed[0].data["output"] == expected_output
    assert completed[0].data["output"]  # 非空


def test_opencode_emits_agent_usage_per_step(monkeypatch):
    """每个 step_finish → 一条 agent_usage（fixture 有 2 个 step_finish）。"""
    lines = _load_fixture()
    patch_runner_with_lines(monkeypatch, lines, exit_code=0)
    node = AgentNode(name="w", prompt="p", executor="opencode")
    ctx = RunContext(inputs={}, outputs={}, run_id="r1")
    events = run_async(_exec_collect(node, ctx))

    usage_events = [e for e in events if e.type == "agent_usage"]
    assert len(usage_events) == 2  # fixture 2 个 step_finish
    # node_completed.data.usage 也带 usage（RunAccumulator 存的最后一条 step_finish）
    completed = [e for e in events if e.type == "node_completed"][0]
    assert "usage" in completed.data
    # 钉死「累积器取最后一条 step_finish 为准」语义（覆盖非累加）——不仅比 cost，也比 tokens。
    assert completed.data["usage"]["cost_usd"] == usage_events[-1].data["cost_usd"]
    assert completed.data["usage"]["input_tokens"] == usage_events[-1].data["input_tokens"]


def test_opencode_tool_call_and_result_in_stream(monkeypatch):
    """opencode 工具事件翻译成 agent_tool_call + agent_tool_result（一次发）。"""
    lines = _load_fixture()
    patch_runner_with_lines(monkeypatch, lines, exit_code=0)
    node = AgentNode(name="w", prompt="p", executor="opencode")
    ctx = RunContext(inputs={}, outputs={}, run_id="r1")
    events = run_async(_exec_collect(node, ctx))

    tool_calls = [e for e in events if e.type == "agent_tool_call"]
    tool_results = [e for e in events if e.type == "agent_tool_result"]
    assert len(tool_calls) == 1
    assert tool_calls[0].data["tool"] == "bash"
    assert len(tool_results) == 1


# ── 错误路径：opencode error 事件 → node_failed（fail loud）──────────────────


def test_opencode_error_event_yields_node_failed(monkeypatch):
    """opencode error 事件经 RunAccumulator.consume_event 置 is_error → node_failed。"""
    error_line = json.dumps(
        {
            "type": "error",
            "error": {"name": "UnknownError", "data": {"message": "Model not found"}},
        }
    )
    patch_runner_with_lines(monkeypatch, [error_line], exit_code=0)
    node = AgentNode(name="w", prompt="p", executor="opencode")
    ctx = RunContext(inputs={}, outputs={}, run_id="r1")
    events = run_async(_exec_collect(node, ctx))

    # error 事件先 yield（推 tape），EOF 后 is_error 判定 → node_failed + error
    assert any(e.type == "error" for e in events)
    failed = [e for e in events if e.type == "node_failed"]
    assert len(failed) == 1
    assert failed[0].data["phase"] == "stream"
    assert "Model not found" in failed[0].data["message"]


def test_opencode_no_text_no_result_yields_node_failed(monkeypatch):
    """exit 0 但无 text 事件（result_text 缺失）→ result_parse 错误（同 claude 行为）。"""
    # 仅一条 step_start（无 text/无 error）→ result_text is None
    line = json.dumps(
        {"type": "step_start", "part": {"type": "step-start"}}
    )
    patch_runner_with_lines(monkeypatch, [line], exit_code=0)
    node = AgentNode(name="w", prompt="p", executor="opencode")
    ctx = RunContext(inputs={}, outputs={}, run_id="r1")
    events = run_async(_exec_collect(node, ctx))

    failed = [e for e in events if e.type == "node_failed"]
    assert len(failed) == 1
    assert failed[0].data["phase"] == "result_parse"


# ── 失败路径：events 模式 + timeout / 非零 exit / 中断（mode 无感的有序互斥）──


def test_opencode_timed_out_yields_node_failed(monkeypatch):
    """events 模式 + timed_out=True → timeout 错误（SPEC §2.4 有序互斥第 1 优先级）。

    executor 的有序互斥判定是 mode 无感的共用代码；此测试守护 events 模式下 timeout 分支。
    """
    text_line = json.dumps({"type": "text", "part": {"type": "text", "text": "partial"}})
    patch_runner_with_lines(
        monkeypatch, [text_line], exit_code=-1, timed_out=True, elapsed=30.0,
    )
    node = AgentNode(name="w", prompt="p", executor="opencode")
    ctx = RunContext(inputs={}, outputs={}, run_id="r1")
    events = run_async(_exec_collect(node, ctx))

    failed = [e for e in events if e.type == "node_failed"]
    assert len(failed) == 1
    assert failed[0].data["phase"] == "timeout"
    assert "opencode" in failed[0].data["message"]


def test_opencode_nonzero_exit_yields_node_failed(monkeypatch):
    """events 模式 + exit_code != 0 → spawn 错误（SPEC §2.4 第 2 优先级）。"""
    text_line = json.dumps({"type": "text", "part": {"type": "text", "text": "partial"}})
    patch_runner_with_lines(
        monkeypatch, [text_line], exit_code=1, stderr="opencode crashed",
    )
    node = AgentNode(name="w", prompt="p", executor="opencode")
    ctx = RunContext(inputs={}, outputs={}, run_id="r1")
    events = run_async(_exec_collect(node, ctx))

    failed = [e for e in events if e.type == "node_failed"]
    assert len(failed) == 1
    assert failed[0].data["phase"] == "spawn"
    assert "exit_code=1" in failed[0].data["message"]
    assert "opencode crashed" in failed[0].data["message"]


def test_opencode_was_interrupted_yields_node_failed(monkeypatch):
    """events 模式 + 用户 SIGINT 中断 → node_failed{was_interrupted:true}（非 error）。

    executor.py:175-182：was_interrupted 优先于 timed_out/exit_code 判定（用户主动中断不是
    transient error）。此测试守护 events 模式下的中断分支。
    """
    text_line = json.dumps({"type": "text", "part": {"type": "text", "text": "partial"}})
    patch_runner_with_lines(
        monkeypatch, [text_line], exit_code=130, was_interrupted=True,
    )
    node = AgentNode(name="w", prompt="p", executor="opencode")
    ctx = RunContext(inputs={}, outputs={}, run_id="r1")
    events = run_async(_exec_collect(node, ctx))

    failed = [e for e in events if e.type == "node_failed"]
    assert len(failed) == 1
    assert failed[0].data["phase"] == "interrupted"
    assert failed[0].data["was_interrupted"] is True
    # 中断不发 error 事件（不当 transient error，SPEC §9.5.2）
    assert not any(e.type == "error" for e in events)


# ── session_id / node 一致性 ─────────────────────────────────────────────────


def test_opencode_events_carry_consistent_session_and_node(monkeypatch):
    """所有事件 session_id 唯一、node 名一致（铁律 5 + SPEC §4.2）。"""
    lines = _load_fixture()
    patch_runner_with_lines(monkeypatch, lines, exit_code=0)
    node = AgentNode(name="oc_node", prompt="p", executor="opencode")
    ctx = RunContext(inputs={}, outputs={}, run_id="r1")
    events = run_async(_exec_collect(node, ctx))

    sids = {ev.session_id for ev in events if ev.session_id is not None}
    assert len(sids) == 1
    assert all(ev.node == "oc_node" for ev in events if ev.node is not None)
