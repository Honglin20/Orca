"""tests/run/test_context.py —— RunContext 扩展验证（locals / task 字段，计划 R1.4）。

覆盖：
  - RunContext frozen
  - ``with_locals`` 返回新实例（不 mutate）
  - ``task`` 字段默认 None
  - ``locals`` 默认空 dict
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from orca.exec.context import RunContext


def _ctx(**kw) -> RunContext:
    base = {"inputs": {}, "outputs": {}, "run_id": "r1"}
    base.update(kw)
    return RunContext(**base)


def test_run_context_is_frozen():
    """frozen dataclass：直接赋值抛 FrozenInstanceError。"""
    ctx = _ctx()
    with pytest.raises(FrozenInstanceError):
        ctx.inputs = {"x": 1}  # type: ignore[misc]


def test_run_context_task_defaults_none():
    """task 字段默认 None（未传 task 时）。"""
    ctx = _ctx()
    assert ctx.task is None


def test_run_context_locals_defaults_empty_dict():
    """locals 默认空 dict（普通 node 上下文）。"""
    ctx = _ctx()
    assert ctx.locals == {}


def test_with_locals_returns_new_instance():
    """with_locals 派生新 frozen 实例，原 ctx 不被 mutate。"""
    ctx = _ctx()
    new_ctx = ctx.with_locals({"item": "x", "_index": 0})

    assert new_ctx is not ctx
    assert ctx.locals == {}  # 原 ctx 未变
    assert new_ctx.locals == {"item": "x", "_index": 0}


def test_with_locals_preserves_other_fields():
    """with_locals 保留 inputs / outputs / run_id / task。"""
    ctx = RunContext(
        inputs={"k": "v"},
        outputs={"node_a": {"output": 1}},
        run_id="run-xyz",
        task="做某事",
    )
    new_ctx = ctx.with_locals({"item": "y"})

    assert new_ctx.inputs == {"k": "v"}
    assert new_ctx.outputs == {"node_a": {"output": 1}}
    assert new_ctx.run_id == "run-xyz"
    assert new_ctx.task == "做某事"
    assert new_ctx.locals == {"item": "y"}


def test_with_locals_copies_dict_avoids_external_mutation():
    """with_locals 拷贝 locals dict，外部 mutate 不污染 frozen 快照。"""
    src = {"item": "a"}
    ctx = _ctx().with_locals(src)
    src["item"] = "b"  # 外部 mutate
    assert ctx.locals == {"item": "a"}  # frozen 实例不受影响
