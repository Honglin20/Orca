"""test_setup_phase.py —— setup_outputs 校验 + 三重杠杆（SPEC phase-10 §5.9 / §2.8）。

覆盖 ``validate_setup_outputs`` 的 4 路径 + 三重杠杆 B 的 fail loud 行为：

  1. 无 setup phase（``setup_agents=[]``）→ 返空 dict（跳过）
  2. 有 setup 但 setup_outputs=None → raise ``SetupRequired``
  3. key 集合不匹配 → raise ``SetupOutputsMismatch``
  4. schema 校验失败（required 缺失）→ raise ``SetupOutputsInvalid``
  5. 全通过 → 返原样 setup_outputs dict

三重杠杆 B（§2.8）：``start_workflow`` 调本函数拦截「跳过 setup 直接 start」。
"""

from __future__ import annotations

import pytest

from orca.iface.mcp.setup_phase import (
    SetupOutputsInvalid,
    SetupOutputsMismatch,
    SetupRequired,
    validate_setup_outputs,
)
from orca.schema.workflow import AgentNode


def _make_setup_agent(
    name: str = "collector",
    *,
    output_schema: dict | None = None,
    prompt: str | None = "collect",
) -> AgentNode:
    """合成 setup AgentNode（minimally configured）。"""
    return AgentNode(
        name=name,
        kind="agent",
        prompt=prompt,
        output_schema=output_schema,
    )


# ── 1. 无 setup phase ─────────────────────────────────────────────────────────


def test_validate_no_setup_agents_returns_empty():
    """无 setup phase → 返空 dict（不校验 setup_outputs）。"""
    result = validate_setup_outputs([], {"anything": {}})
    assert result == {}


def test_validate_no_setup_agents_with_none_outputs():
    """无 setup phase + setup_outputs=None → 仍返空 dict（None 合法）。"""
    result = validate_setup_outputs([], None)
    assert result == {}


# ── 2. setup_required（三重杠杆 B 核心）──────────────────────────────────────


def test_validate_setup_required_raises():
    """有 setup 但 setup_outputs=None → raise SetupRequired。"""
    agents = [_make_setup_agent()]
    with pytest.raises(SetupRequired) as exc_info:
        validate_setup_outputs(agents, None)
    assert exc_info.value.agent_names == ["collector"]


def test_validate_setup_required_message_contains_agent_names():
    """SetupRequired message 含 agent names（MCP 层构造引导 _hint 用）。"""
    agents = [_make_setup_agent("info_a"), _make_setup_agent("info_b")]
    with pytest.raises(SetupRequired) as exc_info:
        validate_setup_outputs(agents, None)
    assert "info_a" in str(exc_info.value)
    assert "info_b" in str(exc_info.value)


# ── 3. setup_outputs_mismatch ────────────────────────────────────────────────


def test_validate_mismatch_missing_key():
    """key 少了 → raise SetupOutputsMismatch。"""
    agents = [_make_setup_agent("a"), _make_setup_agent("b")]
    with pytest.raises(SetupOutputsMismatch) as exc_info:
        validate_setup_outputs(agents, {"a": {}})
    assert "b" in exc_info.value.expected
    assert "b" not in exc_info.value.actual


def test_validate_mismatch_extra_key():
    """key 多了 → raise SetupOutputsMismatch（严格匹配，不救济）。"""
    agents = [_make_setup_agent("a")]
    with pytest.raises(SetupOutputsMismatch):
        validate_setup_outputs(agents, {"a": {}, "extra": {}})


def test_validate_mismatch_wrong_key():
    """key 全错 → raise SetupOutputsMismatch。"""
    agents = [_make_setup_agent("a")]
    with pytest.raises(SetupOutputsMismatch) as exc_info:
        validate_setup_outputs(agents, {"wrong": {}})
    assert exc_info.value.expected == ["a"]
    assert exc_info.value.actual == ["wrong"]


# ── 4. setup_outputs_invalid（schema 校验）──────────────────────────────────


def test_validate_schema_missing_required_field():
    """缺 required 字段 → raise SetupOutputsInvalid。"""
    schema = {
        "type": "object",
        "properties": {"host": {"type": "string"}},
        "required": ["host"],
    }
    agents = [_make_setup_agent("collector", output_schema=schema)]
    with pytest.raises(SetupOutputsInvalid) as exc_info:
        validate_setup_outputs(agents, {"collector": {}})
    assert exc_info.value.agent_name == "collector"


def test_validate_schema_wrong_type():
    """字段类型错 → raise SetupOutputsInvalid。"""
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
    }
    agents = [_make_setup_agent("collector", output_schema=schema)]
    with pytest.raises(SetupOutputsInvalid):
        validate_setup_outputs(agents, {"collector": {"count": "not_int"}})


def test_validate_no_schema_passes_any_dict():
    """setup agent 无 output_schema → 任意 dict 通过（只校验 key 匹配）。"""
    agents = [_make_setup_agent("free_form")]  # output_schema=None
    result = validate_setup_outputs(
        agents, {"free_form": {"anything": "ok", "nested": [1, 2]}}
    )
    assert result == {"free_form": {"anything": "ok", "nested": [1, 2]}}


# ── 5. 全通过 ────────────────────────────────────────────────────────────────


