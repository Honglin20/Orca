"""test_schema_faker.py —— schema_faker 的函数边界单测（Rule 9：验证合成**意图**）。

walk/sentinel E2E 只间接覆盖 faker（经真 workflow）；本文件直接钉死每个分支的合成行为：
object 全 properties / array+minItems / scalar+minimum / enum / const / 多类型联合 /
None schema → 自由文本 / 嵌套 object / 空_properties+required fallback / 造假词不变式。
"""

from __future__ import annotations

import json

import pytest

from tests.e2e_redesign.schema_faker import synthesize_for_schema


def test_none_schema_returns_free_text_ok() -> None:
    """output_schema=None = 自由文本节点 → 返 neutral ``"ok"``。"""
    assert synthesize_for_schema(None) == "ok"


def test_object_synthesizes_all_declared_properties() -> None:
    """object 必产 **全部** properties（不只 required）——StrictUndefined 下漏产被引用的
    optional 字段会让 workflow outputs 渲染崩（ptq-sweeper baked_model_path 即此例）。"""
    schema = {
        "type": "object",
        "required": ["a"],
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "string"},   # optional 但仍产出
            "c": {"type": "integer"},
        },
        "additionalProperties": False,
    }
    out = json.loads(synthesize_for_schema(schema))
    assert set(out.keys()) == {"a", "b", "c"}, f"应产全部 properties，实际 {set(out.keys())}"


def test_object_respects_minimum_on_numeric() -> None:
    """minimum 约束被尊重（integer/number 至少 minimum）。"""
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "minimum": 5},
            "score": {"type": "number", "minimum": 0.1},
        },
    }
    out = json.loads(synthesize_for_schema(schema))
    assert out["count"] == 5
    assert out["score"] == 0.1


def test_array_empty_by_default() -> None:
    """array 默认空（minItems=0）。"""
    schema = {"type": "array", "items": {"type": "string"}}
    assert json.loads(synthesize_for_schema(schema)) == []


def test_array_min_items_one_synthesizes_item() -> None:
    """minItems>=1 → 产 1 个 item（递归合成 items schema）。"""
    schema = {"type": "array", "items": {"type": "integer"}, "minItems": 1}
    out = json.loads(synthesize_for_schema(schema))
    assert len(out) == 1 and isinstance(out[0], int)


def test_enum_returns_first_value() -> None:
    """enum → 取第一个（最约束的合法值之一）。"""
    schema = {"type": "string", "enum": ["alpha", "beta"]}
    assert json.loads(synthesize_for_schema(schema)) == "alpha"


def test_const_returned_verbatim() -> None:
    """const 优先于 type。"""
    schema = {"type": "string", "const": "fixed-value"}
    assert synthesize_for_schema(schema) == '"fixed-value"'


def test_multi_type_union_picks_first_non_null() -> None:
    """多类型联合（如 ['string','null']）→ 取首个非 null 类型。"""
    schema = {"type": ["null", "string"]}  # null 在前应跳过
    out = synthesize_for_schema(schema)
    # 应是 string 的 neutral 默认（"ok"），非 null
    assert out == '"ok"'


def test_boolean_false() -> None:
    schema = {"type": "boolean"}
    assert json.loads(synthesize_for_schema(schema)) is False


def test_nested_object() -> None:
    """嵌套 object 递归合成。"""
    schema = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {"inner": {"type": "integer", "minimum": 1}},
            },
        },
    }
    out = json.loads(synthesize_for_schema(schema))
    assert out == {"outer": {"inner": 1}}


def test_empty_properties_with_required_fallback() -> None:
    """properties 空 + required 非空 → 按 required 给 neutral（兜底分支）。"""
    schema = {"type": "object", "required": ["x", "y"]}
    out = json.loads(synthesize_for_schema(schema))
    assert out == {"x": "ok", "y": "ok"}


def test_non_dict_schema_returns_neutral() -> None:
    """非典型 schema（如 True 占位 = jsonschema「accept anything」）→ 退到 string neutral，
    返合法 JSON 字符串 ``"\"ok\""``（json.dumps("ok")）。不崩、过 type 校验。"""
    assert synthesize_for_schema(True) == '"ok"'  # type: ignore[arg-type]


def test_synthesized_output_never_hits_fabrication_tokens() -> None:
    """合成产物绝不命中哨兵/造假扫描词（fail loud——合成器漂移立即暴露）。"""
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "string", "const": "fake_data_should_not_appear"},
        },
    }
    # const 是 "fake_data_should_not_appear" → 含 fake_data 子串 → assert raise
    with pytest.raises(AssertionError, match="fake_data"):
        synthesize_for_schema(schema)
