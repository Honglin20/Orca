"""tests/run/test_lifecycle.py —— run_id + 生命周期事件 + max_iter 解析（计划 R2.2）。

覆盖：
  - gen_run_id 含 slug + 时间戳 + 6 字符 nanoid
  - 同名 workflow 两次 gen_run_id 不同
  - resolve_max_iter 优先级（cli_override > inputs > yaml default > 100）
  - workflow_started/completed/failed 的 (type, data) 形状正确
"""

from __future__ import annotations

import re

import pytest

from orca.run.lifecycle import (
    _DEFAULT_MAX_ITER,
    gen_run_id,
    make_workflow_completed,
    make_workflow_failed,
    make_workflow_started,
    resolve_max_iter,
)
from orca.schema import InputDef, Workflow


def _wf(
    name: str = "demo",
    entry: str = "a",
    nodes=None,
    inputs: dict | None = None,
) -> Workflow:
    from orca.schema import ScriptNode

    return Workflow(
        name=name,
        entry=entry,
        inputs=inputs or {},
        nodes=nodes or [ScriptNode(name="a", command="echo", routes=[])],
    )


# ── gen_run_id ────────────────────────────────────────────────────────────────


def test_gen_run_id_contains_slug_timestamp_and_nanoid():
    """格式：<slug>-<YYYYMMDD-HHMMSS>-<6 hex>。slug 保下划线（demo_linear → demo_linear）。"""
    rid = gen_run_id("my_workflow")
    # slug 小写、保下划线
    assert rid.startswith("my_workflow-")
    # 整体格式：slug-ts-nano（ts = 8digits-6digits，nano = 6 hex）
    assert re.match(r"^my_workflow-\d{8}-\d{6}-[0-9a-f]{6}$", rid), rid


def test_gen_run_id_strips_unsafe_chars():
    """非 [a-z0-9_] 字符转 '-'（空格 / 标点），首尾不留 '-'。"""
    rid = gen_run_id("My Cool Workflow!")
    # "my-cool-workflow-" → strip 尾 '-' → "my-cool-workflow"，split('-') 取首段 "my"
    # 但这里下划线保留、空格转 -，故 slug 段是 "my cool workflow!" 经处理后多段
    # 验证：大写转小写、空格/! 不在 slug 命名合法集内
    assert rid.startswith("my-cool-workflow-")
    assert "!" not in rid


def test_gen_run_id_unique_across_calls():
    """同 workflow 名两次 gen 必不同（时间戳/nanoid 保证）。"""
    a = gen_run_id("same")
    b = gen_run_id("same")
    assert a != b


def test_gen_run_id_empty_name_uses_run_slug():
    """空 workflow 名 → slug 退化到 'run'（不空字符串）。"""
    rid = gen_run_id("")
    assert rid.startswith("run-")


# ── make_workflow_started / completed / failed ───────────────────────────────


def test_make_workflow_started_payload():
    wf = _wf(name="demo", entry="start")
    inputs = {"x": 1}
    t, data = make_workflow_started("run-1", wf, inputs)
    assert t == "workflow_started"
    assert data["inputs"] == {"x": 1}
    assert data["entry"] == "start"
    assert data["workflow_name"] == "demo"
    assert data["node_count"] == 1


def test_make_workflow_completed_payload():
    t, data = make_workflow_completed(_wf(), {"result": "ok"}, elapsed=1.23)
    assert t == "workflow_completed"
    assert data["outputs"] == {"result": "ok"}
    assert data["elapsed"] == 1.23


def test_make_workflow_failed_payload_with_node():
    t, data = make_workflow_failed("NoRouteMatch", "no route", node="decide")
    assert t == "workflow_failed"
    assert data["error_type"] == "NoRouteMatch"
    assert data["message"] == "no route"
    assert data["node"] == "decide"


def test_make_workflow_failed_payload_workflow_level():
    """workflow 级失败（如 MaxIterations）node 可为 None。"""
    t, data = make_workflow_failed("MaxIterations", "exceeded", node=None)
    assert data["node"] is None


# ── resolve_max_iter ──────────────────────────────────────────────────────────


def test_resolve_max_iter_cli_override_wins():
    """--max-iter 最高优先（即便 inputs 有 iterations）。"""
    wf = _wf(inputs={"iterations": InputDef(type="int", default=50)})
    assert resolve_max_iter(wf, {"iterations": 10}, cli_override=5) == 5


def test_resolve_max_iter_inputs_iterations_next():
    """无 cli override，inputs.iterations 优先于 yaml default。"""
    wf = _wf(inputs={"iterations": InputDef(type="int", default=50)})
    assert resolve_max_iter(wf, {"iterations": 7}) == 7


def test_resolve_max_iter_yaml_default_next():
    """无 cli / inputs.iterations，用 yaml default。"""
    wf = _wf(inputs={"iterations": InputDef(type="int", default=42)})
    assert resolve_max_iter(wf, {}) == 42


def test_resolve_max_iter_global_fallback():
    """都无 → 全局兜底 100。"""
    wf = _wf(inputs={})
    assert resolve_max_iter(wf, {}) == _DEFAULT_MAX_ITER


def test_resolve_max_iter_default_is_100():
    assert _DEFAULT_MAX_ITER == 100


# ── resolve_max_iter fail loud（非法值不静默降级，铁律 4）────────────────────


def test_resolve_max_iter_illegal_inputs_iterations_raises():
    """``inputs["iterations"]`` 显式声明却非法（非数字）→ ValueError（不降级到 yaml default）。

    意图：用户传 ``-i iterations=abc`` 期待覆盖生效；若静默降级到 yaml default / 100，
    用户感知不到覆盖失效 —— 是隐性 bug。fail loud（铁律 4）。
    """
    wf = _wf(inputs={"iterations": InputDef(type="int", default=50)})
    with pytest.raises(ValueError):
        resolve_max_iter(wf, {"iterations": "abc"})


def test_resolve_max_iter_illegal_yaml_default_raises():
    """yaml ``inputs.iterations.default`` 非法 → ValueError（schema 声明却给坏值，是配置错误）。

    意图：yaml 作者把 default 写成 ``default: "lots"`` 是配置错误，应在解析期暴露而非
    静默退化到 100 让 workflow 跑飞。
    """
    wf = _wf(inputs={"iterations": InputDef(type="int", default="lots")})
    with pytest.raises(ValueError):
        resolve_max_iter(wf, {})


def test_resolve_max_iter_cli_override_non_int_raises():
    """programmatic API 传非 int cli_override → int() 自身 raise（不静默吞）。"""
    wf = _wf(inputs={})
    with pytest.raises((ValueError, TypeError)):
        resolve_max_iter(wf, {}, cli_override="fast")  # type: ignore[arg-type]
