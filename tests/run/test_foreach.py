"""tests/run/test_foreach.py —— 动态并行（SPEC §4.5 / 计划 R4.4）。

覆盖：
  - source 数组运行时取值（{{ maker.output.items }}）
  - body 收到 locals（item / _index）—— 用 FakeExecutor 验证 prompt 引用
  - Semaphore 限并发（max_concurrent=2，4 item，同时不超过 2）
  - failure_mode 三态
  - 聚合 {outputs: [...], errors: {idx: msg}, count, succeeded}

策略：monkeypatch ``orca.exec.factory.make_executor`` 注入 FakeExecutor（确定 output）。
直接调 ``run_foreach``（不走 orchestrator，隔离单元）。
"""

from __future__ import annotations

import asyncio

import pytest

from orca.exec.context import RunContext
from orca.run.aggregate import GroupFailure
from orca.run.foreach import run_foreach
from orca.schema import ForeachNode, Route, ScriptNode
from tests.run.conftest import FakeExecutor, make_bus, run_async


def _ctx(outputs=None) -> RunContext:
    return RunContext(inputs={}, outputs=outputs or {}, run_id="r1")


def _patch_executors(monkeypatch, executor_factory):
    """executor_factory(node) -> FakeExecutor。"""
    monkeypatch.setattr("orca.exec.factory.make_executor", executor_factory)


# ── source 运行时取值 + 聚合 ──────────────────────────────────────────────────


def test_foreach_source_resolved_at_runtime(tmp_path, monkeypatch):
    """source = {{ maker.output.items }} → 运行时取数组（[1,2,3]）。"""
    bus, _ = make_bus(tmp_path)
    seen_items: list = []

    def factory(node):
        # body 节点：用 FakeExecutor，但捕获收到的 ctx.locals[item]
        class CapturingFake(FakeExecutor):
            async def exec(self, n, ctx):
                seen_items.append(ctx.locals.get("item"))
                async for e in super().exec(n, ctx):
                    yield e
        return CapturingFake.produces({"doubled": True}, node_name=node.name)

    _patch_executors(monkeypatch, factory)

    node = ForeachNode(
        name="processor",
        source="maker.output.items",
        item_var="item",
        body=ScriptNode(name="body", command="echo {{ item }}", routes=[]),
        max_concurrent=10,
        failure_mode="fail_fast",
        routes=[Route(to="$end")],
    )
    ctx = _ctx(outputs={"maker": {"output": {"items": [1, 2, 3]}}})

    result = run_async(run_foreach(node, ctx, bus))

    # 3 个 item 都被 body 看到
    assert sorted(seen_items) == [1, 2, 3]
    assert result["count"] == 3
    assert result["succeeded"] == 3
    assert len(result["outputs"]) == 3
    assert all(o == {"doubled": True} for o in result["outputs"])


def test_foreach_empty_source_aggregates_zero(tmp_path, monkeypatch):
    """source = [] → count=0，无 body 执行。"""
    bus, _ = make_bus(tmp_path)
    _patch_executors(monkeypatch, lambda n: FakeExecutor.produces({}, node_name=n.name))

    node = ForeachNode(
        name="p", source="maker.output.items", item_var="item",
        body=ScriptNode(name="body", command="echo", routes=[]),
        routes=[Route(to="$end")],
    )
    ctx = _ctx(outputs={"maker": {"output": {"items": []}}})

    result = run_async(run_foreach(node, ctx, bus))
    assert result["count"] == 0
    assert result["outputs"] == []


def test_foreach_source_non_array_raises(tmp_path):
    """source 求值非数组 → ValueError（fail loud，SPEC §4.5）。"""
    bus, _ = make_bus(tmp_path)
    node = ForeachNode(
        name="p", source="maker.output.items", item_var="item",
        body=ScriptNode(name="body", command="echo", routes=[]),
        routes=[Route(to="$end")],
    )
    ctx = _ctx(outputs={"maker": {"output": {"items": "not-a-list"}}})

    with pytest.raises(ValueError, match="非数组"):
        run_async(run_foreach(node, ctx, bus))


# ── Semaphore 限并发 ──────────────────────────────────────────────────────────


def test_foreach_respects_max_concurrent(tmp_path, monkeypatch):
    """max_concurrent=2，4 item → 同时执行的不超过 2（Semaphore 限流）。"""
    bus, _ = make_bus(tmp_path)
    current_concurrent = {"n": 0}
    max_observed = {"n": 0}

    def factory(node):
        inner = FakeExecutor.produces({"ok": True}, node_name=node.name)

        class ThrottledFake(FakeExecutor):
            def __init__(self):
                super().__init__(inner._events, node_name=node.name)

            async def exec(self, n, ctx):
                current_concurrent["n"] += 1
                max_observed["n"] = max(max_observed["n"], current_concurrent["n"])
                await asyncio.sleep(0.05)  # 模拟耗时，让并发可见
                current_concurrent["n"] -= 1
                async for e in super().exec(n, ctx):
                    yield e

        return ThrottledFake()

    _patch_executors(monkeypatch, factory)

    node = ForeachNode(
        name="p", source="maker.output.items", item_var="item",
        body=ScriptNode(name="body", command="echo", routes=[]),
        max_concurrent=2,
        routes=[Route(to="$end")],
    )
    ctx = _ctx(outputs={"maker": {"output": {"items": [1, 2, 3, 4]}}})

    result = run_async(run_foreach(node, ctx, bus))

    assert result["count"] == 4
    assert max_observed["n"] <= 2  # 同时并发不超过 max_concurrent


