"""tests/run/test_router.py —— Router 纯函数求值（SPEC §3 / 计划 R1.3）。

覆盖：
  - first-match-wins（顺序敏感）
  - ``when=None`` 兜底匹配
  - 全部不匹配 → RouteError（fail loud）
  - 变量解析：``output`` / ``inputs`` / ``node_name.output.field``
  - 纯函数性：同输入两次调用结果相同
  - Jinja2 语法错 / 未定义变量 → RouteError（fail loud）

phase-14：``resolve`` 返回命中的 ``Route`` 对象（非 target str）。本文件用 ``_target``
helper 取 ``.to`` 保持断言简洁；直测 RouteError 的用例仍直接调 ``resolve``。
"""

from __future__ import annotations

import pytest

from orca.exec.context import RunContext
from orca.run.router import RouteError, resolve
from orca.schema import Route


def _ctx(inputs=None, outputs=None) -> RunContext:
    """outputs 已含 ``{"output": raw}`` 包装（与 orchestrator 存储形状一致）。"""
    return RunContext(
        inputs=inputs or {},
        outputs=outputs or {},
        run_id="r1",
    )


def _target(routes, output, ctx) -> str:
    """phase-14：resolve 返回 Route，本 helper 取 .to（断言 target 用）。"""
    return resolve(routes, output, ctx).to


# ── first-match-wins / 兜底 ──────────────────────────────────────────────────


def test_resolve_picks_first_matching_when():
    """output.x > 0 → A（首个命中即返回，不评估后续）。"""
    routes = [
        Route(when="output.x > 0", to="A"),
        Route(to="B"),  # 兜底
    ]
    assert _target(routes, {"x": 5}, _ctx()) == "A"


def test_resolve_falls_back_to_catchall():
    """output.x = -1，首条 when 不命中 → 兜底 B。"""
    routes = [
        Route(when="output.x > 0", to="A"),
        Route(to="B"),
    ]
    assert _target(routes, {"x": -1}, _ctx()) == "B"


def test_resolve_no_match_no_catchall_raises_route_error():
    """全不匹配且无兜底 → RouteError（fail loud，SPEC §3.4）。"""
    routes = [Route(when="output.x > 0", to="A")]
    with pytest.raises(RouteError, match="无 route 匹配"):
        resolve(routes, {"x": -1}, _ctx())


def test_resolve_when_none_always_matches_first():
    """when=None 总命中，即便其前 / 后还有别的 route（first-match-wins）。"""
    routes = [
        Route(to="DEFAULT"),  # when=None 在首位，无条件命中
        Route(when="output.x > 0", to="A"),  # 这条永远不可达（compile 会拦，运行时也不该到这）
    ]
    assert _target(routes, {"x": 999}, _ctx()) == "DEFAULT"


def test_resolve_returns_end_target():
    """路由到 $end（终止信号）正确返回。"""
    routes = [Route(to="$end")]
    assert _target(routes, {}, _ctx()) == "$end"


def test_resolve_returns_route_object_with_output():
    """phase-14：resolve 返回 Route 对象（含 output），调用方可取 .to / .output。"""
    routes = [Route(to="$end", output={"summary": "{{ a.output }}"})]
    route = resolve(routes, {}, _ctx())
    assert isinstance(route, Route)
    assert route.to == "$end"
    assert route.output == {"summary": "{{ a.output }}"}


# ── 变量解析 ──────────────────────────────────────────────────────────────────


def test_resolve_reads_output_var():
    """output.exit_code 直接取本节点 raw output（SPEC §3.2）。"""
    routes = [
        Route(when="output.exit_code == 0", to="ok"),
        Route(to="fail"),
    ]
    assert _target(routes, {"exit_code": 0}, _ctx()) == "ok"
    assert _target(routes, {"exit_code": 1}, _ctx()) == "fail"


def test_resolve_reads_inputs_var():
    """inputs.iterations 取 workflow 输入。"""
    routes = [
        Route(when="inputs.iterations >= 5", to="many"),
        Route(to="few"),
    ]
    assert _target(routes, {}, _ctx(inputs={"iterations": 10})) == "many"
    assert _target(routes, {}, _ctx(inputs={"iterations": 1})) == "few"


def test_resolve_reads_other_node_output():
    """{{ decide.output.path }} 从 ctx.outputs['decide'] 取（{'output': raw} 包装）。"""
    routes = [
        Route(when="decide.output.path == 'high'", to="high"),
        Route(to="low"),
    ]
    ctx = _ctx(outputs={"decide": {"output": {"path": "high"}}})
    assert _target(routes, {}, ctx) == "high"
    ctx2 = _ctx(outputs={"decide": {"output": {"path": "low"}}})
    assert _target(routes, {}, ctx2) == "low"


# ── 纯函数性 ──────────────────────────────────────────────────────────────────


def test_resolve_is_pure_same_input_same_output():
    """同输入两次调用结果相同（铁律 5）。"""
    routes = [
        Route(when="output.n >= 3", to="done"),
        Route(to="loop"),
    ]
    ctx = _ctx()
    out = {"n": 5}
    first = resolve(routes, out, ctx)
    second = resolve(routes, out, ctx)
    assert first.to == second.to == "done"
    # ctx 未被 mutate（纯函数）
    assert ctx.outputs == {}


# ── fail loud（语法 / 未定义变量）────────────────────────────────────────────


def test_resolve_jinja2_syntax_error_raises_route_error():
    """when 表达式语法错 → RouteError（fail loud，不静默吞）。"""
    routes = [Route(when="output.x >>>", to="A")]
    with pytest.raises(RouteError, match="求值失败"):
        resolve(routes, {"x": 1}, _ctx())


def test_resolve_undefined_variable_raises_route_error():
    """when 引用未定义变量 → RouteError（StrictUndefined fail loud）。"""
    routes = [Route(when="nonexistent_field == 1", to="A")]
    with pytest.raises(RouteError, match="求值失败"):
        resolve(routes, {}, _ctx())


# ── RouteError payload ────────────────────────────────────────────────────────


def test_route_error_carries_output_for_diagnostics():
    """RouteError 携带 output（诊断用，可拼进 workflow_failed message）。"""
    routes = [Route(when="output.x > 0", to="A")]
    try:
        resolve(routes, {"x": -1}, _ctx())
    except RouteError as e:
        assert e.output == {"x": -1}


# ── _truthy 边界（字符串值恰好是 falsy 字面量）────────────────────────────────


def test_resolve_truthy_string_falsy_literals():
    """渲染结果为 'false' / 'none' / '0' / '[]' → 判假（避免「非空即真」误判）。

    意图：when 表达式如 ``output.flag`` 当 flag='' 渲染成空串 → 假；flag='false'（字面
    串）也判假。引导用户用 ``| int`` / ``| bool`` 显式强转做精确布尔判定。
    """
    routes = [
        Route(when="output.flag", to="truthy"),
        Route(to="falsy"),
    ]
    # 'false' / 'none' / '0' / '[]' / '{}' / 'null' / 空串 → 假
    for falsy in ("false", "none", "0", "[]", "{}", "null", ""):
        assert _target(routes, {"flag": falsy}, _ctx()) == "falsy", falsy
    # 非空非 falsy 字面量 → 真
    for truthy in ("yes", "true", "1 ", "abc"):
        assert _target(routes, {"flag": truthy}, _ctx()) == "truthy", truthy
