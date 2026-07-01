"""tests/exec/test_e2e.py —— 端到端（编排视角，fixture 驱动，SPEC §7.10 / 计划 E.5）。

模拟 phase 5 orchestrator：``executor = make_executor(node); async for ev in executor.exec(node, ctx): ...``。
**不 spawn claude**（agent node 用 mock CLIRunner + fixture 流）。

覆盖：
  - agent node 完整生命周期（mock CLIRunner + 真实 fixture 流）
  - script/set node 串联：set 的 output 进 ctx.outputs，下个 script 能读到（节点间数据传递）
  - make_executor 对每种 kind 都能分派（factory 视角端到端）

共享设施来自 ``tests/exec/conftest.py``（patch_runner_with_lines / run_async /
full_stream_lines / _reset_profiles_registry autouse）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from orca.exec import make_executor
from orca.exec.context import RunContext
from orca.schema import AgentNode, Event, ScriptNode, SetNode

# 共享 autouse fixture（_reset_profiles_registry / full_stream_lines）来自 conftest.py，
# pytest 自动发现。run_async / FakeRunner / patch helper 在本文件就地定义
#（tests 非包，跨目录 import helper 不可行；与 test_executor.py 同构复制）。


def run_async(coro):
    """统一异步入口（asyncio.run，本仓库约定）。"""
    return asyncio.run(coro)


class FakeRunner:
    """CLIRunner 替身（与 test_executor.py 的 FakeRunner 同构，复制原因见上）。"""

    def __init__(self, lines=None, *, exit_code=0, timed_out=False, elapsed=1.0, stderr=""):
        self._lines = list(lines) if lines is not None else []
        self._on_result = None
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.elapsed = elapsed
        self.stderr = stderr
        # phase 11 §4.2：默认未被用户 SIGINT 中断。
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
        if isinstance(obj, dict) and obj.get("type") == "result":
            self._on_result(
                obj.get("result", ""),
                obj.get("usage") or {},
                obj.get("total_cost_usd") or 0.0,
                bool(obj.get("is_error", False)),
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


# ── agent node 完整生命周期（编排视角）──────────────────────────────────────


def test_agent_node_full_lifecycle_orchestrator_view(full_stream_lines, monkeypatch):
    """模拟 orchestrator：make_executor(agent) → exec → 收集事件。

    断言完整生命周期：node_started → 流式 → node_completed（SPEC §7.10）。
    """
    patch_runner_with_lines(monkeypatch, full_stream_lines, exit_code=0)
    node = AgentNode(name="worker", prompt="run bash then say DONE")
    ctx = RunContext(inputs={}, outputs={}, run_id="run-1")
    events = run_async(_exec_collect(node, ctx))

    assert events[0].type == "node_started"
    assert events[-1].type == "node_completed"
    sids = {ev.session_id for ev in events}
    assert len(sids) == 1
    assert all(ev.node == "worker" for ev in events)


def test_orchestrator_collects_tool_call_and_result(full_stream_lines, monkeypatch):
    """编排视角能看到 agent 的工具调用 + 结果（SPEC §7.10 端到端事件流断言）。"""
    patch_runner_with_lines(monkeypatch, full_stream_lines, exit_code=0)
    node = AgentNode(name="w", prompt="p")
    ctx = RunContext(inputs={}, outputs={}, run_id="r1")
    events = run_async(_exec_collect(node, ctx))

    tool_calls = [ev for ev in events if ev.type == "agent_tool_call"]
    tool_results = [ev for ev in events if ev.type == "agent_tool_result"]
    assert len(tool_calls) == 1
    assert tool_calls[0].data["tool"] == "Bash"
    assert len(tool_results) == 1
    assert tool_results[0].data["result"] == "PHASE_B_FIXTURE"


# ── 节点间数据传递：set → script 串联 ────────────────────────────────────────


def test_set_output_flows_into_downstream_script():
    """set 节点求值后，output 进 ctx.outputs，下游 script 的 command 能引用（SPEC §7.10）。

    模拟 orchestrator：执行 set → 取 output → 构造新 ctx → 执行 script 引用之。
    """
    ctx0 = RunContext(inputs={"name": "world"}, outputs={}, run_id="r1")
    set_node = SetNode(name="build_msg", values={"text": "hello-{{ inputs.name }}"})
    set_events = run_async(_exec_collect(set_node, ctx0))
    set_completed = [e for e in set_events if e.type == "node_completed"][0]
    set_output = set_completed.data["output"]
    assert set_output == {"text": "hello-world"}

    # orchestrator 把 set 的 output 累加进 outputs
    ctx1 = RunContext(inputs=ctx0.inputs, outputs={"build_msg": set_output}, run_id="r1")
    script_node = ScriptNode(name="print_msg", command="echo {{ build_msg.text }}")
    script_events = run_async(_exec_collect(script_node, ctx1))
    script_completed = [e for e in script_events if e.type == "node_completed"][0]
    assert script_completed.data["output"]["stdout"].strip() == "hello-world"


def test_three_kinds_chain_set_then_script_then_set():
    """三种叶子 kind 串联：set 求值 → script 引用 → set 再求值（编排视角）。"""
    ctx0 = RunContext(inputs={"n": "X"}, outputs={}, run_id="r1")
    set1 = SetNode(name="s1", values={"base": "{{ inputs.n }}"})
    e1 = run_async(_exec_collect(set1, ctx0))
    out1 = [e for e in e1 if e.type == "node_completed"][0].data["output"]

    ctx1 = RunContext(inputs=ctx0.inputs, outputs={"s1": out1}, run_id="r1")
    script_node = ScriptNode(name="sc", command="echo {{ s1.base }}")
    e2 = run_async(_exec_collect(script_node, ctx1))
    out2 = [e for e in e2 if e.type == "node_completed"][0].data["output"]
    assert out2["stdout"].strip() == "X"

    ctx2 = RunContext(inputs=ctx0.inputs, outputs={"s1": out1, "sc": out2}, run_id="r1")
    set2 = SetNode(name="s2", values={"captured": "{{ sc.stdout }}"})
    e3 = run_async(_exec_collect(set2, ctx2))
    out3 = [e for e in e3 if e.type == "node_completed"][0].data["output"]
    assert out3 == {"captured": "X\n"}  # echo 输出带尾部换行


# ── make_executor 端到端分派（每种 kind 返回正确 Executor 类型）─────────────


def test_make_executor_dispatches_all_three_kinds():
    """make_executor 对三种叶子 kind 都能分派（SPEC §7.8 端到端）。"""
    from orca.exec.claude.executor import ClaudeExecutor
    from orca.exec.script import ScriptExecutor
    from orca.exec.set_node import SetExecutor

    assert isinstance(make_executor(AgentNode(name="a")), ClaudeExecutor)
    assert isinstance(make_executor(ScriptNode(name="s", command="echo x")), ScriptExecutor)
    assert isinstance(make_executor(SetNode(name="st", values={"a": "1"})), SetExecutor)