def test_validate_all_pass_returns_setup_outputs():
    """setup_outputs 校验全通过 → 返原样 dict（直接注入 workflow runtime）。"""
    schema = {
        "type": "object",
        "properties": {"host": {"type": "string"}, "port": {"type": "integer"}},
        "required": ["host"],
    }
    agents = [_make_setup_agent("collector", output_schema=schema)]
    outputs = {"collector": {"host": "nas1.example.com", "port": 22}}
    result = validate_setup_outputs(agents, outputs)
    assert result == outputs


def test_validate_multiple_agents_all_pass():
    """多 setup agent 全通过（key 集合 + schema 都匹配）。"""
    agents = [
        _make_setup_agent("a", output_schema=None),
        _make_setup_agent("b", output_schema=None),
    ]
    outputs = {"a": {"x": 1}, "b": {"y": 2}}
    result = validate_setup_outputs(agents, outputs)
    assert result == outputs


# ── compile validator execute phase check（§0.1 铁律 7）─────────────────────


def test_compile_rejects_ask_user_in_execute_phase():
    """compile validator 拒绝 execute phase agent 配 ask_user（铁律 7）。"""
    from orca.compile.validator import validate_workflow
    from orca.compile.validator import ConfigurationError
    from orca.schema.workflow import Workflow

    wf = Workflow(
        name="bad",
        entry="a",
        nodes=[
            AgentNode(name="a", kind="agent", prompt="do", tools=["ask_user"]),
        ],
    )
    with pytest.raises(ConfigurationError) as exc_info:
        validate_workflow(wf)
    assert "ask_user" in str(exc_info.value)
    assert "execute phase" in str(exc_info.value).lower() or "铁律 7" in str(exc_info.value)


def test_compile_rejects_gate_in_execute_phase():
    """compile validator 拒绝 execute phase agent 配 gate（铁律 7）。"""
    from orca.compile.validator import ConfigurationError, validate_workflow
    from orca.schema.workflow import Workflow

    wf = Workflow(
        name="bad",
        entry="a",
        nodes=[
            AgentNode(name="a", kind="agent", prompt="do", tools=["Bash", "gate"]),
        ],
    )
    with pytest.raises(ConfigurationError) as exc_info:
        validate_workflow(wf)
    assert "gate" in str(exc_info.value)


def test_compile_allows_ask_user_in_setup_phase():
    """compile validator 允许 setup phase agent 配 ask_user（setup phase 可中断）。"""
    from orca.compile.validator import validate_workflow
    from orca.schema.workflow import Workflow

    wf = Workflow(
        name="ok",
        setup=[
            AgentNode(name="collector", kind="agent", prompt="ask", tools=["ask_user"]),
        ],
        entry="a",
        nodes=[
            AgentNode(name="a", kind="agent", prompt="do"),
        ],
    )
    # 不 raise（setup phase 允许 ask_user/gate）
    validate_workflow(wf)


def test_compile_allows_tools_none_in_execute_phase():
    """compile validator 允许 execute phase agent tools=None（默认全开，runtime 把关）。"""
    from orca.compile.validator import validate_workflow
    from orca.schema.workflow import Workflow

    wf = Workflow(
        name="ok",
        entry="a",
        nodes=[
            AgentNode(name="a", kind="agent", prompt="do", tools=None),
        ],
    )
    validate_workflow(wf)  # 不 raise


# ── setup phase 结构校验（phase-10 v4 §2.7）──────────────────────────────────


def test_compile_setup_name_conflict_with_execute_rejected():
    """setup agent name 与 execute node 重名 → fail loud。"""
    from orca.compile.validator import ConfigurationError, validate_workflow
    from orca.schema.workflow import Workflow

    wf = Workflow(
        name="bad",
        setup=[AgentNode(name="shared", kind="agent", prompt="setup")],
        entry="shared",
        nodes=[AgentNode(name="shared", kind="agent", prompt="exec")],
    )
    with pytest.raises(ConfigurationError) as exc_info:
        validate_workflow(wf)
    assert "shared" in str(exc_info.value)


def test_compile_setup_routes_not_empty_rejected():
    """setup agent routes 非空 → fail loud（setup phase 无控制流）。"""
    from orca.compile.validator import ConfigurationError, validate_workflow
    from orca.schema.workflow import Route, Workflow

    wf = Workflow(
        name="bad",
        setup=[
            AgentNode(
                name="collector",
                kind="agent",
                prompt="collect",
                routes=[Route(to="$end")],
            )
        ],
        entry="a",
        nodes=[AgentNode(name="a", kind="agent", prompt="do")],
    )
    with pytest.raises(ConfigurationError) as exc_info:
        validate_workflow(wf)
    assert "routes" in str(exc_info.value).lower()


def test_compile_setup_valid_workflow_passes():
    """合法 setup workflow（agent 命名唯一 + routes 空）通过校验。"""
    from orca.compile.validator import validate_workflow
    from orca.schema.workflow import Workflow

    wf = Workflow(
        name="ok",
        setup=[
            AgentNode(name="collector", kind="agent", prompt="collect info"),
        ],
        entry="deploy",
        nodes=[
            AgentNode(name="deploy", kind="agent", prompt="deploy"),
        ],
    )
    validate_workflow(wf)  # 不 raise
