"""tests/exec/claude/test_result_extractor.py —— JSON 提取 + schema 校验（SPEC §7.6 / 计划 C.6）。

覆盖：
  - extract_json_text：纯 JSON / json fence / 第一个平衡块 / 嵌套 / 字符串内括号 / 无 JSON raise
  - extract_and_validate：schema=None 原文 / schema 通过 / schema 失败 raise ExecError(phase=schema)
"""

from __future__ import annotations

import pytest

from orca.exec.claude.result_extractor import extract_and_validate, extract_json_text
from orca.exec.error import ExecError


# ── extract_json_text ────────────────────────────────────────────────────────


def test_extract_pure_json_object():
    assert extract_json_text('{"a": 1}') == {"a": 1}


def test_extract_pure_json_array():
    assert extract_json_text("[1, 2, 3]") == [1, 2, 3]


def test_extract_json_fence():
    assert extract_json_text('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_plain_fence():
    assert extract_json_text("```\n{\"a\": 1}\n```") == {"a": 1}


def test_extract_first_balanced_block_with_prefix_suffix():
    """前缀 + JSON + 后缀：取第一个平衡块（SPEC §2.7）。"""
    assert extract_json_text('前缀 {"a": 1} 后缀') == {"a": 1}


def test_extract_nested_braces():
    assert extract_json_text('{"a": {"b": 1}}') == {"a": {"b": 1}}


def test_extract_nested_arrays():
    assert extract_json_text('{"a": [1, {"b": 2}]}') == {"a": [1, {"b": 2}]}


def test_extract_handles_braces_inside_strings():
    """字符串内的 {/} 不计入深度（SPEC §2.7 平衡块处理字符串内括号）。"""
    text = '{"desc": "has {brace} inside", "val": 1}'
    assert extract_json_text(text) == {"desc": "has {brace} inside", "val": 1}


def test_extract_handles_escaped_quotes_in_strings():
    text = '{"desc": "say \\"hi\\""}'
    assert extract_json_text(text) == {"desc": 'say "hi"'}


def test_extract_fence_with_surrounding_text():
    """整段非 JSON + 含 fence → 取 fence 内。"""
    text = 'Here is the answer:\n```json\n{"x": 42}\n```\nDone.'
    assert extract_json_text(text) == {"x": 42}


def test_extract_no_json_raises():
    with pytest.raises(ValueError):
        extract_json_text("文本无json")


def test_extract_unbalanced_raises():
    """不平衡括号（无闭合）→ raise。"""
    with pytest.raises(ValueError):
        extract_json_text('{"a": ')


# ── extract_and_validate ─────────────────────────────────────────────────────


def test_validate_schema_none_returns_raw_text():
    """schema=None → 返回原 text（自由文本，SPEC §2.7）。"""
    out = extract_and_validate("just some text", None)
    assert out == "just some text"


def test_validate_schema_passes():
    schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}
    out = extract_and_validate('{"a": 1}', schema)
    assert out == {"a": 1}


def test_validate_schema_fails_missing_required():
    schema = {"type": "object", "required": ["a"]}
    with pytest.raises(ExecError) as ei:
        extract_and_validate('{"b": 1}', schema)
    assert ei.value.phase == "schema"
    assert ei.value.error_type == "SchemaValidationError"
    assert "a" in ei.value.message  # 缺 required 字段 a


def test_validate_schema_fails_wrong_type():
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    with pytest.raises(ExecError, match="schema"):
        extract_and_validate('{"a": "not int"}', schema)


def test_validate_non_json_text_with_schema_raises_schema():
    """schema 非 None 但 result 文本无合法 JSON → ExecError(phase=schema)（fail loud）。"""
    with pytest.raises(ExecError) as ei:
        extract_and_validate("no json here", {"type": "object"})
    assert ei.value.phase == "schema"


def test_validate_extracts_from_fence_then_validates():
    """result 文本是 ```json fence``` → 提取 + 校验一气呵成。"""
    schema = {"type": "object", "required": ["result"]}
    text = '```json\n{"result": "DONE"}\n```'
    out = extract_and_validate(text, schema)
    assert out == {"result": "DONE"}


def test_validate_array_schema():
    schema = {"type": "array", "items": {"type": "integer"}}
    assert extract_and_validate("[1, 2, 3]", schema) == [1, 2, 3]


def test_validate_array_schema_wrong_item_type_raises():
    schema = {"type": "array", "items": {"type": "integer"}}
    with pytest.raises(ExecError, match="schema"):
        extract_and_validate("[1, \"two\", 3]", schema)
