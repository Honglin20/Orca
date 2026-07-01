"""tests/run/test_parallel.py —— 并行组执行（SPEC §4.4 / 计划 R4.4）。

覆盖：
  - branches 并行执行（asyncio.gather，所有完成才聚合）
  - 幂等：branch 已在 ctx.outputs → 跳过不重复执行
  - failure_mode fail_fast：首个失败抛
  - failure_mode continue_on_error：部分成功不抛，仅全失败抛
  - failure_mode all_or_nothing：任一失败抛
  - 聚合 {outputs: {branch: raw}, errors: {}, count, succeeded}

策略：monkeypatch ``orca.exec.factory.make_executor`` 注入 FakeExecutor（确定 output，
不 spawn）。直接调 ``run_parallel_group``（不走 orchestrator 主循环，隔离单元）。
"""

from __future__ import annotations

from typing import Any

import pytest

from orca.exec.context import RunContext
from orca.run.aggregate import GroupFailure
from orca.run.parallel import run_parallel_group
from orca.schema import ParallelGroup, Route, ScriptNode, Workflow
from tests.run.conftest import FakeExecutor, make_bus, run_async


def _ctx(outputs=None) -> RunContext:
    return RunContext(inputs={}, outputs=outputs or {}, run_id="r1")


def _wf(branches: list[str], failure_mode: str = "fail_fast") -> Workflow:
    """branches 是 node 名列表；nodes 为每个 branch 建一个 ScriptNode 占位。"""
    return Workflow(
        name="p_test",
        entry="__unused__",
        nodes=[ScriptNode(name=b, command="echo", routes=[Route(to="$end")]) for b in branches]
        + [ScriptNode(name="__unused__", command="echo", routes=[])],
        parallel=[
            ParallelGroup(
                name="grp",
                branches=branches,
                failure_mode=failure_mode,  # type: ignore[arg-type]
                routes=[Route(to="$end")],
            )
        ],
    )


def _patch_executors(monkeypatch, branch_to_executor: dict[str, FakeExecutor]):
    """monkeypatch make_executor：按 node.name 分派到预设 FakeExecutor。"""

    def fake(node, agent_tools_server=None):
        return branch_to_executor.get(node.name, FakeExecutor.produces({"default": True}, node_name=node.name))

    monkeypatch.setattr("orca.exec.factory.make_executor", fake)


# ── 并行 + 聚合 ───────────────────────────────────────────────────────────────


def test_parallel_gather_all_branches(tmp_path, monkeypatch):
    """两 branch 都产出 → 聚合 outputs 含两者 raw output。"""
    bus, _ = make_bus(tmp_path)
    _patch_executors(monkeypatch, {
        "a": FakeExecutor.produces({"v": 1}, node_name="a"),
        "b": FakeExecutor.produces({"v": 2}, node_name="b"),
    })
    group = _wf(["a", "b"]).parallel[0]

    result = run_async(run_parallel_group(group, _ctx(), bus, _wf(["a", "b"])))

    # raw aggregated（orchestrator 外层会包 {"output": raw}）
    assert result["outputs"] == {"a": {"v": 1}, "b": {"v": 2}}
    assert result["count"] == 2
    assert result["succeeded"] == 2
    assert result["errors"] == {}


def test_parallel_three_branches(tmp_path, monkeypatch):
    """3 branch 并行，全成功。"""
    bus, _ = make_bus(tmp_path)
    _patch_executors(monkeypatch, {
        "x": FakeExecutor.produces({"r": "x"}, node_name="x"),
        "y": FakeExecutor.produces({"r": "y"}, node_name="y"),
        "z": FakeExecutor.produces({"r": "z"}, node_name="z"),
    })
    group = _wf(["x", "y", "z"]).parallel[0]
    result = run_async(run_parallel_group(group, _ctx(), bus, _wf(["x", "y", "z"])))
    assert set(result["outputs"].keys()) == {"x", "y", "z"}
    assert result["count"] == 3


# ── 幂等 ──────────────────────────────────────────────────────────────────────


