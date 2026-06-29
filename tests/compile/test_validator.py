"""tests/compile/test_validator.py —— 8 项语义校验各正/反例 + 聚合 + warnings。

直接调 validate_workflow（内部入口），逐项验证 SPEC §4 的 8 条规则。
测试覆盖意图（非仅行为）：每项校验对「正确工作流」放行、对「对应错误」精确报错；
errors 聚合（多处错一次报全）；warnings 不阻止返回。
"""

from __future__ import annotations

import pytest

from orca.compile import ConfigurationError, load_workflow
from orca.compile.validator import ValidationResult, validate_workflow
from orca.schema import Workflow


# ── helpers ──────────────────────────────────────────────────────────────────


def _wf(nodes: list, *, entry: str = "a", outputs: dict | None = None,
        inputs: dict | None = None) -> Workflow:
    """用 dict 构造 Workflow（贴近 YAML→dict→Workflow 真实路径）。"""
    return Workflow(
        name="w",
        entry=entry,
        nodes=nodes,
        outputs=outputs or {},
        inputs=inputs or {},
    )


def _agent(name: str, prompt: str = "p", **kw) -> dict:
    """单 agent 节点 dict。"""
    d = {"name": name, "kind": "agent", "prompt": prompt}
    d.update(kw)
    return d


def _errors(wf: Workflow) -> list[str]:
    """断言会抛，返回 errors 列表。"""
    with pytest.raises(ConfigurationError) as exc:
        validate_workflow(wf)
    return exc.value.errors


# ── 一个最小合法工作流：不应抛 ──


def test_minimal_valid_workflow():
    wf = _wf([_agent("a", routes=[{"to": "$end"}])])
    assert validate_workflow(wf) == []  # 无 warnings


# ── ① name 非空 + 全局唯一 ──


def test_name_duplicate():
    wf = _wf([
        _agent("a", routes=[{"to": "$end"}]),
        _agent("a", routes=[{"to": "$end"}]),
    ])
    errs = _errors(wf)
    assert any("重复" in e and "'a'" in e for e in errs)


def test_name_empty():
    wf = _wf([{"name": "", "kind": "agent", "prompt": "p", "routes": [{"to": "$end"}]}],
             entry="")
    # entry="" 也不在 names → 同时触发 ②；这里只断言 ① 的「空」错误在
    errs = _errors(wf)
    assert any("name" in e and ("空" in e or "缺少" in e) for e in errs)


# ── ② entry 存在 ──


def test_entry_missing():
    wf = _wf([_agent("a", routes=[{"to": "$end"}])], entry="ghost")
    errs = _errors(wf)
    assert any("entry" in e and "ghost" in e for e in errs)


# ── ③ after 引用有效 ──


def test_after_ref_missing():
    wf = _wf([_agent("a", after=["ghost"], routes=[{"to": "$end"}])])
    errs = _errors(wf)
    assert any("after" in e and "ghost" in e for e in errs)


def test_after_ref_valid():
    wf = _wf([
        _agent("a", routes=[{"to": "b"}]),
        _agent("b", after=["a"], routes=[{"to": "$end"}]),
    ])
    validate_workflow(wf)  # 不抛


# ── ④ routes[].to 引用有效 ──


def test_route_ref_missing():
    wf = _wf([_agent("a", routes=[{"to": "nowhere"}])])
    errs = _errors(wf)
    assert any("route" in e and "nowhere" in e for e in errs)


def test_route_to_end_marker_valid():
    wf = _wf([_agent("a", routes=[{"to": "$end"}])])
    validate_workflow(wf)  # $end 合法，不抛


# ── ⑤ after 静态边无环 ──


def test_after_cycle_detected():
    wf = _wf([
        _agent("a", after=["b"], routes=[{"to": "$end"}]),
        _agent("b", after=["a"], routes=[{"to": "$end"}]),
    ])
    errs = _errors(wf)
    assert any("环" in e and "→" in e for e in errs)


