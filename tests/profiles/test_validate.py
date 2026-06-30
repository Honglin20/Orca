"""tests/profiles/test_validate.py —— validate_workflow_profiles 四条规则各覆盖。

覆盖 SPEC §6.6 / §4.9：
  ① get_profile(executor) 失败 → error
  ② output_schema 非 None 且 structured_output=='none' → error
  ③ foreach body 是 AgentNode 且 concurrent_safe==False → error
  ④ streaming_events==False → warning

规则仅基于 AgentNode 真实字段（executor/output_schema/foreach body），不自创字段。
"""

from __future__ import annotations

import pytest

import orca.profiles.registry as reg
from orca.profiles import (
    CliProfile,
    ProviderCapabilities,
    register,
    validate_workflow_profiles,
)
from orca.profiles.validate import ProfileIssue
from orca.schema import AgentNode, ForeachNode, Workflow


@pytest.fixture(autouse=True)
def _reset_registry():
    reg._reset_for_test()
    yield
    reg._reset_for_test()


# ── helpers ──────────────────────────────────────────────────────────────────


def _wf(nodes: list, *, entry: str = "a") -> Workflow:
    return Workflow(name="w", entry=entry, nodes=nodes, outputs={})


def _profile(name: str, **cap_overrides) -> CliProfile:
    cap_kw = dict(
        mcp_tools=True, streaming_events=True, structured_output="native",
        interrupt=True, checkpoint_resume=True, usage_tracking=True,
        concurrent_safe=True,
    )
    cap_kw.update(cap_overrides)
    return CliProfile(
        name=name,
        capabilities=ProviderCapabilities(**cap_kw),
        cli_path_env=f"ORCA_{name.upper()}_CLI",
        default_cli_path=name,
        flags=(),
        prompt_channel="stdin",
        mcp_flag_template=None,
        env_overlay_prefixes=(),
        stream_format="text",
        translator=lambda l, s: [],
        result_extractor=lambda r: r,
    )


def _issues(wf: Workflow) -> list[ProfileIssue]:
    return validate_workflow_profiles(wf)


# ── 合法 workflow 无 issue ───────────────────────────────────────────────────


def test_valid_claude_agent_no_issues():
    register(_profile("claude"))
    wf = _wf([AgentNode(name="a", prompt="p", executor="claude", routes=[{"to": "$end"}])])
    assert _issues(wf) == []


# ── 规则 ① 未知 executor → error ─────────────────────────────────────────────


def test_rule1_unknown_executor_is_error():
    register(_profile("claude"))
    wf = _wf([AgentNode(name="a", prompt="p", executor="ghost", routes=[{"to": "$end"}])])
    issues = _issues(wf)
    errors = [i for i in issues if i.severity == "error"]
    assert len(errors) == 1
    assert errors[0].node == "a"
    assert "ghost" in errors[0].message


def test_rule1_unknown_executor_skips_subsequent_rules():
    """规则①触发后，后续规则（依赖 profile）不再判（不重复报）。"""
    register(_profile("claude"))
    wf = _wf([
        AgentNode(name="a", prompt="p", executor="ghost",
                  output_schema={"type": "object"}, routes=[{"to": "$end"}]),
    ])
    issues = _issues(wf)
    # 只有规则①的 1 个 error（不会因 output_schema 再加 error）
    assert len([i for i in issues if i.severity == "error"]) == 1


# ── 规则 ② output_schema + structured_output=none → error ────────────────────


def test_rule2_output_schema_with_none_structured_is_error():
    register(_profile("nostruct", structured_output="none"))
    wf = _wf([
        AgentNode(name="a", prompt="p", executor="nostruct",
                  output_schema={"type": "object"}, routes=[{"to": "$end"}]),
    ])
    issues = _issues(wf)
    errors = [i for i in issues if i.severity == "error"]
    assert len(errors) == 1
    assert "不支持结构化输出" in errors[0].message


def test_rule2_output_schema_with_native_is_ok():
    """structured_output=native 时 output_schema 合法（不报）。"""
    register(_profile("okstruct", structured_output="native"))
    wf = _wf([
        AgentNode(name="a", prompt="p", executor="okstruct",
                  output_schema={"type": "object"}, routes=[{"to": "$end"}]),
    ])
    assert _issues(wf) == []


def test_rule2_output_schema_with_prompt_injection_is_ok():
    """structured_output=prompt_injection 仍能产出结构化输出（不报，SPEC §4.9）。"""
    register(_profile("inj", structured_output="prompt_injection"))
    wf = _wf([
        AgentNode(name="a", prompt="p", executor="inj",
                  output_schema={"type": "object"}, routes=[{"to": "$end"}]),
    ])
    assert _issues(wf) == []


