"""tests/compile/test_validator.py —— 9 项语义校验各正/反例 + 聚合 + warnings。

直接调 validate_workflow（内部入口），逐项验证 SPEC §4 的 9 条规则
（①②④⑥⑦⑧⑨⑩⑪⑬，③⑤ 随 after 废除）。
测试覆盖意图（非仅行为）：每项校验对「正确工作流」放行、对「对应错误」精确报错；
errors 聚合（多处错一次报全）；warnings 不阻止返回。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orca.compile import ConfigurationError, load_workflow
from orca.compile.validator import ValidationResult, validate_workflow
from orca.schema import Workflow

# repo root（tests/compile/test_validator.py → parents[2]）
_REPO_ROOT = Path(__file__).resolve().parents[2]
_REAL_WORKFLOWS = sorted(str(p) for p in (_REPO_ROOT / "workflows").glob("*.yaml"))


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
        inputs={"its": {"type": "int", "description": "[default] labeled"}},
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
        inputs={"its": {"type": "int", "description": "[default] labeled"}},
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


# ── terminate step 约束（routes 空 / 非entry / 非parallel branch / 非foreach body）──


def _terminate(name: str = "t", **kw) -> dict:
    """单 terminate 节点 dict。"""
    d = {"name": name, "kind": "terminate", "status": kw.pop("status", "failed")}
    d.update(kw)
    return d


def test_terminate_valid_minimal():
    """合法 terminate 节点：routes 空，被业务 node 路由到。"""
    wf = _wf([
        _agent("classifier", routes=[{"to": "reject"}]),
        _terminate("reject", status="failed", reason="reject {{ classifier.output.x }}"),
    ], entry="classifier")
    validate_workflow(wf)  # 不抛


def test_terminate_with_routes_rejected():
    """terminate.routes 非空 → error（terminate 不评估路由，非空 routes 是死代码）。"""
    wf = _wf([
        _agent("a", routes=[{"to": "t"}]),
        _terminate("t", status="failed", routes=[{"to": "$end"}]),
    ], entry="a")
    errs = _errors(wf)
    assert any("terminate" in e and "'t'" in e and "routes" in e for e in errs), errs


def test_terminate_as_entry_rejected():
    """terminate 作为 workflow.entry → error（必须先经业务节点）。"""
    wf = _wf([
        _terminate("t", status="success"),
        _agent("a", routes=[{"to": "$end"}]),
    ], entry="t")
    errs = _errors(wf)
    assert any("terminate" in e and "entry" in e for e in errs), errs


def test_terminate_in_parallel_branches_rejected():
    """terminate 出现在 parallel 组的 branches 里 → error（语义不清，同 Conductor 限制）。"""
    wf = _wf([
        _agent("a", routes=[{"to": "split"}]),
        _agent("b", routes=[{"to": "$end"}]),
        _terminate("c", status="failed"),
    ], entry="a", parallel=[{"name": "split", "branches": ["b", "c"], "routes": [{"to": "$end"}]}])
    errs = _errors(wf)
    assert any("terminate" in e and "branch" in e and "'c'" in e for e in errs), errs


def test_terminate_in_foreach_body_rejected_by_schema():
    """terminate 出现在 foreach body → schema 层 ForeachBody 判别联合就拦（pydantic raise）。

    schema 层 fail loud：ForeachBody 仅允许 agent/script，terminate 不在联合里 →
    ValidationError 在 Workflow 构造时抛，到不了 compile/validator。
    """
    from pydantic import ValidationError

    bad_body = {"name": "leaf", "kind": "terminate", "status": "failed"}
    with pytest.raises(ValidationError):
        Workflow(
            name="w",
            entry="fe",
            nodes=[{"name": "fe", "kind": "foreach", "source": "x.y", "body": bad_body}],
        )


def test_terminate_jinja_ref_validated():
    """terminate.reason / outputs 的 Jinja2 引用也走 ⑦ 浅校验（fail loud 在 compile 期）。"""
    wf = _wf([
        _agent("a", routes=[{"to": "t"}]),
        _terminate("t", status="failed", reason="bad {{ ghost.output.x }}"),
    ], entry="a")
    errs = _errors(wf)
    assert any("ghost" in e for e in errs), errs


def test_terminate_success_with_outputs_valid():
    """status=success + outputs 引用合法 node → 通过校验。"""
    wf = _wf([
        _agent("classifier", routes=[{"to": "done"}]),
        _terminate("done", status="success", outputs={"cat": "{{ classifier.output.x }}"}),
    ], entry="classifier")
    validate_workflow(wf)  # 不抛


# ── 铁律 7：execute phase 永不中断（gate 校验，从 tests/iface/mcp/test_setup_phase.py 搬迁）──
# 这 3 个测试是 ``_check_execute_phase_no_gate_tools`` 的唯一覆盖。setup phase 删除（in-session
# v5 §6.1 step 5a）后从 setup_phase 测试文件搬迁到此（compile 层归属），去 setup 专属上下文
# 使其 compile 自洽。``_check_execute_phase_no_gate_tools`` / ``_INTERRUPT_TOOL_NAMES`` /
# ``_check_no_interrupt_tools`` 保留（A2 铁律：与 setup 正交）。


def test_compile_rejects_ask_user_in_execute_phase():
    """compile validator 拒绝 execute phase agent 配 ask_user（铁律 7）。"""
    wf = _wf([_agent("a", prompt="do", tools=["ask_user"])])
    with pytest.raises(ConfigurationError) as exc_info:
        validate_workflow(wf)
    assert "ask_user" in str(exc_info.value)
    assert "execute phase" in str(exc_info.value).lower() or "铁律 7" in str(exc_info.value)


def test_compile_rejects_gate_in_execute_phase():
    """compile validator 拒绝 execute phase agent 配 gate（铁律 7）。"""
    wf = _wf([_agent("a", prompt="do", tools=["Bash", "gate"])])
    with pytest.raises(ConfigurationError) as exc_info:
        validate_workflow(wf)
    assert "gate" in str(exc_info.value)


def test_compile_allows_tools_none_in_execute_phase():
    """compile validator 允许 execute phase agent tools=None（默认全开，runtime 把关）。"""
    wf = _wf([_agent("a", prompt="do", tools=None)])
    validate_workflow(wf)  # 不 raise


# ── plan sprightly-questing-donut §1.4：requires 白名单 ──

def test_requires_known_token_accepted():
    """requires:[knowledge_base]（已知 token）→ Workflow 构造成功（field_validator 放行）。"""
    wf = Workflow(
        name="w", entry="a",
        nodes=[_agent("a", routes=[{"to": "$end"}])],
        requires=["knowledge_base"],
    )
    assert wf.requires == ["knowledge_base"]


def test_requires_unknown_token_rejected():
    """requires 含 typo（如 knowlegde_base）→ field_validator fail loud（防预检静默失效）。"""
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as ei:
        Workflow(
            name="w", entry="a",
            nodes=[_agent("a", routes=[{"to": "$end"}])],
            requires=["knowlegde_base"],  # typo
        )
    assert "knowledge_base" in str(ei.value)  # 错误信息含已知 token 提示


# ── 引用合规深度校验（self_reference / output_schema 对齐 / scripts 存在 / input 三档）──
#
# 校验 4 项新规则（catch struct {%raw%} 误删类 bug + 字段拼写 / 脚本缺失 / input 标签）。
# 每条规则验「正例放行 + 反例精确报 + {%raw%} 免疫 + 现有 workflow 0 误报」。


def test_self_reference_in_prompt_rejected():
    """agent 'a' 的 prompt 引用 {{ a.output.x }} → error（render 期自身无 output）。"""
    wf = _wf([_agent("a", prompt="self {{ a.output.x }}", routes=[{"to": "$end"}])])
    errs = _errors(wf)
    assert any("自引用" in e and "a.output" in e for e in errs), errs


def test_self_reference_in_command_rejected():
    """script 'a' 的 command 引用 {{ a.output.x }} → error。"""
    wf = _wf([
        {"name": "a", "kind": "script", "command": "echo {{ a.output.x }}",
         "routes": [{"to": "$end"}]},
    ])
    errs = _errors(wf)
    assert any("自引用" in e and "a.output" in e for e in errs), errs


def test_self_reference_in_set_values_rejected():
    """set 'a' 的 values 引用 {{ a.output.x }} → error。"""
    wf = _wf([
        {"name": "a", "kind": "set", "values": {"k": "{{ a.output.x }}"},
         "routes": [{"to": "$end"}]},
    ])
    errs = _errors(wf)
    assert any("自引用" in e and "a.output" in e for e in errs), errs


def test_self_reference_in_route_when_allowed():
    """route.when 引用本节点 output 合法（评估期 self.output 已在 ctx）。"""
    wf = _wf([
        _agent("a", prompt="do", routes=[{"when": "output.x == 1", "to": "$end"}]),
    ])
    validate_workflow(wf)  # 不抛


def test_self_reference_in_route_output_at_end_allowed():
    """route.output（to=$end）引用本节点 output 合法（节点已跑完）。"""
    wf = _wf([
        _agent("a", prompt="do",
               routes=[{"to": "$end", "output": {"r": "{{ a.output.x }}"}}]),
    ])
    validate_workflow(wf)


def test_self_reference_in_workflow_outputs_allowed():
    """workflow.outputs 引用本节点 output 合法（终态渲染）。"""
    wf = _wf([
        _agent("a", prompt="do", routes=[{"to": "$end"}]),
    ], outputs={"r": "{{ a.output }}"})
    validate_workflow(wf)


def test_cross_node_reference_in_prompt_allowed():
    """agent 'a' 引用上游 'b'.output 合法（不是自引用）。"""
    wf = _wf([
        _agent("b", prompt="upstream", routes=[{"to": "a"}]),
        _agent("a", prompt="got {{ b.output }}", routes=[{"to": "$end"}]),
    ], entry="b")
    validate_workflow(wf)


def test_raw_wrapped_self_reference_immune():
    """{% raw %}{{ a.output.x }}{% endraw %} 包裹的自引用提及 → Jinja2 parse 为 Const 文本
    不进 AST 的 ref 集合 → 自引用检测**不**报错（这是修复 struct yaml 的核心理由）。"""
    wf = _wf([
        _agent("a", prompt="doc {% raw %}{{ a.output.x }}{% endraw %} end",
               routes=[{"to": "$end"}]),
    ])
    validate_workflow(wf)  # raw 包裹 → 不报自引用


def test_raw_wrap_verified_in_original_struct_scenario():
    """复刻 struct yaml 的真实场景：setup prompt 里 {% raw %}`{{ setup.output.X }}`{% endraw %}
    是文档说明（指示下游节点怎么取），不是 setup 自己的运行时引用 → 不应报自引用。

    这是「前置分析结论」的实证测试：raw 包裹让自引用检测天然免疫。"""
    wf = _wf([
        _agent("setup",
               prompt="下游经 {% raw %}`{{ setup.output.struct_scripts_dir }}`{% endraw %} 取",
               routes=[{"to": "$end"}]),
    ], entry="setup")
    validate_workflow(wf)


def test_output_schema_strict_field_typo_rejected():
    """b.output_schema additionalProperties:false；a 引用 b.output.ghost（不在 properties）→ error。"""
    schema = {
        "type": "object",
        "properties": {"known": {"type": "string"}},
        "required": ["known"],
        "additionalProperties": False,
    }
    wf = _wf([
        _agent("b", prompt="up", output_schema=schema, routes=[{"to": "a"}]),
        _agent("a", prompt="got {{ b.output.ghost }}", routes=[{"to": "$end"}]),
    ], entry="b")
    errs = _errors(wf)
    assert any("'b'" in e and "ghost" in e and "不存在的字段" in e for e in errs), errs


def test_output_schema_strict_known_field_ok():
    """b.output_schema strict；a 引用 b.output.known（在 properties）→ 通过。"""
    schema = {
        "type": "object",
        "properties": {"known": {"type": "string"}},
        "required": ["known"],
        "additionalProperties": False,
    }
    wf = _wf([
        _agent("b", prompt="up", output_schema=schema, routes=[{"to": "a"}]),
        _agent("a", prompt="got {{ b.output.known }}", routes=[{"to": "$end"}]),
    ], entry="b")
    validate_workflow(wf)


def test_output_schema_no_schema_free_text_skipped():
    """b 无 output_schema（自由文本）；a 引用 b.output.anything → 不报字段对齐（整段引用 anyway）。"""
    wf = _wf([
        _agent("b", prompt="up", routes=[{"to": "a"}]),
        _agent("a", prompt="got {{ b.output.anything }}", routes=[{"to": "$end"}]),
    ], entry="b")
    # b 无 schema → 字段对齐跳过；anything 不是「字段不存在」（运行时归 exec/）。不报。
    validate_workflow(wf)


def test_output_schema_additional_properties_true_skipped():
    """b.output_schema 显式 additionalProperties:true（放行）→ 不强制字段对齐。"""
    schema = {
        "type": "object",
        "properties": {"known": {"type": "string"}},
        "additionalProperties": True,
    }
    wf = _wf([
        _agent("b", prompt="up", output_schema=schema, routes=[{"to": "a"}]),
        _agent("a", prompt="got {{ b.output.extra }}", routes=[{"to": "$end"}]),
    ], entry="b")
    validate_workflow(wf)


def test_output_schema_script_node_skipped_no_schema():
    """ScriptNode 无 output_schema 字段（schema_map 不收）；a 引用 b.output.json.foo
    不报字段对齐——因 ScriptNode 根本不进 schema_map，天然跳过（运行时归 exec/）。

    回归锁定：原实现曾含 ``is_script_json and field == "json"`` 死代码，给读者错觉
    跳过发生在此分支；实际跳过发生在 ``schema_map`` 不收 ScriptNode（schema=None 早 continue）。
    本测试删掉死代码后必须仍绿（证明真实跳过路径）。
    """
    wf = _wf([
        {"name": "b", "kind": "script", "command": "echo {}",
         "parse_json": True, "routes": [{"to": "a"}]},
        _agent("a", prompt="got {{ b.output.json.foo }}", routes=[{"to": "$end"}]),
    ], entry="b")
    validate_workflow(wf)


def test_output_schema_typo_in_subscript_form_rejected():
    """``b.output['ghost']``（Getitem 字面量变体）也触发字段对齐。"""
    schema = {
        "type": "object",
        "properties": {"known": {"type": "string"}},
        "additionalProperties": False,
    }
    wf = _wf([
        _agent("b", prompt="up", output_schema=schema, routes=[{"to": "a"}]),
        _agent("a", prompt="got {{ b.output['ghost'] }}", routes=[{"to": "$end"}]),
    ], entry="b")
    errs = _errors(wf)
    assert any("'b'" in e and "ghost" in e for e in errs), errs


def test_folder_agent_scripts_missing_rejected(tmp_path):
    """folder agent 的 prompt 引用 $ORCA_AGENT_RESOURCES/scripts/missing.py 但脚本不存在 → error。"""
    wf = _wf([
        _agent("a", prompt="run $ORCA_AGENT_RESOURCES/scripts/missing.py",
               resources_root=str(tmp_path), routes=[{"to": "$end"}]),
    ])
    errs = _errors(wf)
    assert any("missing.py" in e and "脚本不存在" in e for e in errs), errs


def test_folder_agent_scripts_present_ok(tmp_path):
    """folder agent 引用的脚本存在 → 通过（resources_root/scripts/<file>）。"""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "helper.py").write_text("# ok", encoding="utf-8")
    wf = _wf([
        _agent("a", prompt="run $ORCA_AGENT_RESOURCES/scripts/helper.py",
               resources_root=str(tmp_path), routes=[{"to": "$end"}]),
    ])
    validate_workflow(wf)


def test_inline_prompt_without_resources_root_skipped():
    """内联 prompt（resources_root=None）即使含 $ORCA_AGENT_RESOURCES 提及也不报
    （无 resolver 物化，ORCA_AGENT_RESOURCES 语义不适用）。"""
    wf = _wf([
        _agent("a", prompt="doc mentions $ORCA_AGENT_RESOURCES/scripts/anything.py",
               routes=[{"to": "$end"}]),
    ])
    validate_workflow(wf)


def test_input_without_tier_label_warns():
    """input description 不以三档标签起头 → warning（contract §6）。"""
    wf = _wf(
        [_agent("a", prompt="do", routes=[{"to": "$end"}])],
        inputs={"x": {"type": "string", "description": "no label here"}},
    )
    warnings = validate_workflow(wf)
    assert any("input 'x'" in w and "三档标签" in w for w in warnings), warnings


def test_input_with_tier_label_ok():
    """input description 以 [ask] 起头 → 无 warning。"""
    wf = _wf(
        [_agent("a", prompt="do", routes=[{"to": "$end"}])],
        inputs={"x": {"type": "string", "description": "[ask] business decision"}},
    )
    warnings = validate_workflow(wf)
    assert not any("三档标签" in w for w in warnings), warnings


@pytest.mark.parametrize("label", ["[ask]", "[infer]", "[default]", "[advanced]"])
def test_input_all_tier_labels_accepted(label):
    """四种标签前缀都接受。"""
    wf = _wf(
        [_agent("a", prompt="do", routes=[{"to": "$end"}])],
        inputs={"x": {"type": "string", "description": label + " desc"}},
    )
    warnings = validate_workflow(wf)
    assert not any("三档标签" in w for w in warnings), warnings


def test_input_empty_description_warns():
    """空 description 也 warn（contract §6 标签是机器可读前缀，缺即不合规）。"""
    wf = _wf(
        [_agent("a", prompt="do", routes=[{"to": "$end"}])],
        inputs={"x": {"type": "string", "description": ""}},
    )
    warnings = validate_workflow(wf)
    assert any("input 'x'" in w and "三档标签" in w for w in warnings), warnings


def test_input_label_on_second_line_still_warns():
    """标签在第二行不算（contract §6 要求**起头**）→ 仍 warn。

    锁定 ``startswith`` 语义：只认首字符前缀，多行 description 不扫描后续行。
    """
    wf = _wf(
        [_agent("a", prompt="do", routes=[{"to": "$end"}])],
        inputs={"x": {"type": "string", "description": "first line\n[ask] real intent"}},
    )
    warnings = validate_workflow(wf)
    assert any("input 'x'" in w and "三档标签" in w for w in warnings), warnings


def test_self_reference_in_foreach_body_rejected():
    """foreach 'fe' 的 body.prompt 引用 {{ fe.output.X }} → error（foreach 未完成）。

    锁定 ``_iter_templates`` 对 foreach body 的 ``self_name = foreach_name`` 意图
    （validator.py:547-567 显式声明）。body 在 foreach 执行期内逐项跑，foreach.output
    尚未产出 → 引用必崩。零测试的话，重构者误改 self_name=None 不会被拦截。
    """
    wf = _wf([
        _agent("f", prompt="upstream", routes=[{"to": "fe"}]),
        {
            "name": "fe", "kind": "foreach",
            "source": "f.output.items", "item_var": "candidate",
            "body": {"kind": "agent", "prompt": "got {{ fe.output.x }}"},
            "routes": [{"to": "$end"}],
        },
    ], entry="f")
    errs = _errors(wf)
    assert any("foreach 'fe'.body" in e and "自引用" in e and "fe.output" in e
               for e in errs), errs


def test_folder_agent_scripts_in_foreach_body_rejected(tmp_path):
    """foreach body 是 AgentNode 且 body.prompt 引用缺失脚本 → error。

    锁定 ``_check_folder_agent_scripts_exist`` 的 foreach body 分支
    （validator.py:880-885 显式处理 foreach body agent）。
    """
    wf = _wf([
        _agent("f", prompt="upstream", routes=[{"to": "fe"}]),
        {
            "name": "fe", "kind": "foreach",
            "source": "f.output.items", "item_var": "candidate",
            "body": {
                "kind": "agent",
                "prompt": "run $ORCA_AGENT_RESOURCES/scripts/missing.py",
                "resources_root": str(tmp_path),
            },
            "routes": [{"to": "$end"}],
        },
    ], entry="f")
    errs = _errors(wf)
    assert any("foreach 'fe'.body" in e and "missing.py" in e
               and "脚本不存在" in e for e in errs), errs


def test_new_ref_rules_aggregate_with_jinja_check():
    """同一 wf 同时含 ⑦ 未知 root 错 + 新自引用错 + 新 schema 字段错 → 全部进 errors。

    锁定聚合属性：4 项新 ``_check_*`` 与既有 ⑦ 共享同一 ``ValidationResult``，一次报全。
    防止有人误把某个新检查改成 early-return / 抛独立异常。
    """
    schema = {
        "type": "object",
        "properties": {"known": {"type": "string"}},
        "additionalProperties": False,
    }
    wf = _wf([
        # ⑦ 未知 root 错：引用 ghost
        # 新自引用错：a 引用 a.output
        # 新 schema 字段错：a 引用 b.output.ghost_field（b strict schema）
        _agent("a",
               prompt="bad {{ ghost.output.x }} and self {{ a.output.y }} "
                      "and field {{ b.output.ghost_field }}",
               routes=[{"to": "$end"}]),
        _agent("b", prompt="up", output_schema=schema, routes=[{"to": "$end"}]),
    ])
    errs = _errors(wf)
    # 3 类错误各在：⑦ ghost / 自引用 a.output / schema ghost_field
    assert any("ghost" in e and "不存在的 node/变量" in e for e in errs), errs
    assert any("自引用" in e and "a.output" in e for e in errs), errs
    assert any("'b'" in e and "ghost_field" in e and "不存在的字段" in e
               for e in errs), errs
    assert len(errs) >= 3, errs


@pytest.mark.parametrize("wf_yaml", _REAL_WORKFLOWS)
def test_no_false_positive_on_real_workflows(wf_yaml):
    """回归：所有真实 workflows/*.yaml 在 4 项新规则下 0 误报。

    用 ``load_workflow`` 跑完整 pipeline（含 agent.md 物化），断言：
    - 不抛 ConfigurationError（无新 error）
    - warnings 不含新规则的 error 痕迹（tier-label warning 允许，但 9 个真实 wf
      的 input 全已标签化，故应为 0）

    这是对「4 项新规则不破坏现有 workflow」最强的端到端锁定——比单 fixture 扫描覆盖
    更广（含 folder agent / parallel 组 / foreach / terminate 等形态）。
    """
    wf = load_workflow(wf_yaml)
    # 不抛 = 0 errors。warnings 走 load_workflow 内部丢弃（本测试只验无 error 误报）。
    assert wf.name  # 加载成功即通过；若新规则误报 error，load_workflow 会抛进不了这行