def test_route_backedge_is_not_after_cycle():
    """route 回指是合法循环，不算 after 环（SPEC §4⑤）。a⇄b 仅靠 route 连通且有 $end 出口。"""
    wf = _wf([
        _agent("a", routes=[{"when": "output.loop == true", "to": "b"}, {"to": "$end"}]),
        _agent("b", routes=[{"to": "a"}]),
    ])
    validate_workflow(wf)  # 不抛：route 环合法，且 a 有 $end 出口


# ── ⑥ entry 可达 $end ──


def test_dead_end_detected():
    """a→b→a 路由环无 $end 出口 → 死胡同 error。"""
    wf = _wf([
        _agent("a", routes=[{"to": "b"}]),
        _agent("b", routes=[{"to": "a"}]),
    ])
    errs = _errors(wf)
    assert any("$end" in e for e in errs)
    assert any("a" in e or "b" in e for e in errs)


def test_implicit_terminal_no_routes():
    """无 route 的 sink = 隐式终态（裁决 A）：单节点无 route 也合法。"""
    wf = _wf([_agent("a")])  # 无 routes
    validate_workflow(wf)


def test_orphan_node_is_warning_not_error():
    """从 entry 不可达的节点 = warning（不阻止返回）。"""
    wf = _wf([
        _agent("a", routes=[{"to": "$end"}]),
        _agent("orphan", routes=[{"to": "$end"}]),
    ])
    warnings = validate_workflow(wf)
    assert any("orphan" in w for w in warnings)


# ── ⑦ Jinja2 引用浅校验 ──


def test_jinja_ref_to_nonexistent_node():
    wf = _wf([_agent("a", prompt="use {{ ghost.output.x }}", routes=[{"to": "$end"}])])
    errs = _errors(wf)
    assert any("ghost" in e for e in errs)


def test_jinja_ref_to_existing_node_ok():
    wf = _wf([
        _agent("a", routes=[{"to": "b"}]),
        _agent("b", prompt="got {{ a.output }}", after=["a"], routes=[{"to": "$end"}]),
    ])
    validate_workflow(wf)


def test_jinja_undeclared_workflow_input_is_warning():
    wf = _wf(
        [_agent("a", prompt="n {{ workflow.input.missing }}", routes=[{"to": "$end"}])],
        inputs={},
    )
    warnings = validate_workflow(wf)
    assert any("missing" in w and "input" in w for w in warnings)


def test_jinja_declared_workflow_input_no_warning():
    wf = _wf(
        [_agent("a", prompt="n {{ workflow.input.its }}", routes=[{"to": "$end"}])],
        inputs={"its": {"type": "int"}},
    )
    assert validate_workflow(wf) == []


def test_jinja_workflow_input_subscription_form_undeclared():
    """``workflow.input['key']``（Getitem 写法）也能触发未声明 input warning。

    回归：jinja2 Getitem 索引字段是 .arg（非 .index），曾因此 AttributeError 崩溃。
    """
    wf = _wf(
        [_agent("a", prompt="n {{ workflow.input['missing'] }}", routes=[{"to": "$end"}])],
        inputs={},
    )
    warnings = validate_workflow(wf)
    assert any("missing" in w for w in warnings)


def test_jinja_workflow_input_subscription_form_declared():
    wf = _wf(
        [_agent("a", prompt="n {{ workflow.input['its'] }}", routes=[{"to": "$end"}])],
        inputs={"its": {"type": "int"}},
    )
    assert validate_workflow(wf) == []


def test_jinja_route_when_output_is_valid():
    """when 里 output 指当前 node 自身输出，合法。"""
    wf = _wf([_agent("a", routes=[{"when": "output.exit_code == 0", "to": "$end"}])])
    validate_workflow(wf)