def test_parallel_idempotent_skips_executed_branch(tmp_path, monkeypatch):
    """branch 已在 ctx.outputs（如作为 entry 跑过）→ 跳过，不重复执行。"""
    bus, _ = make_bus(tmp_path)
    call_count = {"n": 0}

    class CountingFake(FakeExecutor):
        async def exec(self, node, ctx):
            call_count["n"] += 1
            async for e in super().exec(node, ctx):
                yield e

    _patch_executors(monkeypatch, {
        "a": CountingFake.produces({"v": 1}, node_name="a"),
        "b": CountingFake.produces({"v": 2}, node_name="b"),
    })
    # ctx.outputs 已含 a（模拟 a 作为 entry 已跑）
    ctx = _ctx(outputs={"a": {"output": {"v": 1}}})
    group = _wf(["a", "b"]).parallel[0]

    result = run_async(run_parallel_group(group, ctx, bus, _wf(["a", "b"])))

    assert result["outputs"]["a"] == {"v": 1}  # 沿用已有结果
    assert result["outputs"]["b"] == {"v": 2}  # b 正常执行
    # a 没被重新执行（只有 b 触发了 executor.exec）
    assert call_count["n"] == 1


# ── failure_mode 三态 ─────────────────────────────────────────────────────────


def test_parallel_fail_fast_raises_on_first_failure(tmp_path, monkeypatch):
    """fail_fast：任一失败 → GroupFailure（不等其余也抛，gather 已等全）。"""
    bus, _ = make_bus(tmp_path)
    _patch_executors(monkeypatch, {
        "a": FakeExecutor.produces({"v": 1}, node_name="a"),
        "b": FakeExecutor.failing(error_type="ExecTimeout", message="b 挂了", node_name="b"),
    })
    group = _wf(["a", "b"], failure_mode="fail_fast").parallel[0]

    with pytest.raises(GroupFailure, match="b"):
        run_async(run_parallel_group(group, _ctx(), bus, _wf(["a", "b"])))


def test_parallel_continue_on_error_partial_success(tmp_path, monkeypatch):
    """continue_on_error + 部分成功 → 不抛，聚合 errors。"""
    bus, _ = make_bus(tmp_path)
    _patch_executors(monkeypatch, {
        "a": FakeExecutor.produces({"v": 1}, node_name="a"),
        "b": FakeExecutor.failing(error_type="ExecTimeout", message="b 挂了", node_name="b"),
    })
    group = _wf(["a", "b"], failure_mode="continue_on_error").parallel[0]

    result = run_async(run_parallel_group(group, _ctx(), bus, _wf(["a", "b"])))

    assert result["succeeded"] == 1
    assert "a" in result["outputs"]
    assert "b" in result["errors"]
    assert "b 挂了" in result["errors"]["b"]


def test_parallel_continue_on_error_all_fail_raises(tmp_path, monkeypatch):
    """continue_on_error + 全失败 → 抛。"""
    bus, _ = make_bus(tmp_path)
    _patch_executors(monkeypatch, {
        "a": FakeExecutor.failing(node_name="a"),
        "b": FakeExecutor.failing(node_name="b"),
    })
    group = _wf(["a", "b"], failure_mode="continue_on_error").parallel[0]

    with pytest.raises(GroupFailure):
        run_async(run_parallel_group(group, _ctx(), bus, _wf(["a", "b"])))


def test_parallel_all_or_nothing_raises_on_any_failure(tmp_path, monkeypatch):
    """all_or_nothing：任一失败即抛（全或无）。"""
    bus, _ = make_bus(tmp_path)
    _patch_executors(monkeypatch, {
        "a": FakeExecutor.produces({"v": 1}, node_name="a"),
        "b": FakeExecutor.failing(node_name="b"),
    })
    group = _wf(["a", "b"], failure_mode="all_or_nothing").parallel[0]

    with pytest.raises(GroupFailure):
        run_async(run_parallel_group(group, _ctx(), bus, _wf(["a", "b"])))


# ── GroupFailure 携带诊断 ─────────────────────────────────────────────────────


def test_group_failure_carries_group_and_key(tmp_path, monkeypatch):
    """GroupFailure 携带 group_name + key（诊断用）。"""
    bus, _ = make_bus(tmp_path)
    _patch_executors(monkeypatch, {
        "a": FakeExecutor.failing(node_name="a"),
    })
    group = ParallelGroup(
        name="mygroup", branches=["a", "b"],
        failure_mode="fail_fast", routes=[],
    )
    wf = _wf(["a", "b"])
    _patch_executors(monkeypatch, {
        "a": FakeExecutor.failing(node_name="a"),
        "b": FakeExecutor.produces({"v": 2}, node_name="b"),
    })

    with pytest.raises(GroupFailure) as ei:
        run_async(run_parallel_group(group, _ctx(), bus, wf))
    assert ei.value.group_name == "mygroup"
