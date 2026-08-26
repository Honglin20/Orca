"""test_p9a_input_contract.py —— 锁定 6 workflow 的 input key 集合（SPEC §5 契约测试）。

P9a 按 [input 三档原则](docs/specs/workflow-input-design-principle.md) §5 收敛 quant(4)+NAS(2)
的 inputs：Tier C（算法开关/工程路径）固化进脚本默认或 $ORCA_ARTIFACTS_DIR，Tier B（loader/
project_root）下沉给 agent 推断 + 哨兵，只留 Tier A（模型入口/KPI/硬件/种子）作 [ask] input。

本测试把每个 workflow 的 input key 集合钉死成 SPEC §5 目标——未来改动若静默把已删 input 加回 yaml
（或误删一个 Tier A），这里立即红。这是「测试验证意图（Rule 9）」：验证 SPEC §5 的收敛意图，
而非仅行为。

Jinja2 StrictUndefined 运行时不崩溃的验证由 exec/render 层覆盖；本测试只锁 input 契约。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orca.compile.parser import load_workflow

REPO = Path(__file__).resolve().parents[2]
WF_DIR = REPO / "workflows"

# SPEC §5 目标 input 集合（P9a 收敛后）。
# 任何改动这些集合的 PR 必须同时更新 docs/specs/workflow-input-design-principle.md §5 + 本表，
# 并在 release note 说明为何调整 Tier 归类。
EXPECTED_INPUTS = {
    "quant-ptq-sweep.yaml": {
        "model_path",        # [ask] 模型入口（Tier A）
        "target_hardware",   # [ask] 目标硬件（Tier A）
        "seed",              # [default] 复现性种子（Tier A，默认 0）
    },
    "quant-sensitivity.yaml": {
        "model_path", "target_hardware", "seed",
    },
    "quant-qat.yaml": {
        "model_path", "target_hardware", "seed",
    },
    # bit-curve 额外保留 Tier A KPI / 预算闸门（SPEC §5：accuracy_tolerance/avg_bit_budget/max_evals）。
    "quant-bit-curve.yaml": {
        "model_path", "target_hardware", "seed",
        "accuracy_tolerance",  # [ask] 精度损失容忍（Pareto 选点闸门）
        "avg_bit_budget",      # [ask] 平均位宽硬上限
        "max_evals",           # [ask] 主搜索 candidate 预算
    },
    # NAS：P6 已补 4 个 KPI + 下沉 project_root；P9a 再下沉 output_dir（→ $ORCA_ARTIFACTS_DIR）。
    "nas-agent-pipeline.yaml": {
        "model_path", "target_hardware", "latency_constraint", "max_rounds", "seed",
    },
    "nas-hp-search.yaml": {
        "model_path", "target_hardware", "latency_constraint", "max_rounds", "seed",
    },
}

# Tier C / Tier B 已下沉项——必须不在 inputs 里（防回潮）。SPEC §5 REMOVE 清单。
FORBIDDEN_INPUTS = {
    "project_root",        # Tier B：agent infer-once（NAS P6 / quant P9a）
    "calib_data_ref",      # Tier B：agent 读代码 + 哨兵
    "eval_data_ref",       # Tier B
    "train_data_ref",      # Tier B
    "eval_fn_ref",         # Tier B
    "output_dir",          # Tier C：$ORCA_ARTIFACTS_DIR（P8 接口）
    "mode", "bit_width", "bit_widths", "recipes", "scheme", "cage",
    "method", "ratio", "low_bits", "high_bits",
    "candidate_format_space", "bit_objective", "granularity",
    "bake",                # Tier C 算法开关 / 预设
}


@pytest.mark.parametrize("wf_name,expected", sorted(EXPECTED_INPUTS.items()))
def test_input_keys_match_spec(wf_name: str, expected: set[str]):
    """每个 workflow 的 input key 集合必须逐字等于 SPEC §5 目标集合。"""
    wf = load_workflow(WF_DIR / wf_name)
    actual = set((wf.inputs or {}).keys())
    assert actual == expected, (
        f"{wf_name} input 集合偏离 SPEC §5。\n"
        f"  缺失（应保留却没了）: {sorted(expected - actual)}\n"
        f"  多余（应下沉却还在）: {sorted(actual - expected)}\n"
        f"  若是有意调整，请同步更新 docs/specs/workflow-input-design-principle.md §5 + 本测试 EXPECTED_INPUTS。"
    )


@pytest.mark.parametrize("wf_name", sorted(EXPECTED_INPUTS))
def test_no_forbidden_tier_bc_inputs(wf_name: str):
    """已下沉的 Tier B/C 项绝不能回潮成 input（防静默回退）。"""
    wf = load_workflow(WF_DIR / wf_name)
    actual = set((wf.inputs or {}).keys())
    leaked = actual & FORBIDDEN_INPUTS
    assert not leaked, (
        f"{wf_name} 含已下沉的 Tier B/C input {sorted(leaked)}（SPEC §5 要求移除）。"
    )
