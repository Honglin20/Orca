"""tests/compile/test_validate_profiles.py —— compile _check_profiles 集成 + phase 2 不回归。

覆盖 SPEC §6.7 / §6.8：
  - validate_workflow 追加 _check_profiles（第 ⑨ 项），issue 正确汇入 ValidationResult
  - capability error 阻止 workflow（随 ConfigurationError 抛出）；warning 不阻止
  - 与 phase 2 的 8 项校验共存不回归（聚合一次报全）
  - 端到端：ccr + output_schema 不报 error（prompt_injection 仍支持结构化输出）

注：compile 单向依赖 profiles（``from orca.profiles import validate_workflow_profiles``）。
registry 全局状态需重置隔离。
"""

from __future__ import annotations

import pytest

import orca.profiles.registry as reg
from orca.compile import ConfigurationError
from orca.compile.validator import validate_workflow
from orca.profiles import (
    CliProfile,
    ProviderCapabilities,
    register,
)
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


def _errors(wf: Workflow) -> list[str]:
    with pytest.raises(ConfigurationError) as exc:
        validate_workflow(wf)
    return exc.value.errors


# ── capability error 阻止 workflow ───────────────────────────────────────────


def test_unknown_executor_blocks_workflow():
    """规则① error：未知 executor 随 ConfigurationError 抛出（SPEC §6.7）。"""
    register(_profile("claude"))
    wf = _wf([AgentNode(name="a", prompt="p", executor="ghost", routes=[{"to": "$end"}])])
    errs = _errors(wf)
    assert any("ghost" in e and "不可用" in e for e in errs)


def test_output_schema_none_structured_blocks_workflow():
    """规则② error：output_schema + structured_output=none 阻止 workflow。"""
    register(_profile("nostruct", structured_output="none"))
    wf = _wf([
        AgentNode(name="a", prompt="p", executor="nostruct",
                  output_schema={"type": "object"}, routes=[{"to": "$end"}]),
    ])
    errs = _errors(wf)
    assert any("不支持结构化输出" in e for e in errs)


def test_foreach_non_concurrent_blocks_workflow():
    """规则③ error：foreach body 非 concurrent_safe 阻止 workflow。"""
    register(_profile("serial", concurrent_safe=False))
    wf = _wf([
        ForeachNode(
            name="fan", source="up.output.items",
            body=AgentNode(name="", prompt="p", executor="serial"),
            routes=[{"to": "$end"}],
        ),
    ])
    errs = _errors(wf)
    assert any("不可并行" in e for e in errs)


# ── warning 不阻止 workflow ──────────────────────────────────────────────────


def test_streaming_events_warning_does_not_block():
    """规则④ warning：streaming_events=False 不阻止，仅在 warnings 中返回。"""
    register(_profile("nosream", streaming_events=False))
    wf = _wf([
        AgentNode(name="a", prompt="p", executor="nosream", routes=[{"to": "$end"}]),
    ])
    warnings = validate_workflow(wf)  # 不抛，返回 warnings
    assert any("不产出结构化流事件" in w for w in warnings)


# ── 合法 workflow 放行 ───────────────────────────────────────────────────────


def test_valid_claude_workflow_passes():
    register(_profile("claude"))
    wf = _wf([AgentNode(name="a", prompt="p", executor="claude", routes=[{"to": "$end"}])])
    assert validate_workflow(wf) == []


# ── 端到端：ccr + output_schema（prompt_injection）不报 error ─────────────────


def test_ccr_with_output_schema_prompt_injection_not_error():
    """端到端（SPEC §6.8）：ccr (prompt_injection) + output_schema 不报 error。

    ccr 的 structured_output=prompt_injection（非 native 也非 none），仍能产出结构化输出，
    故规则②不触发。这是 capability 闭环的关键验证：validate 精确区分 none vs prompt_injection。
    """
    # 直接用 builtin ccr（lazy load）
    from orca.profiles import get_profile
    get_profile("ccr")  # 触发 builtin 加载
    wf = _wf([
        AgentNode(name="a", prompt="p", executor="ccr",
                  output_schema={"type": "object"}, routes=[{"to": "$end"}]),
    ])
    # 不应抛（ccr 支持 prompt_injection 结构化输出）
    warnings = validate_workflow(wf)
    assert all("不支持结构化输出" not in w for w in warnings)


# ── 与 phase 2 共存（聚合一次报全）────────────────────────────────────────────


def test_phase2_and_capability_errors_aggregate():
    """phase 2 结构错误 + capability 错误共存，一次报全（聚合，SPEC §6.7）。

    验证：① 环（phase 2 ⑤）+ 未知 executor（⑨）同时出现时，两者都在 errors 里。
    """
    register(_profile("claude"))
    wf = _wf([
        AgentNode(name="a", prompt="p", executor="ghost", after=["b"],
                  routes=[{"to": "$end"}]),
        AgentNode(name="b", prompt="p", executor="claude", after=["a"],
                  routes=[{"to": "$end"}]),
    ])
    errs = _errors(wf)
    # 既有 phase 2 的环错误，也有 capability 的 unknown executor 错误
    assert any("环" in e or "依赖" in e for e in errs), f"应含 phase 2 环错误：{errs}"
    assert any("ghost" in e for e in errs), f"应含 capability 错误：{errs}"


def test_phase2_checks_still_run():
    """phase 2 的 8 项校验仍正常工作（_check_profiles 不影响它们，SPEC §6.7 不回归）。"""
    register(_profile("claude"))
    # entry 不存在（phase 2 ②）
    wf = _wf([
        AgentNode(name="a", prompt="p", executor="claude", routes=[{"to": "$end"}]),
    ], entry="nonexistent")
    errs = _errors(wf)
    assert any("entry" in e for e in errs)
