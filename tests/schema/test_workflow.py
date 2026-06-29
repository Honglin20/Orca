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


def test_foreach_body_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        ForeachNode(name="f", source="s", body={"kind": "bogus"})


def test_workflow_extra_forbid():
    with pytest.raises(ValidationError):
        Workflow(name="w", entry="a", nodes=[], bogus=1)


# ── Route ──


def test_route_construct():
    r = Route(to="trainer")
    assert r.when is None
    assert r.to == "trainer"


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
