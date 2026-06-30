"""tests/schema/test_workflow.py —— Workflow / Node / Route / 各 kind 构造与判别联合分派。

覆盖 SPEC §7.2（discriminated union 验收）+ §7.4（端到端 yaml 解析）。
测试覆盖意图：判别联合正确分派、extra=forbid 拒多余字段、body 仅允许 agent/script。
"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from orca.schema import (
    AgentNode,
    AnnotatedNode,
    ForeachNode,
    InputDef,
    Node,
    ParallelGroup,
    Route,
    ScriptNode,
    SetNode,
    Workflow,
)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


# ── 各 kind 直接构造 + 默认值 ──


def test_agent_node_defaults():
    a = AgentNode(name="a")
    assert a.kind == "agent"
    assert a.executor == "claude"
    assert a.prompt is None
    assert a.tools is None
    assert a.model is None
    assert a.output_schema is None


def test_script_node_construct():
    s = ScriptNode(name="s", command="ls -la")
    assert s.kind == "script"
    assert s.command == "ls -la"
    assert s.parse_json is False
    assert s.timeout is None


def test_set_node_construct():
    n = SetNode(name="set1", values={"best": "{{ x.output }}"})
    assert n.kind == "set"
    assert n.values == {"best": "{{ x.output }}"}


def test_foreach_node_construct():
    body = AgentNode(prompt="评估：{{ item }}")
    f = ForeachNode(name="fe", source="finder.output.candidates", body=body)
    assert f.kind == "foreach"
    assert f.item_var == "item"
    assert f.index_var == "_index"
    assert f.max_concurrent == 10
    assert f.failure_mode == "fail_fast"
    assert isinstance(f.body, AgentNode)


# ── discriminator 分派（从 dict 经 Workflow 构造）──


@pytest.mark.parametrize(
    "node_dict, expected_cls",
    [
        ({"name": "a", "kind": "agent"}, AgentNode),
        ({"name": "a", "kind": "script", "command": "ls"}, ScriptNode),
        ({"name": "a", "kind": "set", "values": {"k": "v"}}, SetNode),
    ],
)
def test_discriminator_dispatch(node_dict, expected_cls):
    wf = Workflow(name="w", entry="a", nodes=[node_dict])
    assert isinstance(wf.nodes[0], expected_cls)


def test_discriminator_foreach_from_dict():
    wf = Workflow(
        name="w",
        entry="fe",
        nodes=[
            {
                "name": "fe",
                "kind": "foreach",
                "source": "x.output.ys",
                "body": {"kind": "agent", "prompt": "p"},
            }
        ],
    )
    node = wf.nodes[0]
    assert isinstance(node, ForeachNode)
    assert isinstance(node.body, AgentNode)


def test_foreach_body_accepts_script():
    f = ForeachNode(
        name="f", source="s", body={"kind": "script", "command": "echo hi"}
    )
    assert isinstance(f.body, ScriptNode)


def test_unknown_kind_rejected():
    with pytest.raises(ValidationError):
        Workflow(name="w", entry="a", nodes=[{"name": "a", "kind": "nonexistent"}])


def test_missing_kind_rejected():
    # 判别联合无 kind → 无法分派 → fail loud
    with pytest.raises(ValidationError):
        Workflow(name="w", entry="a", nodes=[{"name": "a"}])


# ── extra="forbid" ──


@pytest.mark.parametrize(
    "cls, kwargs",
    [
        (AgentNode, {"name": "a", "prompt": "x", "wrong_field": 1}),
        (ScriptNode, {"name": "a", "command": "ls", "bogus": 2}),
        (SetNode, {"name": "a", "values": {}, "extra": 3}),
        (ForeachNode, {"name": "a", "source": "s", "body": AgentNode(), "nope": 4}),
    ],
)
def test_extra_forbid_on_kind(cls, kwargs):
    with pytest.raises(ValidationError):
        cls(**kwargs)


def test_foreach_body_rejects_set():
    # SPEC §2.3：body 仅 agent/script，不含 set/foreach
    with pytest.raises(ValidationError):
        ForeachNode(name="f", source="s", body={"kind": "set", "values": {}})


def test_foreach_body_rejects_foreach():
    # 对称闭合：body 同样拒绝 foreach（已测 set，补 foreach）
    with pytest.raises(ValidationError):
        ForeachNode(name="f", source="s", body={"kind": "foreach"})


def test_foreach_body_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        ForeachNode(name="f", source="s", body={"kind": "bogus"})


def test_workflow_extra_forbid():
    with pytest.raises(ValidationError):
        Workflow(name="w", entry="a", nodes=[], bogus=1)


def test_extra_forbid_via_dict_path():
    # 真实失败模式：YAML→dict→Workflow 路径上，节点 dict 的多余字段被拒
    # （非仅直接构造子类；discriminated union 分派后整体验证 extra=forbid）
    with pytest.raises(ValidationError):
        Workflow(
            name="w", entry="a", nodes=[{"name": "a", "kind": "agent", "bogus": 1}]
        )


# ── Route ──


def test_route_construct():
    r = Route(to="trainer")
    assert r.when is None
    assert r.to == "trainer"


def test_route_to_end_marker():
    # SPEC §2.2："$end" 是合法路由目标（终态）。锁定它，防回归误禁
    r = Route(to="$end")
    assert r.to == "$end"


def test_route_to_required():
    with pytest.raises(ValidationError):
        Route()


def test_route_extra_forbid():
    with pytest.raises(ValidationError):
        Route(to="t", bogus=1)


# ── InputDef ──


def test_input_def_defaults():
    d = InputDef(type="int")
    assert d.required is True
    assert d.default is None
    assert d.description == ""


def test_input_def_extra_forbid():
    with pytest.raises(ValidationError):
        InputDef(type="int", bogus=1)


# ── Workflow 顶层结构 ──


def test_workflow_defaults():
    wf = Workflow(name="w", entry="a", nodes=[AgentNode(name="a")])
    assert wf.description == ""
    assert wf.inputs == {}
    assert wf.outputs == {}


def test_annotated_node_exported():
    # AnnotatedNode 是判别联合别名，应可导入
    assert AnnotatedNode is not None


# ── 端到端：3 个 examples yaml 解析成 Workflow（SPEC §7.4）──


@pytest.mark.parametrize(
    "name, entry",
    [
        ("nas", "optimizer"),
        ("parallel_research", "researcher_a"),
        ("batch_assess", "finder"),
    ],
)
def test_example_yaml_parses(name, entry):
    with (EXAMPLES / f"{name}.yaml").open() as f:
        data = yaml.safe_load(f)
    wf = Workflow(**data)
    assert wf.name == name
    assert wf.entry == entry
    assert len(wf.nodes) > 0


def _load_example(name):
    with (EXAMPLES / f"{name}.yaml").open() as f:
        return Workflow(**yaml.safe_load(f))


def test_nas_yaml_deep_parse():
    """E2E 深度验证：分派正确 + inputs/outputs 真被解析（SPEC §7.4 实质，非浅断言）。"""
    wf = _load_example("nas")
    by_name = {n.name: n for n in wf.nodes}

    # 分派正确性：evaluator 必须是 ScriptNode（不能被静默吞成 AgentNode）
    assert isinstance(by_name["evaluator"], ScriptNode)
    assert isinstance(by_name["optimizer"], AgentNode)
    assert by_name["optimizer"].output_schema is not None
    assert isinstance(by_name["record_best"], SetNode)

    # inputs 解析
    assert wf.inputs["iterations"].type == "int"
    assert wf.inputs["iterations"].default == 3

    # outputs 解析
    assert "best_structure" in wf.outputs
    assert "final_accuracy" in wf.outputs


def test_batch_assess_yaml_deep_parse():
    """E2E：foreach 节点 + 无名 body 分派 + foreach 专属字段被解析。"""
    wf = _load_example("batch_assess")
    by_name = {n.name: n for n in wf.nodes}
    assessor = by_name["assessor"]
    assert isinstance(assessor, ForeachNode)
    assert isinstance(assessor.body, AgentNode)  # 无名 body 正确分派
    assert assessor.source == "finder.output.candidates"
    assert assessor.item_var == "candidate"
    assert assessor.max_concurrent == 3
    assert assessor.failure_mode == "continue_on_error"


def test_parallel_research_yaml_deep_parse():
    """E2E：parallel 组（diamond 汇聚）被解析为顶层独立列表。

    注意：researcher_a 既是 entry 又在 researchers_merge.branches 里——这是有意为之。
    运行时 parallel 组幂等执行（已执行的 branch 跳过，SPEC §4.4），不会跑两次；
    静态校验只判可达性（a→组→synthesizer→$end），不重复执行归 5-R。
    """
    wf = _load_example("parallel_research")
    # parallel 组作为顶层独立列表存在
    assert len(wf.parallel) == 1
    group = wf.parallel[0]
    assert isinstance(group, ParallelGroup)
    assert group.name == "researchers_merge"
    assert group.branches == ["researcher_a", "researcher_b"]
    assert group.failure_mode == "continue_on_error"
    # 组完成后路由到 synthesizer
    assert [r.to for r in group.routes] == ["synthesizer"]
    # researcher_a 作为 entry，完成后路由到 parallel 组（route.to 指向组名合法）
    by_name = {n.name: n for n in wf.nodes}
    assert [r.to for r in by_name["researcher_a"].routes] == ["researchers_merge"]


# ── phase 5 单轨化：after 字段删除 + ParallelGroup + Workflow.parallel ────────


def test_node_has_no_after():
    """after 字段已删除（phase 5 单轨化迁移）。"""
    n = AgentNode(name="a", prompt="p", routes=[{"to": "$end"}])
    assert not hasattr(n, "after")


def test_parallel_group_basic():
    g = ParallelGroup(name="g", branches=["a", "b"])
    assert g.name == "g"
    assert g.branches == ["a", "b"]
    assert g.failure_mode == "fail_fast"  # 默认
    assert g.routes == []


def test_parallel_group_failure_mode_literal():
    """failure_mode 是 Literal，非法值被拒（fail loud）。"""
    with pytest.raises(ValidationError):
        ParallelGroup(name="g", branches=["a"], failure_mode="invalid")


def test_parallel_group_extra_forbid():
    with pytest.raises(ValidationError):
        ParallelGroup(name="g", branches=["a", "b"], bogus=1)


def test_parallel_group_routes_required_field_is_to():
    """组的 route 同样要求 to 字段（Route schema）。"""
    g = ParallelGroup(name="g", branches=["a", "b"], routes=[{"to": "$end"}])
    assert g.routes[0].to == "$end"


def test_workflow_parallel_default_empty():
    """Workflow.parallel 默认空列表。"""
    wf = Workflow(
        name="w", entry="a", nodes=[AgentNode(name="a", prompt="p", routes=[{"to": "$end"}])]
    )
    assert wf.parallel == []


def test_workflow_accepts_parallel_via_dict():
    """YAML→dict→Workflow 路径上 parallel 列表被解析为 ParallelGroup。"""
    wf = Workflow(
        name="w",
        entry="a",
        nodes=[
            {"name": "a", "kind": "agent", "prompt": "p", "routes": [{"to": "split"}]},
            {"name": "b", "kind": "agent", "prompt": "p", "routes": [{"to": "$end"}]},
            {"name": "c", "kind": "agent", "prompt": "p", "routes": [{"to": "$end"}]},
        ],
        parallel=[{"name": "split", "branches": ["b", "c"], "routes": [{"to": "$end"}]}],
    )
    assert len(wf.parallel) == 1
    assert isinstance(wf.parallel[0], ParallelGroup)
    assert wf.parallel[0].branches == ["b", "c"]


def test_node_extra_forbid_rejects_after():
    """after 字段已删除 → YAML 里写 after 应被 extra=forbid 拒（fail loud）。"""
    with pytest.raises(ValidationError):
        Workflow(
            name="w", entry="a", nodes=[{"name": "a", "kind": "agent", "after": ["b"]}]
        )
