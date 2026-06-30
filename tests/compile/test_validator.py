"""tests/compile/test_validator.py —— 9 项语义校验各正/反例 + 聚合 + warnings。

直接调 validate_workflow（内部入口），逐项验证 SPEC §4 的 9 条规则
（①②④⑥⑦⑧⑨⑩⑪⑬，③⑤ 随 after 废除）。
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
        inputs: dict | None = None, parallel: list | None = None) -> Workflow:
    """用 dict 构造 Workflow（贴近 YAML→dict→Workflow 真实路径）。"""
    return Workflow(
        name="w",
        entry=entry,
        nodes=nodes,
        parallel=parallel or [],
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


# ── ④ routes[].to 引用有效 ──


def test_route_ref_missing():
    wf = _wf([_agent("a", routes=[{"to": "nowhere"}])])
    errs = _errors(wf)
    assert any("route" in e and "nowhere" in e for e in errs)


def test_route_to_end_marker_valid():
    wf = _wf([_agent("a", routes=[{"to": "$end"}])])
    validate_workflow(wf)  # $end 合法，不抛


# ── ⑥ route 回指是合法循环（单轨：route 环只要有 $end 出口即合法）──


def test_route_backedge_is_legal_loop():
    """route 回指是合法循环（单轨模型：route 环只要有 $end 出口即放行）。

    a⇄b 仅靠 route 连通，a 有 $end 出口 → 不报死胡同。
    （迁移前这测试叫 test_route_backedge_is_not_after_cycle，after 废除后改名。）
    """
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
        _agent("b", prompt="got {{ a.output }}", routes=[{"to": "$end"}]),
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


def test_jinja_parallel_group_route_when_checked():
    """⑦ 一致性：parallel 组 route.when 引用不存在的 node → error（与 node 路由同校验）。

    回归：_iter_templates 曾漏遍历 wf.parallel，导致组路由的坏引用被静默放行。
    """
    wf = _wf(
        [
            _agent("a", routes=[{"to": "split"}]),
            _agent("b", routes=[{"to": "$end"}]),
            _agent("c", routes=[{"to": "$end"}]),
        ],
        parallel=[{
            "name": "split", "branches": ["b", "c"],
            "routes": [{"when": "ghost.output.x == 1", "to": "$end"}],
        }],
    )
    errs = _errors(wf)
    assert any("split" in e and "ghost" in e for e in errs)


def test_jinja_parallel_group_route_when_output_valid():
    """⑦ 正向：parallel 组 route.when 引用 output（组聚合输出）合法。"""
    wf = _wf(
        [
            _agent("a", routes=[{"to": "split"}]),
            _agent("b", routes=[{"to": "$end"}]),
            _agent("c", routes=[{"to": "$end"}]),
        ],
        parallel=[{
            "name": "split", "branches": ["b", "c"],
            "routes": [{"when": "output.count == 2", "to": "$end"}],
        }],
    )
    validate_workflow(wf)


def test_jinja_foreach_body_item_var_valid():
    """foreach body 里 item_var（candidate）合法，不被当未知 node。"""
    wf = _wf([
        _agent("f", routes=[{"to": "fe"}]),
        {
            "name": "fe", "kind": "foreach",
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
            "name": "fe", "kind": "foreach",
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
            "name": "fe", "kind": "foreach",
            "source": "f.output.items",
            "body": {"kind": "agent", "prompt": "x"},
            "routes": [{"to": "$end"}],
        },
    ], entry="f")
    validate_workflow(wf)


def test_foreach_max_concurrent_zero_rejected():
    """max_concurrent < 1 → 编译期 error（run 层 ``Semaphore(max(1, ...))`` 不再静默改写）。

    意图：用户写 ``max_concurrent: 0`` 是误配置（并发上限无意义），应在编译期 fail loud
    而非被 run 层静默改成 1（用户感知不到配置失效）。
    """
    wf = _wf([
        _agent("f", routes=[{"to": "fe"}]),
        {
            "name": "fe", "kind": "foreach",
            "source": "f.output.items",
            "max_concurrent": 0,
            "body": {"kind": "agent", "prompt": "x"},
            "routes": [{"to": "$end"}],
        },
    ], entry="f")
    errs = _errors(wf)
    assert any("max_concurrent" in e and "0" in e for e in errs)


def test_foreach_max_concurrent_negative_rejected():
    """负数 max_concurrent 同样拒绝。"""
    wf = _wf([
        _agent("f", routes=[{"to": "fe"}]),
        {
            "name": "fe", "kind": "foreach",
            "source": "f.output.items",
            "max_concurrent": -3,
            "body": {"kind": "agent", "prompt": "x"},
            "routes": [{"to": "$end"}],
        },
    ], entry="f")
    errs = _errors(wf)
    assert any("max_concurrent" in e for e in errs)


# ── errors 聚合（SPEC §6.4）──


def test_errors_aggregated():
    """一处工作流多处独立错误 → ConfigurationError.errors 含全部，不止首个。

    凑 ≥4 个独立错误：② entry 不存在(ghost_entry) + ④ route 引用 nowhere
    + ⑪ 兜底 route(nowhere 无 when)不是最后一条 + ⑦ prompt 引用 nope。
    """
    wf = _wf(
        [_agent("a", prompt="use {{ nope.output.x }}",
                routes=[{"to": "nowhere"}, {"when": "output.x", "to": "$end"}])],
        entry="ghost_entry",
    )
    errs = _errors(wf)
    # 至少 4 个独立错误：entry 不存在 / route nowhere / 兜底不在最后 / jinja nope
    assert len(errs) >= 4
    joined = " ".join(errs)
    assert "ghost_entry" in joined
    assert "nowhere" in joined
    assert "nope" in joined
    assert "最后一条" in joined


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
        ("bad_route", "route"),
        ("dead_end", "$end"),
        ("bad_jinja", "ghost"),
        ("bad_foreach_source", "source"),
        ("multi_error", "ghost_entry"),
        ("structural_error", "结构校验"),
        # phase 5 新增 fixture
        ("bad_parallel_branches", "branch"),
        ("bad_parallel_too_few", "branches"),
        ("bad_parallel_dup_branch", "重复"),
        ("bad_parallel_self_ref", "自引用"),
        ("bad_route_fallback", "最后一条"),
        ("bad_entry_is_parallel", "parallel 组"),
    ],
)
def test_fixture_rejected(fixtures_dir, fixture, keyword):
    with pytest.raises(ConfigurationError) as exc:
        load_workflow(fixtures_dir / f"{fixture}.yaml")
    joined = " ".join(exc.value.errors)
    assert keyword in joined, f"{fixture}: 期望含 '{keyword}'，实得 {exc.value.errors}"


def test_parallel_reachable_fixture_valid(fixtures_dir):
    """parallel_reachable.yaml：合法 parallel 组 + entry 经组可达 $end → 不抛。"""
    wf = load_workflow(fixtures_dir / "parallel_reachable.yaml")
    validate_workflow(wf)


def test_multi_error_fixture_aggregated(fixtures_dir):
    """multi_error.yaml 一处 YAML 含 4 个独立错 → 一次报全。"""
    with pytest.raises(ConfigurationError) as exc:
        load_workflow(fixtures_dir / "multi_error.yaml")
    assert len(exc.value.errors) >= 4


# ── phase 5 单轨化：⑩⑪⑬④⑥ parallel 组 / 兜底位置 / entry 非组 ────────────────
#
# 以下用 _wf(parallel=[...]) 内联构造，逐项验证 SPEC §2.2 的新校验意图。
# ⑩ parallel 组结构：branches < 2 / 引用不存在 node / 重复 / 自引用 / 组名与 node 名冲突
# ⑪ 兜底 route 位置（node 与 parallel 组都校验）
# ⑬ entry 不能指向 parallel 组
# ④ route.to 指向 parallel 组名 → 合法
# ⑥ entry 经 parallel 组可达 $end；parallel 组死胡同


def _parallel_diamond(*, group_routes=None, branches=None, group_name="split",
                      entry_routes=None):
    """构造一个 a→split(parallel 组)→d 的 diamond 骨架，便于 ⑩⑥ 测试复用。"""
    return _wf(
        [
            _agent("a", routes=entry_routes if entry_routes is not None else [{"to": group_name}]),
            _agent("b", routes=[{"to": "$end"}]),
            _agent("c", routes=[{"to": "$end"}]),
            _agent("d", routes=[{"to": "$end"}]),
        ],
        parallel=[{
            "name": group_name,
            "branches": branches if branches is not None else ["b", "c"],
            "routes": group_routes if group_routes is not None else [{"to": "d"}],
        }],
    )


# ── ⑩ parallel 组结构 ──


def test_parallel_branches_too_few():
    """⑩-1：branches 长度 < 2 → error（少于 2 不是并行）。"""
    wf = _parallel_diamond(branches=["b"])
    errs = _errors(wf)
    assert any("branches" in e and "< 2" in e for e in errs)


def test_parallel_branch_ref_missing():
    """⑩-2：branch 引用不存在的 node → error。"""
    wf = _parallel_diamond(branches=["b", "ghost"])
    errs = _errors(wf)
    assert any("branch" in e and "ghost" in e for e in errs)


def test_parallel_branch_duplicate():
    """⑩-3：branches 重复 → error。"""
    wf = _parallel_diamond(branches=["b", "b"])
    errs = _errors(wf)
    assert any("重复" in e and "b" in e for e in errs)


def test_parallel_self_reference():
    """⑩-4：组 route 自引用 → error。"""
    wf = _parallel_diamond(group_routes=[{"to": "split"}])
    errs = _errors(wf)
    assert any("自引用" in e and "split" in e for e in errs)


def test_parallel_group_name_collides_with_node():
    """① 扩展：parallel 组名与 node 名冲突 → error（共享命名空间）。"""
    wf = _wf(
        [_agent("a", routes=[{"to": "$end"}]), _agent("dup", routes=[{"to": "$end"}])],
        parallel=[{"name": "dup", "branches": ["a", "dup"], "routes": [{"to": "$end"}]}],
    )
    errs = _errors(wf)
    assert any("重复" in e and "dup" in e for e in errs)


def test_parallel_branch_cannot_reference_group():
    """⑩-2：branch 不能指向另一个 parallel 组（组内不嵌套组）。"""
    wf = _wf(
        [
            _agent("a", routes=[{"to": "outer"}]),
            _agent("b", routes=[{"to": "$end"}]),
            _agent("c", routes=[{"to": "$end"}]),
        ],
        parallel=[
            {"name": "outer", "branches": ["b", "inner"], "routes": [{"to": "$end"}]},
            {"name": "inner", "branches": ["b", "c"], "routes": [{"to": "$end"}]},
        ],
    )
    errs = _errors(wf)
    # outer 的 branch 'inner' 是组名不是 node → ⑩-2 报错
    assert any("branch" in e and "inner" in e for e in errs)


def test_parallel_group_empty_name():
    """① 扩展：parallel 组 name 空字符串 → error（与 node 空 name 对称）。"""
    wf = _wf(
        [_agent("a", routes=[{"to": "$end"}]), _agent("b", routes=[{"to": "$end"}])],
        parallel=[{"name": "", "branches": ["a", "b"], "routes": [{"to": "$end"}]}],
    )
    errs = _errors(wf)
    assert any("parallel 组" in e and "name" in e for e in errs)


# ── ⑪ 兜底 route 位置（node 与 parallel 组都校验）──


def test_route_fallback_not_last_on_node():
    """⑪：node 的兜底 route（when=None）不在最后 → error（其后的 route 不可达）。"""
    wf = _wf([
        _agent("a", routes=[{"to": "b"}, {"when": "output.x", "to": "$end"}]),
        _agent("b", routes=[{"to": "$end"}]),
    ])
    errs = _errors(wf)
    assert any("最后一条" in e and "a" in e for e in errs)


def test_route_fallback_not_last_on_parallel_group():
    """⑪：parallel 组的兜底 route 不在最后 → error。"""
    wf = _parallel_diamond(
        group_routes=[{"to": "d"}, {"when": "output.x", "to": "$end"}],
    )
    errs = _errors(wf)
    assert any("最后一条" in e and "split" in e for e in errs)


def test_route_fallback_last_is_ok():
    """⑪ 正向：兜底 route 是最后一条 → 不报。"""
    wf = _wf([
        _agent("a", routes=[{"when": "output.x", "to": "b"}, {"to": "$end"}]),
        _agent("b", routes=[{"to": "$end"}]),
    ])
    validate_workflow(wf)


def test_route_single_fallback_route_is_ok():
    """⑪ 边界：单条兜底 route（len=1，i=0==len-1）合法 —— node 与 parallel 组两侧。

    回归：_check_fallback_last 的 `i != len(routes)-1` 在 len==1 时不应误报。
    """
    # node 侧单 route（已被多个测试隐式覆盖，此处显式锁定）
    validate_workflow(_wf([_agent("a", routes=[{"to": "$end"}])]))
    # parallel 组侧单 route（无显式覆盖，补上）
    validate_workflow(_wf(
        [
            _agent("a", routes=[{"to": "split"}]),
            _agent("b", routes=[{"to": "$end"}]),
            _agent("c", routes=[{"to": "$end"}]),
        ],
        parallel=[{"name": "split", "branches": ["b", "c"], "routes": [{"to": "$end"}]}],
    ))


# ── ⑬ entry 不能是 parallel 组 ──


def test_entry_cannot_be_parallel_group():
    """⑬：entry 指向 parallel 组 → error（entry 必须是 node）。"""
    wf = _wf(
        [_agent("a", routes=[{"to": "$end"}]), _agent("b", routes=[{"to": "$end"}])],
        entry="split",
        parallel=[{"name": "split", "branches": ["a", "b"], "routes": [{"to": "$end"}]}],
    )
    errs = _errors(wf)
    assert any("parallel 组" in e and "split" in e for e in errs)


# ── ④ route.to 指向 parallel 组名 → 合法 ──


def test_route_to_parallel_group_name_valid():
    """④：node 的 route.to 指向 parallel 组名 → 合法（不报 ④）。"""
    wf = _parallel_diamond()  # a.routes → split（组名）
    validate_workflow(wf)  # 整个 diamond 合法


def test_parallel_group_route_to_node_valid():
    """④：parallel 组的 route.to 指向 node → 合法。"""
    wf = _parallel_diamond(group_routes=[{"to": "d"}])
    validate_workflow(wf)


# ── ⑥ entry 经 parallel 组可达 $end（含死胡同检测）──


def test_parallel_group_reachable_to_end():
    """⑥：entry→parallel 组→组 routes→$end，可达 → 不报死胡同。"""
    wf = _parallel_diamond()  # a→split, split→d, d→$end；b/c 各自 $end
    validate_workflow(wf)


def test_parallel_group_no_routes_is_implicit_terminal():
    """⑥：parallel 组无 routes = 隐式终态（SPEC §2.2⑥ line 215）。

    node→group(group.routes=[])，组完成后隐式结束；branches 各自有 $end 出口，
    可达性展开到 branches → $end，不报死胡同。
    且组本身经 a→split 可达，不应被误报孤立（回归：successors_of 曾漏把组名标记可达）。
    """
    wf = _wf(
        [
            _agent("a", routes=[{"to": "split"}]),
            _agent("b", routes=[{"to": "$end"}]),
            _agent("c", routes=[{"to": "$end"}]),
        ],
        parallel=[{"name": "split", "branches": ["b", "c"]}],  # 无 routes
    )
    warnings = validate_workflow(wf)
    # 组 split 经 a 路由可达，绝不能被误报孤立
    assert not any("split" in w and "孤立" in w for w in warnings), \
        f"组 split 经 a→split 可达，不应报孤立：{warnings}"


def test_parallel_group_dead_end_detected():
    """⑥：parallel 组完成后无 $end 出口且组 routes 指向死胡同 → 报死胡同。

    split.branches=[b,c] 都 routes→b（无 $end 出口的环）→ b/c 死胡同；
    split.routes 也指向 b（死胡同）→ split 死胡同；a→split 死胡同。
    """
    wf = _wf(
        [
            _agent("a", routes=[{"to": "split"}]),
            _agent("b", routes=[{"to": "b"}]),  # 自环无 $end
            _agent("c", routes=[{"to": "b"}]),
        ],
        parallel=[{
            "name": "split", "branches": ["b", "c"], "routes": [{"to": "b"}],
        }],
    )
    errs = _errors(wf)
    assert any("$end" in e for e in errs)


def test_parallel_group_orphan_is_warning():
    """⑥：parallel 组从 entry 不可达 → warning（不阻止）。"""
    wf = _wf(
        [_agent("a", routes=[{"to": "$end"}]),
         _agent("b", routes=[{"to": "$end"}]), _agent("c", routes=[{"to": "$end"}])],
        parallel=[{"name": "orphan_group", "branches": ["b", "c"], "routes": [{"to": "$end"}]}],
    )
    warnings = validate_workflow(wf)
    assert any("orphan_group" in w and "parallel 组" in w for w in warnings)
