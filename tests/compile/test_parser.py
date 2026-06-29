"""tests/compile/test_parser.py —— load_workflow 解析 + prompt 约定加载 + 错误包装。

覆盖 SPEC §6.1（接口）/ §6.2（解析）/ §6.4（聚合）。测试意图：
- 3 个 example 经 load_workflow 通过，且约定加载让每个 agent.prompt 成为确定字符串
- 约定加载：内联不读文件 / 约定命中 / 约定缺失聚合报错
- pydantic 结构错包装成 ConfigurationError；YAML 语法错透传
"""

from __future__ import annotations

import pytest
import yaml

from orca.compile import ConfigurationError, load_workflow
from orca.schema import AgentNode, Workflow

from .conftest import write_yaml


# ── 3 个 example：load_workflow 通过 + 约定加载生效（SPEC §6.1/§6.2）──


@pytest.mark.parametrize(
    "name, entry, node_count",
    [
        ("nas", "optimizer", 5),
        ("parallel_research", "researcher_a", 3),
        ("batch_assess", "finder", 3),
    ],
)
def test_examples_load(examples_dir, name, entry, node_count):
    wf = load_workflow(examples_dir / f"{name}.yaml")
    assert isinstance(wf, Workflow)
    assert wf.name == name
    assert wf.entry == entry
    assert len(wf.nodes) == node_count


@pytest.mark.parametrize("name", ["nas", "parallel_research", "batch_assess"])
def test_examples_all_agents_have_string_prompt(examples_dir, name):
    """约定加载后，每个顶层 agent.prompt 都是确定字符串（run/ 不再管文件加载）。"""
    wf = load_workflow(examples_dir / f"{name}.yaml")
    agents = [n for n in wf.nodes if isinstance(n, AgentNode)]
    assert agents, f"{name} 应至少有一个 agent"
    for a in agents:
        assert isinstance(a.prompt, str) and a.prompt, f"agent '{a.name}' prompt 未被填充"


def test_optimizer_prompt_loaded_from_convention(examples_dir):
    """nas.optimizer 无内联 prompt → 内容来自 agents/optimizer.md（非空且含迭代约定）。"""
    wf = load_workflow(examples_dir / "nas.yaml")
    optimizer = next(n for n in wf.nodes if n.name == "optimizer")
    assert isinstance(optimizer, AgentNode)
    assert "优化器" in optimizer.prompt or "iterations" in optimizer.prompt


# ── prompt 约定加载（SPEC §3 _load_prompts）──


def test_inline_prompt_not_overwritten(tmp_path):
    """有内联 prompt 时不读约定文件（即便无 agents/x.md 也不报错）。"""
    doc = """
    name: w
    entry: a
    nodes:
      - name: a
        kind: agent
        prompt: "inline prompt"
        routes: [{to: "$end"}]
    """
    wf = load_workflow(write_yaml(tmp_path, doc))
    assert wf.nodes[0].prompt == "inline prompt"


def test_convention_prompt_loaded(tmp_path):
    """prompt 省略 → 从 agents/<name>.md 加载。"""
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "a.md").write_text("convention body", encoding="utf-8")
    doc = {
        "name": "w",
        "entry": "a",
        "nodes": [{"name": "a", "kind": "agent", "routes": [{"to": "$end"}]}],
    }
    wf = load_workflow(write_yaml(tmp_path, doc))
    assert wf.nodes[0].prompt == "convention body"


def test_convention_missing_raises(tmp_path):
    """prompt 省略且无约定文件 → ConfigurationError 精确点名 agent。"""
    doc = {
        "name": "w",
        "entry": "a",
        "nodes": [{"name": "a", "kind": "agent", "routes": [{"to": "$end"}]}],
    }
    with pytest.raises(ConfigurationError) as exc:
        load_workflow(write_yaml(tmp_path, doc))
    assert any("'a'" in e and "agents" in e for e in exc.value.errors)


def test_convention_missing_aggregated(tmp_path):
    """多个 agent 都缺约定文件 → 一次性聚合报全（SPEC §1 聚合精神）。"""
    doc = {
        "name": "w",
        "entry": "a",
        "nodes": [
            {"name": "a", "kind": "agent", "routes": [{"to": "b"}]},
            {"name": "b", "kind": "agent", "routes": [{"to": "$end"}]},
        ],
    }
    with pytest.raises(ConfigurationError) as exc:
        load_workflow(write_yaml(tmp_path, doc))
    assert len(exc.value.errors) == 2
    joined = " ".join(exc.value.errors)
    assert "'a'" in joined and "'b'" in joined


# ── 结构错包装 + YAML 语法错透传（SPEC §3 失败模式）──


def test_pydantic_structural_error_wrapped(fixtures_dir):
    """未知 kind（pydantic ValidationError）→ 包装成 ConfigurationError，对外单一类型。"""
    with pytest.raises(ConfigurationError) as exc:
        load_workflow(fixtures_dir / "structural_error.yaml")
    assert exc.value.errors  # 非空


def test_yaml_syntax_error_passthrough(tmp_path):
    """YAML 语法错 → yaml.YAMLError 透传（不包装）。"""
    bad = "name: w\n  nodes: [this is : broken :\n"
    with pytest.raises(yaml.YAMLError):
        load_workflow(write_yaml(tmp_path, bad))


# ── fixture E2E：缺约定文件 ──


def test_missing_prompt_fixture(fixtures_dir):
    with pytest.raises(ConfigurationError) as exc:
        load_workflow(fixtures_dir / "missing_prompt.yaml")
    assert any("orphan" in e for e in exc.value.errors)


# ── ConfigurationError 结构 ──


def test_configuration_error_shape():
    err = ConfigurationError(["e1", "e2"], ["w1"])
    assert err.errors == ["e1", "e2"]
    assert err.warnings == ["w1"]
    msg = str(err)
    assert "e1" in msg and "❌" in msg and "⚠️" in msg and "w1" in msg