# ── 规则 ③ foreach body + 非 concurrent_safe → error ─────────────────────────


def test_rule3_foreach_body_non_concurrent_is_error():
    """foreach body 是 AgentNode 且 concurrent_safe==False → error（SPEC §4.9 规则③）。"""
    register(_profile("serial", concurrent_safe=False))
    wf = _wf([
        ForeachNode(
            name="fan",
            source="upstream.output.items",
            body=AgentNode(name="", prompt="p", executor="serial"),
            routes=[{"to": "$end"}],
        ),
    ])
    issues = _issues(wf)
    errors = [i for i in issues if i.severity == "error"]
    # body 的规则①（executor 存在）通过，规则③（非并发）触发
    concurrent_errors = [e for e in errors if "不可并行" in e.message]
    assert len(concurrent_errors) == 1
    assert concurrent_errors[0].node == "fan.body"


def test_rule3_foreach_body_concurrent_safe_is_ok():
    register(_profile("par", concurrent_safe=True))
    wf = _wf([
        ForeachNode(
            name="fan",
            source="upstream.output.items",
            body=AgentNode(name="", prompt="p", executor="par"),
            routes=[{"to": "$end"}],
        ),
    ])
    issues = _issues(wf)
    assert [i for i in issues if i.severity == "error"] == []


def test_rule3_foreach_body_unknown_executor_no_double_report():
    """foreach body executor 未知：规则③ 早退（不重复报），仅规则①报 1 个 error。

    防止「body executor 未知」同时触发规则①和规则③的重复 error。
    """
    register(_profile("claude"))  # 仅 claude 可用
    wf = _wf([
        ForeachNode(
            name="fan",
            source="up.output.items",
            body=AgentNode(name="", prompt="p", executor="ghost"),  # 未知
            routes=[{"to": "$end"}],
        ),
    ])
    issues = _issues(wf)
    errors = [i for i in issues if i.severity == "error"]
    # 仅规则①的 1 个 error（body executor 未知），不因规则③再报「不可并行」
    assert len(errors) == 1
    assert "ghost" in errors[0].message
    assert all("不可并行" not in e.message for e in errors)


def test_rule3_foreach_body_script_not_checked():
    """foreach body 是 ScriptNode 时不判规则③（仅 AgentNode 有 executor，SPEC §4.9）。"""
    from orca.schema import ScriptNode

    register(_profile("claude"))
    wf = _wf([
        ForeachNode(
            name="fan",
            source="upstream.output.items",
            body=ScriptNode(name="", command="echo {{ item }}"),
            routes=[{"to": "$end"}],
        ),
    ])
    assert _issues(wf) == []


# ── 规则 ④ streaming_events=False → warning ──────────────────────────────────


def test_rule4_no_streaming_events_is_warning():
    """streaming_events=False → warning（前端 live 观测降级，不阻止，SPEC §4.9 规则④）。"""
    register(_profile("nosream", streaming_events=False))
    wf = _wf([
        AgentNode(name="a", prompt="p", executor="nosream", routes=[{"to": "$end"}]),
    ])
    issues = _issues(wf)
    warnings = [i for i in issues if i.severity == "warning"]
    assert len(warnings) == 1
    assert "不产出结构化流事件" in warnings[0].message
    assert [i for i in issues if i.severity == "error"] == []  # 仅 warning


# ── 多 node 聚合 ─────────────────────────────────────────────────────────────


def test_multiple_nodes_aggregate_issues():
    """多个 node 的 issue 聚合返回（compile 层会一次报全）。"""
    register(_profile("claude"))
    register(_profile("nostruct", structured_output="none"))
    wf = _wf([
        AgentNode(name="a", prompt="p", executor="ghost", routes=[{"to": "b"}]),
        AgentNode(name="b", prompt="p", executor="nostruct",
                  output_schema={"type": "object"}, routes=[{"to": "$end"}]),
    ])
    issues = _issues(wf)
    nodes = {i.node for i in issues}
    assert nodes == {"a", "b"}


# ── ProfileIssue dataclass ───────────────────────────────────────────────────


def test_profile_issue_is_frozen_dataclass():
    """ProfileIssue frozen：不可变（错误定位用，不应被改）。"""
    import dataclasses

    issue = ProfileIssue(node="a", severity="error", message="x")
    assert dataclasses.is_dataclass(issue)
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        issue.node = "b"  # type: ignore[misc]