def test_jinja_foreach_body_item_var_valid():
    """foreach body 里 item_var（candidate）合法，不被当未知 node。"""
    wf = _wf([
        _agent("f", routes=[{"to": "fe"}]),
        {
            "name": "fe", "kind": "foreach", "after": ["f"],
            "source": "f.output.items", "item_var": "candidate",
            "body": {"kind": "agent", "prompt": "eval {{ candidate }}"},
            "routes": [{"to": "$end"}],
        },
    ], entry="f")
    validate_workflow(wf)


def test_jinja_template_syntax_error_reported():
    """模板语法错 → 当校验错误报（fail loud），不静默。"""
    wf = _wf([_agent("a", prompt="bad {{ a. }}", routes=[{"to": "$end"}])])
    errs = _errors(wf)
    assert any("语法错误" in e for e in errs)


# ── ⑧ foreach.source node 存在 ──


def test_foreach_source_missing_node():
    wf = _wf([
        _agent("f", routes=[{"to": "$end"}]),
        {
            "name": "fe", "kind": "foreach", "after": ["f"],
            "source": "ghost.output.items",
            "body": {"kind": "agent", "prompt": "x"},
            "routes": [{"to": "$end"}],
        },
    ])
    errs = _errors(wf)
    assert any("source" in e and "ghost" in e for e in errs)


def test_foreach_source_existing_node_ok():
    wf = _wf([
        _agent("f", routes=[{"to": "fe"}]),
        {
            "name": "fe", "kind": "foreach", "after": ["f"],
            "source": "f.output.items",
            "body": {"kind": "agent", "prompt": "x"},
            "routes": [{"to": "$end"}],
        },
    ], entry="f")
    validate_workflow(wf)


# ── errors 聚合（SPEC §6.4）──


def test_errors_aggregated():
    """一处工作流多处独立错误 → ConfigurationError.errors 含全部，不止首个。"""
    wf = _wf(
        [_agent("a", prompt="use {{ nope.output.x }}", after=["ghost"],
                routes=[{"to": "nowhere"}])],
        entry="ghost_entry",
    )
    errs = _errors(wf)
    # 至少 4 个独立错误：entry 不存在 / after ghost / route nowhere / jinja nope
    assert len(errs) >= 4
    joined = " ".join(errs)
    assert "ghost_entry" in joined
    assert "ghost" in joined
    assert "nowhere" in joined
    assert "nope" in joined


# ── ValidationResult 行为（SPEC §1）──


def test_validation_result_raise_if_errors():
    r = ValidationResult()
    r.add_error("e1")
    r.add_warning("w1")
    with pytest.raises(ConfigurationError) as exc:
        r.raise_if_errors()
    assert exc.value.errors == ["e1"]
    assert exc.value.warnings == ["w1"]


def test_validation_result_returns_warnings_when_clean():
    r = ValidationResult()
    r.add_warning("w1")
    assert r.raise_if_errors() == ["w1"]


# ── fixture E2E：load_workflow(坏 yaml) → ConfigurationError（SPEC §6.6）──


@pytest.mark.parametrize(
    "fixture, keyword",
    [
        ("dup_name", "重复"),
        ("bad_entry", "entry"),
        ("bad_after", "after"),
        ("bad_route", "route"),
        ("after_cycle", "环"),
        ("dead_end", "$end"),
        ("bad_jinja", "ghost"),
        ("bad_foreach_source", "source"),
        ("multi_error", "ghost_entry"),
        ("structural_error", "结构校验"),
    ],
)
def test_fixture_rejected(fixtures_dir, fixture, keyword):
    with pytest.raises(ConfigurationError) as exc:
        load_workflow(fixtures_dir / f"{fixture}.yaml")
    joined = " ".join(exc.value.errors)
    assert keyword in joined, f"{fixture}: 期望含 '{keyword}'，实得 {exc.value.errors}"


def test_multi_error_fixture_aggregated(fixtures_dir):
    """multi_error.yaml 一处 YAML 含 4 个独立错 → 一次报全。"""
    with pytest.raises(ConfigurationError) as exc:
        load_workflow(fixtures_dir / "multi_error.yaml")
    assert len(exc.value.errors) >= 4
