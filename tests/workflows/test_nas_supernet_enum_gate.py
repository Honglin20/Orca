"""tests/workflows/test_nas_supernet_enum_gate.py —— 三版本 nas-supernet yaml enum 闸门。

SPEC 2026-08-11-inputdef-enum §3.6 / §5 AC7：``latency_unit`` 输入必须声明 ``enum: [ms, us, s]``，
让 bootstrap 期抓笔误值（``"MS"`` / ``"foo"``）——而非靠 description 文本（非机器可校验）。

测试形态选择（Rule 9：测意图非行为）：不走 grep——grep 只校「字串在」，不校「schema 真解析 +
InputDef.enum 真存储 + default∈enum 自洽」。本测试走真路径：``yaml.safe_load`` →
``Workflow(**data)``（schema 层 3 validator 全跑），assert ``wf.inputs["latency_unit"].enum``
真值。这同时是 schema 3 validator（空 list / 标量-only / default∈enum）的端到端落地震。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from orca.schema import Workflow

_REPO = Path(__file__).resolve().parents[2]
_YAMLS = [
    _REPO / "workflows" / "nas-supernet" / "workflow.yaml",
    _REPO / "workflows" / "nas-supernet-v2" / "workflow.yaml",
    _REPO / "workflows" / "nas-supernet-v3" / "workflow.yaml",
]


def test_nas_supernet_latency_unit_has_enum():
    """AC7：三版本 ``latency_unit`` 都声明 ``enum: [ms, us, s]`` 且 schema 真解析。

    端到端：yaml → Workflow 构造（schema 层 3 validator 跑过：空 list 拒 / 标量-only /
    default∈enum）→ ``wf.inputs["latency_unit"].enum`` 真值 = ``["ms", "us", "s"]``。
    若任一 yaml 漏 enum 字段 / 写错值集 / default 不在 enum 内 → 本测试 fail。
    """
    for yaml_path in _YAMLS:
        with yaml_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        wf = Workflow(**data)  # schema 层 3 validator 跑（漏 / 错即 ValidationError）
        idef = wf.inputs["latency_unit"]
        assert idef.enum == ["ms", "us", "s"], (
            f"{yaml_path.name}: latency_unit.enum 应为 ['ms','us','s']，实际 {idef.enum!r}"
        )
        # default∈enum 自洽（schema model_validator 已校；这里再 assert 锁定契约）。
        assert idef.default == "ms"
        assert idef.default in idef.enum


def test_nas_supernet_latency_unit_yaml_has_enum_literal():
    """AC7 grep 闸门（字串层）：三版本 yaml 原文含 ``enum: [ms, us, s]`` 字串。

    补充上一测试的字串层守门：防 yaml 被改回不带 enum 字段（schema 解析仍过，但 enum=None
    不再约束 = 行为退化）。两测试叠加锁语义 + 字串双契。
    """
    for yaml_path in _YAMLS:
        text = yaml_path.read_text(encoding="utf-8")
        # ``enum: [ms, us, s]`` 是 latency_unit 块下的字面量（YAML flow list）。
        # 用 "ms, us, s" 子串验（容忍空格 / quote 风格差异）。
        assert "enum: [ms, us, s]" in text, (
            f"{yaml_path.name}: 缺 ``enum: [ms, us, s]`` 字面量（latency_unit 失去 enum 约束）"
        )
