"""test_workflow_contracts.py —— 8 workflow 静态契约（parametrized，不驱动引擎）。

每条 check 对应任务 §1 结构/契约验证清单的一项；Finding 列表为空 = pass。本文件不启动
任何 orca run（那是 ``test_tars_harness_walk.py`` 的事），纯静态，快、确定性。

测试验证**意图**（Rule 9）：不是「yaml 能解析」，而是「P5/P6/P9 收敛后的契约逐项成立」
——已删 input 不被残留引用 / output_schema 链不破 / chart 有标签 / 无造假兜底。
"""

from __future__ import annotations

import pytest

from tests.e2e_redesign.contract import (
    WORKFLOWS,
    all_checks,
    check_chart_labels,
    check_fabrication_prohibition_present,
    check_hardware_inputs_present,
    check_no_fabrication,
    check_no_undeclared_input_refs,
    check_output_schema_chain,
    load_parsed,
)

# 8 workflow（sorted 稳定输出顺序，失败定位更快）。
ALL_WF = sorted(WORKFLOWS.keys())


@pytest.mark.parametrize("wf_name", ALL_WF)
def test_workflow_compiles(wf_name: str) -> None:
    """check 1：load_workflow 成功（compile validator 内嵌：节点图合法 + inputs 解析）。"""
    wf = load_parsed(wf_name)
    assert wf.name == wf_name
    assert wf.entry, f"{wf_name} 缺 entry"
    assert len(wf.nodes) >= 1, f"{wf_name} 无节点"


@pytest.mark.parametrize("wf_name", ALL_WF)
def test_no_undeclared_input_refs(wf_name: str) -> None:
    """check 3：无 ``{{ inputs.X }}`` 残留引用已删 input。"""
    findings = check_no_undeclared_input_refs(wf_name)
    assert not findings, _fmt(findings)


@pytest.mark.parametrize("wf_name", ALL_WF)
def test_hardware_inputs_present(wf_name: str) -> None:
    """check 4：device/target_hardware/seed input 在（P5/P6/P9 设备契约）。"""
    findings = check_hardware_inputs_present(wf_name)
    assert not findings, _fmt(findings)


@pytest.mark.parametrize("wf_name", ALL_WF)
def test_output_schema_chain(wf_name: str) -> None:
    """check 2：``{{ X.output.Y }}`` 的 Y 必在 X 的 output_schema properties（链不破）。"""
    findings = check_output_schema_chain(wf_name)
    assert not findings, _fmt(findings)


@pytest.mark.parametrize("wf_name", ALL_WF)
def test_chart_labels(wf_name: str) -> None:
    """check 5：chart 推图调用有 x_label/y_label/caption（table 只要求 caption）。"""
    findings = check_chart_labels(wf_name)
    assert not findings, _fmt(findings)


@pytest.mark.parametrize("wf_name", ALL_WF)
def test_no_fabrication(wf_name: str) -> None:
    """check 6：active-path 脚本无造假兜底（上下文感知：fake_data/dummy_calib 零容忍；
    torch.randn 仅在非 smoke/dummy/proxy 上下文才 finding）。"""
    findings = check_no_fabrication(wf_name)
    assert not findings, _fmt(findings)


@pytest.mark.parametrize("wf_name", ALL_WF)
def test_fabrication_prohibition_present(wf_name: str) -> None:
    """check 7：quant/nas/struct-kd 系 agent.md 必含「严禁造假」prohibition（prompt guard）。"""
    findings = check_fabrication_prohibition_present(wf_name)
    assert not findings, _fmt(findings)


@pytest.mark.parametrize("wf_name", ALL_WF)
def test_all_checks_aggregate(wf_name: str) -> None:
    """聚合闸：任一静态契约违例 → 红（给单条 check 已定位时这里是总览）。"""
    findings = all_checks(wf_name)
    assert not findings, _fmt(findings)


def _fmt(findings) -> str:
    """把 Finding 列表格式化成可读断言消息。"""
    lines = ["静态契约违例："]
    for f in findings:
        lines.append(f"  [{f.check}] {f.location}: {f.detail}")
    return "\n".join(lines)