# ── failure_mode 三态 ─────────────────────────────────────────────────────────


def _failing_for(indices: set[int], total: int):
    """工厂：指定 index 的 item 失败，其余成功。"""

    def factory(node):
        # 每次 make_executor 都返回一个独立 executor；按 call count 判定 index
        factory.calls = getattr(factory, "calls", -1) + 1
        idx = factory.calls
        if idx in indices:
            return FakeExecutor.failing(
                error_type="ExecTimeout", message=f"item {idx} 挂", node_name=node.name,
            )
        return FakeExecutor.produces({"ok": True}, node_name=node.name)

    factory.calls = -1
    return factory


def test_foreach_fail_fast_raises(tmp_path, monkeypatch):
    """fail_fast + 任一失败 → GroupFailure。"""
    bus, _ = make_bus(tmp_path)
    _patch_executors(monkeypatch, _failing_for({1}, 3))

    node = ForeachNode(
        name="p", source="maker.output.items", item_var="item",
        body=ScriptNode(name="body", command="echo", routes=[]),
        max_concurrent=1,  # 串行，保证 factory.calls 稳定映射 idx
        failure_mode="fail_fast",
        routes=[Route(to="$end")],
    )
    ctx = _ctx(outputs={"maker": {"output": {"items": [10, 20, 30]}}})

    with pytest.raises(GroupFailure, match="item 1"):
        run_async(run_foreach(node, ctx, bus))


def test_foreach_continue_on_error_partial(tmp_path, monkeypatch):
    """continue_on_error + 部分失败 → 不抛，errors 记录失败项。"""
    bus, _ = make_bus(tmp_path)
    _patch_executors(monkeypatch, _failing_for({1}, 3))

    node = ForeachNode(
        name="p", source="maker.output.items", item_var="item",
        body=ScriptNode(name="body", command="echo", routes=[]),
        max_concurrent=1,
        failure_mode="continue_on_error",
        routes=[Route(to="$end")],
    )
    ctx = _ctx(outputs={"maker": {"output": {"items": [10, 20, 30]}}})

    result = run_async(run_foreach(node, ctx, bus))
    assert result["count"] == 3
    assert result["succeeded"] == 2
    assert 1 in result["errors"]
    assert "item 1" in result["errors"][1]
    # outputs 长度仍 == total（失败项占位 None）
    assert len(result["outputs"]) == 3
    assert result["outputs"][0] == {"ok": True}
    assert result["outputs"][1] is None
    assert result["outputs"][2] == {"ok": True}


def test_foreach_all_or_nothing_raises(tmp_path, monkeypatch):
    """all_or_nothing + 任一失败 → GroupFailure。"""
    bus, _ = make_bus(tmp_path)
    _patch_executors(monkeypatch, _failing_for({0}, 3))

    node = ForeachNode(
        name="p", source="maker.output.items", item_var="item",
        body=ScriptNode(name="body", command="echo", routes=[]),
        max_concurrent=1,
        failure_mode="all_or_nothing",
        routes=[Route(to="$end")],
    )
    ctx = _ctx(outputs={"maker": {"output": {"items": [10, 20, 30]}}})

    with pytest.raises(GroupFailure):
        run_async(run_foreach(node, ctx, bus))


# ── item_var / index_var 自定义 ───────────────────────────────────────────────


def test_foreach_custom_item_and_index_var(tmp_path, monkeypatch):
    """item_var / index_var 自定义（非默认 item / _index）。"""
    bus, _ = make_bus(tmp_path)
    seen = []

    def factory(node):
        inner = FakeExecutor.produces({"ok": True}, node_name=node.name)

        class CapturingFake(FakeExecutor):
            def __init__(self):
                super().__init__(inner._events, node_name=node.name)

            async def exec(self, n, ctx):
                seen.append((ctx.locals.get("candidate"), ctx.locals.get("idx")))
                async for e in super().exec(n, ctx):
                    yield e

        return CapturingFake()

    _patch_executors(monkeypatch, factory)

    node = ForeachNode(
        name="p", source="maker.output.items",
        item_var="candidate", index_var="idx",
        body=ScriptNode(name="body", command="echo", routes=[]),
        max_concurrent=1,
        routes=[Route(to="$end")],
    )
    ctx = _ctx(outputs={"maker": {"output": {"items": ["a", "b"]}}})

    run_async(run_foreach(node, ctx, bus))
    assert seen == [("a", 0), ("b", 1)]
