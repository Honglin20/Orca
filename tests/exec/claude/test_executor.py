"""tests/exec/claude/test_executor.py —— ClaudeExecutor 端到端事件流 + 错误映射（SPEC §7.3 / §7.6 / 计划 C.7）。

策略（SPEC §12）：mock CLIRunner.stream 逐行 yield fixture 行（或构造的错误行），
断言产出完整事件流。**不 spawn claude**（纯函数可测性）。

共享设施来自 ``tests/exec/conftest.py``（FakeRunner / patch_runner_with_lines / run_async /
full_stream_lines / _reset_profiles_registry autouse），避免与 test_e2e.py 重复（DRY）。

覆盖：
  - 完整事件流：node_started → thinking* → tool_call → tool_result → agent_usage → node_completed
  - output_schema=None → node_completed.output = result 原文
  - output_schema 非空 → 提取 + 校验后的 dict
  - timeout 路径 → node_failed(phase=timeout) + error
  - spawn 路径（exit_code!=0）→ node_failed(phase=spawn)
  - stream 路径（result.is_error=true）→ node_failed(phase=stream)
  - result_parse 路径（无 result 行）→ node_failed(phase=result_parse)
  - schema 路径（result 非 JSON + schema 非空）→ node_failed(phase=schema)
  - render 路径（prompt 渲染失败）→ node_failed(phase=render)
  - 多 result 行：最后一个 result 生效（on_result 覆盖语义）
  - session_id 一致（铁律 5）
  - 不写 tape（无 EventBus/Tape 依赖）
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from orca.exec.claude.executor import ClaudeExecutor
from orca.exec.context import RunContext
from orca.profiles import get_profile
from orca.schema import AgentNode, Event

# 共享 autouse fixture（_reset_profiles_registry / full_stream_lines）来自 conftest.py，
# pytest 自动发现。run_async / FakeRunner / patch helper 在本文件就地定义
#（tests 非包，跨目录 import helper 不可行；轻微重复可接受）。


def run_async(coro):
    """统一异步入口（asyncio.run，本仓库约定）。"""
    return asyncio.run(coro)


class FakeRunner:
    """CLIRunner 替身：按预设行 yield，暴露 timed_out/exit_code/elapsed/stderr。

    检测到 result 行时回调 on_result（含 is_error + api_error_status 透传，SPEC §2.4 /
    §6 可观测性需要）。与 test_e2e.py 的 FakeRunner 同构（DRY 受限于 tests 非包，故就地复制）。
    与真实 CLIRunner 的 OnResult 5 参签名保持一致。
    """

    def __init__(
        self,
        lines=None,
        *,
        exit_code: int = 0,
        timed_out: bool = False,
        elapsed: float = 0.5,
        stderr: str = "",
    ) -> None:
        self._lines = list(lines) if lines is not None else []
        self._on_result = None
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.elapsed = elapsed
        self.stderr = stderr
        # phase 11 §4.2：默认未被用户 SIGINT 中断（ClaudeExecutor 据此区分中断 vs 崩溃）。
        self.was_interrupted = False

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
        if not isinstance(obj, dict) or obj.get("type") != "result":
            return
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


@pytest.fixture
def profile():
    """claude builtin profile（含真 translator）。"""
    return get_profile("claude")


@pytest.fixture
def executor(profile):
    return ClaudeExecutor(profile)


async def _collect(node, executor, ctx) -> list[Event]:
    return [ev async for ev in executor.exec(node, ctx)]


# ── 完整事件流（fixture 驱动）────────────────────────────────────────────────


def test_full_event_stream_happy_path(executor, full_stream_lines, monkeypatch):
    """完整 fixture 流 → node_started → 流式 → tool_call → tool_result → usage → completed。

    output_schema=None → node_completed.output = result 原文 "DONE"（SPEC §7.3）。
    """
    patch_runner_with_lines(monkeypatch, full_stream_lines, exit_code=0, elapsed=24.7)

    node = AgentNode(name="worker", prompt="echo DONE")
    ctx = RunContext(inputs={}, outputs={}, run_id="r1")

    events = run_async(_collect(node, executor, ctx))
    types = [ev.type for ev in events]

    # 生命周期：node_started ... node_completed
    assert types[0] == "node_started"
    assert types[-1] == "node_completed"
    # 流式事件齐全（fixture 的 bash 调用流）
    assert "agent_thinking" in types
    assert "agent_tool_call" in types
    assert "agent_tool_result" in types
    assert "agent_usage" in types
    # node_completed.output = result 原文（output_schema=None）
    completed = events[-1]
    assert completed.data["output"] == "DONE"
    assert completed.data["elapsed"] == pytest.approx(24.7)


def test_output_schema_none_keeps_raw_text(executor, monkeypatch):
    """output_schema=None → output 是 result 原文（自由文本，SPEC §2.7）。"""
    lines = [
        json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                  "delta": {"type": "text_delta", "text": "hello"}}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "free text answer", "total_cost_usd": 0.01,
                  "usage": {"input_tokens": 10, "output_tokens": 2}}),
    ]
    patch_runner_with_lines(monkeypatch, lines, exit_code=0)

    node = AgentNode(name="a", prompt="p", output_schema=None)
    events = run_async(_collect(node, executor, RunContext({}, {}, "r1")))
    assert events[-1].type == "node_completed"
    assert events[-1].data["output"] == "free text answer"


def test_output_schema_non_null_extracts_and_validates(executor, monkeypatch):
    """output_schema 非空 → output 是提取 + 校验后的 dict（SPEC §2.7）。"""
    result_obj = {"answer": 42, "note": "hi"}
    lines = [
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": json.dumps(result_obj), "total_cost_usd": 0.0,
                  "usage": {"input_tokens": 5, "output_tokens": 1}}),
    ]
    patch_runner_with_lines(monkeypatch, lines, exit_code=0)

    schema = {"type": "object", "required": ["answer"]}
    node = AgentNode(name="a", prompt="p", output_schema=schema)
    events = run_async(_collect(node, executor, RunContext({}, {}, "r1")))
    assert events[-1].type == "node_completed"
    assert events[-1].data["output"] == result_obj


def test_node_completed_carries_usage_when_present(executor, monkeypatch):
    """on_result 收到 usage → node_completed.data 带 usage（供 orchestrator 聚合）。"""
    lines = [
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "x", "total_cost_usd": 0.5,
                  "usage": {"input_tokens": 100, "output_tokens": 20,
                            "cache_read_input_tokens": 50}}),
    ]
    patch_runner_with_lines(monkeypatch, lines, exit_code=0)
    node = AgentNode(name="a", prompt="p")
    events = run_async(_collect(node, executor, RunContext({}, {}, "r1")))
    completed = events[-1]
    assert completed.data["usage"]["input_tokens"] == 100
    assert completed.data["usage"]["cache_tokens"] == 50
    assert completed.data["usage"]["cost_usd"] == pytest.approx(0.5)


def test_multiple_result_lines_last_wins(executor, monkeypatch):
    """多 result 行：最后一个 result 生效（on_result 覆盖语义，pin 契约）。

    场景：claude retry 后发第二个 result。result_holder 被后发者覆盖，最终 output 用后者。
    """
    lines = [
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "first", "total_cost_usd": 0.1, "usage": {"input_tokens": 1}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "second-final", "total_cost_usd": 0.2, "usage": {"input_tokens": 2}}),
    ]
    patch_runner_with_lines(monkeypatch, lines, exit_code=0)
    node = AgentNode(name="a", prompt="p", output_schema=None)
    events = run_async(_collect(node, executor, RunContext({}, {}, "r1")))
    assert events[-1].data["output"] == "second-final"
    # usage 也用最后一个 result（input_tokens=2）
    assert events[-1].data["usage"]["input_tokens"] == 2


# ── 错误路径（6 类，SPEC §7.6 / §2.4 有序互斥）──────────────────────────────


def _failed_event(events: list[Event]) -> Event:
    """取 node_failed 事件。"""
    failed = [e for e in events if e.type == "node_failed"]
    assert len(failed) == 1, f"应恰好一个 node_failed，got {len(failed)}"
    return failed[0]


def test_error_path_timeout(executor, monkeypatch):
    """timed_out=True → node_failed(phase=timeout) + error（SPEC §7.6）。"""
    patch_runner_with_lines(monkeypatch, [], timed_out=True, exit_code=-1, elapsed=30.0)
    node = AgentNode(name="a", prompt="p")
    events = run_async(_collect(node, executor, RunContext({}, {}, "r1")))
    failed = _failed_event(events)
    assert failed.data["phase"] == "timeout"
    assert failed.data["error_type"] == "ExecTimeout"
    # error 事件也发（双发，SPEC §6）
    assert any(e.type == "error" and e.data["phase"] == "timeout" for e in events)


def test_error_path_spawn_nonzero_exit(executor, monkeypatch):
    """exit_code!=0 → node_failed(phase=spawn)（SPEC §7.6）。"""
    patch_runner_with_lines(monkeypatch, [], exit_code=2, stderr="boom")
    node = AgentNode(name="a", prompt="p")
    events = run_async(_collect(node, executor, RunContext({}, {}, "r1")))
    failed = _failed_event(events)
    assert failed.data["phase"] == "spawn"
    assert failed.data["error_type"] == "CliExitNonZero"


def test_error_path_stream_is_error(executor, monkeypatch):
    """result.is_error=true → node_failed(phase=stream)（SPEC §7.6 / §2.4 第 3 项）。"""
    lines = [
        json.dumps({"type": "result", "subtype": "error", "is_error": True,
                  "result": "API overloaded", "total_cost_usd": 0.0, "usage": {}}),
    ]
    patch_runner_with_lines(monkeypatch, lines, exit_code=0)
    node = AgentNode(name="a", prompt="p")
    events = run_async(_collect(node, executor, RunContext({}, {}, "r1")))
    failed = _failed_event(events)
    assert failed.data["phase"] == "stream"
    assert failed.data["error_type"] == "ClaudeStreamError"


def test_error_path_message_carries_api_error_status(executor, monkeypatch):
    """Bug1：claude 把 API 错误写在 result 行（api_error_status / result 文本），不在 stderr。

    典型 529 早退场景：exit_code!=0 且 stderr 空，node_failed.message 仍须带 HTTP 错误码
    + 错误描述（否则用户看到「exit_code=1；stderr 末尾：」完全无信息）。
    """
    lines = [
        json.dumps({"type": "result", "subtype": "error", "is_error": True,
                  "api_error_status": 529, "result": "API Error: 529 overloaded",
                  "total_cost_usd": 0.0, "usage": {}}),
    ]
    patch_runner_with_lines(monkeypatch, lines, exit_code=1, stderr="")
    node = AgentNode(name="a", prompt="p")
    events = run_async(_collect(node, executor, RunContext({}, {}, "r1")))
    failed = _failed_event(events)
    assert failed.data["phase"] == "spawn"
    msg = failed.data["message"]
    assert "529" in msg           # HTTP 错误码可见（_result_diag 产出）
    assert "overloaded" in msg    # 错误描述可见


def test_error_path_stream_message_carries_api_error_status(executor, monkeypatch):
    """Bug1（stream 分支）：exit_code=0 + result.is_error=true + api_error_status →
    node_failed(phase=stream) 的 message 也带 HTTP 错误码（claude 重试到放弃、返回
    is_error result 的场景）。_result_diag 在 spawn / stream 分支共用，显式用例防回归。
    """
    lines = [
        json.dumps({"type": "result", "subtype": "error", "is_error": True,
                  "api_error_status": 529, "result": "API Error: 529 overloaded",
                  "total_cost_usd": 0.0, "usage": {}}),
    ]
    patch_runner_with_lines(monkeypatch, lines, exit_code=0)
    node = AgentNode(name="a", prompt="p")
    events = run_async(_collect(node, executor, RunContext({}, {}, "r1")))
    failed = _failed_event(events)
    assert failed.data["phase"] == "stream"
    assert "529" in failed.data["message"]


def test_error_path_result_parse_no_result(executor, monkeypatch):
    """exit 0 但流里无 result 行 → node_failed(phase=result_parse)（SPEC §7.6）。"""
    lines = [
        json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                  "delta": {"type": "text_delta", "text": "partial"}}}),
        # 没有 result 行就结束
    ]
    patch_runner_with_lines(monkeypatch, lines, exit_code=0)
    node = AgentNode(name="a", prompt="p")
    events = run_async(_collect(node, executor, RunContext({}, {}, "r1")))
    failed = _failed_event(events)
    assert failed.data["phase"] == "result_parse"
    assert failed.data["error_type"] == "NoResultEvent"


def test_error_path_schema_validation(executor, monkeypatch):
    """result 非 JSON + output_schema 非空 → node_failed(phase=schema)（SPEC §7.6）。"""
    lines = [
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "not json at all", "total_cost_usd": 0.0, "usage": {}}),
    ]
    patch_runner_with_lines(monkeypatch, lines, exit_code=0)
    node = AgentNode(name="a", prompt="p", output_schema={"type": "object"})
    events = run_async(_collect(node, executor, RunContext({}, {}, "r1")))
    failed = _failed_event(events)
    assert failed.data["phase"] == "schema"
    assert failed.data["error_type"] == "SchemaValidationError"


def test_error_path_render_failure(executor, monkeypatch):
    """prompt Jinja2 渲染失败（未定义变量）→ node_failed(phase=render)（SPEC §7.6）。"""
    patch_runner_with_lines(monkeypatch, [], exit_code=0)
    node = AgentNode(name="a", prompt="uses {{ undefined_thing }}")
    events = run_async(_collect(node, executor, RunContext({}, {}, "r1")))
    failed = _failed_event(events)
    assert failed.data["phase"] == "render"
    assert failed.data["error_type"] == "RenderError"


# ── 错误判定有序互斥（§2.4：timed_out 优先于 exit_code）──────────────────────


def test_timeout_precedes_nonzero_exit(executor, monkeypatch):
    """timed_out=True 且 exit_code!=0 → phase=timeout（§2.4 有序，timeout 优先）。"""
    patch_runner_with_lines(monkeypatch, [], timed_out=True, exit_code=137)
    node = AgentNode(name="a", prompt="p")
    events = run_async(_collect(node, executor, RunContext({}, {}, "r1")))
    assert _failed_event(events).data["phase"] == "timeout"


# ── session_id 一致性（铁律 5）───────────────────────────────────────────────


def test_session_id_consistent_across_events(executor, full_stream_lines, monkeypatch):
    """单次 exec 内所有 Event.session_id 一致（铁律 5）。"""
    patch_runner_with_lines(monkeypatch, full_stream_lines, exit_code=0)
    node = AgentNode(name="a", prompt="p")
    events = run_async(_collect(node, executor, RunContext({}, {}, "r1")))
    sids = {ev.session_id for ev in events}
    assert len(sids) == 1, f"session_id 应唯一，got {sids}"
    assert next(iter(sids)) is not None


def test_session_id_distinct_per_exec_call(executor, full_stream_lines, monkeypatch):
    """两次 exec() 调用 → 两个不同 session_id（SPEC §3.2 每次调用一个）。"""
    patch_runner_with_lines(monkeypatch, full_stream_lines, exit_code=0)
    node = AgentNode(name="a", prompt="p")
    ctx = RunContext({}, {}, "r1")
    e1 = run_async(_collect(node, executor, ctx))
    e2 = run_async(_collect(node, executor, ctx))
    sid1 = {ev.session_id for ev in e1}
    sid2 = {ev.session_id for ev in e2}
    assert sid1 != sid2


def test_node_field_enriched_on_translator_events(executor, full_stream_lines, monkeypatch):
    """translator 产的事件也带 node=node.name（executor 富化，SPEC §4.2）。"""
    patch_runner_with_lines(monkeypatch, full_stream_lines, exit_code=0)
    node = AgentNode(name="worker", prompt="p")
    events = run_async(_collect(node, executor, RunContext({}, {}, "r1")))
    # 所有事件（含 translator 产的 agent_*）node 都应是 worker
    for ev in events:
        assert ev.node == "worker", f"{ev.type} 的 node={ev.node!r}（应为 worker）"


# ── 不写 tape（铁律 2：executor 不依赖 events.bus/Tape）──────────────────────


def test_executor_does_not_import_events_bus():
    """静态判据：orca.exec.claude.executor 不 import events.bus / Tape（铁律 2）。"""
    import orca.exec.claude.executor as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert "from orca.events.bus" not in src
    assert "EventBus" not in src
    assert "Tape" not in src


# ── argv 构造（--model / --allowed-tools，SPEC §7.3）─────────────────────────


def test_spawn_config_includes_model_when_specified(executor, monkeypatch):
    """node.model 显式 → argv 含 --model <m>（SPEC §2.1 / §7.3）。"""
    captured: list = []
    fake = patch_runner_with_lines(monkeypatch, [], exit_code=0)

    def factory_capture(cfg, on_result=None):
        captured.append(cfg)
        fake._on_result = on_result
        return fake

    monkeypatch.setattr("orca.exec.claude.executor.CLIRunner", factory_capture)
    node = AgentNode(name="a", prompt="p", model="claude-haiku", tools=["Bash", "Read"])
    run_async(_collect(node, executor, RunContext({}, {}, "r1")))
    assert len(captured) == 1
    cfg = captured[0]
    assert "--model" in cfg.extra_args
    assert "claude-haiku" in cfg.extra_args
    # --allowed-tools 单 flag + 空格 join（非 variadic）
    assert "--allowed-tools" in cfg.extra_args
    idx = cfg.extra_args.index("--allowed-tools")
    assert cfg.extra_args[idx + 1] == "Bash Read"


def test_spawn_config_omits_model_and_tools_when_none(executor, monkeypatch):
    """node.model=None / tools=None → argv 不含 --model / --allowed-tools（SPEC §2.1）。"""
    captured: list = []
    fake = patch_runner_with_lines(monkeypatch, [], exit_code=0)

    def factory_capture(cfg, on_result=None):
        captured.append(cfg)
        fake._on_result = on_result
        return fake

    monkeypatch.setattr("orca.exec.claude.executor.CLIRunner", factory_capture)
    node = AgentNode(name="a", prompt="p")  # model=None, tools=None
    run_async(_collect(node, executor, RunContext({}, {}, "r1")))
    cfg = captured[0]
    assert "--model" not in cfg.extra_args
    assert "--allowed-tools" not in cfg.extra_args
