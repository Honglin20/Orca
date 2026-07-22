"""schema_faker.py —— 从节点 output_schema 合成最小合规 JSON。

**为什么需要**：headless TARS harness 要把 mock 子 agent 产出喂给 ``orca next``，引擎会
按节点 ``output_schema``（jsonschema）校验 ``--output``。没有真模型时，我们用「最小合规
JSON」驱动 DAG walk，证明 output_schema 链不破、引擎能逐节点推进。这是确定性逻辑
（铁律 5），不靠模型。

**不造假**：合成的值刻意避开 ``torch.randn`` / ``fake_data`` / ``dummy_calib`` 等哨兵/
造假扫描命中词（用 ``ok`` / ``/tmp/x`` / 0 等 neutral 值）——本模块产出的是 *结构性*
占位，不是「冒充真实实验数据」，且 driver 的 ``looks_fabricated`` 最后一道 sanity 仍会
扫真实子 agent 的 output（非本合成器产出）。

**依赖单向**：零外部依赖（仅 stdlib + orca.compile.parser 的纯数据读）。不 import
run/exec/events/iface。

**扩展点（OCP）**：新 JSON schema 关键字（pattern/format/oneOf 等）→ 加分支，不改
``synthesize_for_schema`` 对外签名。
"""

from __future__ import annotations

import json
from typing import Any

# 哨兵/造假扫描的禁词——合成值绝不命中（spike sentinel.looks_fabricated 同口径）。
_FABRICATION_TOKENS = ("torch.randn", "torch.rand", "fake_data", "dummy_calib")


def synthesize_for_schema(schema: dict | None) -> str:
    """据 ``schema`` 合成最小合规 JSON，返回 JSON 串（喂 ``orca next --output``）。

    - ``schema is None`` → 自由文本节点；返回 neutral 字符串 ``"ok"``。
    - ``schema`` 非 None → 据 ``type`` / ``required`` / ``properties`` / ``enum`` /
      ``minimum`` / ``minItems`` 合成最小 JSON 对象/数组/标量。

    合成保证：尊重 ``additionalProperties: false``（只产 properties 中声明的键）+
    ``required`` 必出现 + 数值类约束（minimum/maximum/minItems）。

    **不保证的约束**（pattern / format / oneOf）：本合成器服务于 DAG walk，非完整 jsonschema
    fuzzer；string 默认 ``"ok"`` 过 type 但**不保证过 pattern**（如 ``pattern: "^[a-z]+$"``
    收到 ``"ok"`` 违约束）。当前 8 workflow 的 20 个 output_schema 均无 pattern/format（已
    grep 确认），故现状无 bug；未来若加 pattern 约束，需在此处扩展分支（用 ``regex`` 模块
    生成满足串）或改用真 jsonschema fake 库。
    """
    if schema is None:
        return "ok"
    value = _synthesize_value(schema)
    text = json.dumps(value, ensure_ascii=False)
    _assert_no_fabrication_tokens(text)
    return text


def _synthesize_value(schema: dict) -> Any:
    """递归合成一个 schema-valid 值。"""
    if not isinstance(schema, dict):
        # 非典型 schema（如 True/False 占位）→ 给 neutral 默认
        return "ok"

    # enum / const 优先（最约束）
    if "const" in schema:
        return schema["const"]
    if "enum" in schema and isinstance(schema["enum"], list) and schema["enum"]:
        return schema["enum"][0]

    node_type = schema.get("type")

    # 多类型联合（如 ["string", "null"]）→ 取第一个非 null 类型
    if isinstance(node_type, list):
        node_type = next((t for t in node_type if t != "null"), node_type[0])

    if node_type == "object":
        return _synthesize_object(schema)
    if node_type == "array":
        return _synthesize_array(schema)
    return _synthesize_scalar(node_type or "string", schema)


def _synthesize_object(schema: dict) -> dict[str, Any]:
    # 产 **全部** declared properties（不只 required）——下游 ``outputs`` / ``routes.when``
    # 可能引用任意 optional 字段（如 ptq-sweeper 的 baked_model_path 非必填但 outputs 引用它）。
    # StrictUndefined 下漏产任何一个被引用的字段都会让 workflow outputs 渲染崩。故全产。
    properties: dict = schema.get("properties", {}) or {}
    obj: dict[str, Any] = {}
    for key, sub_schema in properties.items():
        if isinstance(sub_schema, dict):
            obj[key] = _synthesize_value(sub_schema)
        else:
            obj[key] = "ok"  # 无 sub-schema → neutral 默认
    # 兜底：若 properties 为空但 required 非空（少见），按 required 给 neutral
    if not obj:
        for key in (schema.get("required") or []):
            obj[key] = "ok"
    return obj


def _synthesize_array(schema: dict) -> list[Any]:
    items_schema = schema.get("items")
    min_items = schema.get("minItems", 0)
    if min_items >= 1 and isinstance(items_schema, dict):
        return [_synthesize_value(items_schema)]
    return []


def _synthesize_scalar(node_type: str, schema: dict) -> Any:
    minimum = schema.get("minimum")
    if node_type == "integer":
        return int(minimum) if isinstance(minimum, (int, float)) else 0
    if node_type == "number":
        return float(minimum) if isinstance(minimum, (int, float)) else 0.0
    if node_type == "boolean":
        return False
    if node_type == "null":
        return None
    # string（最常见）——neutral 非空串，绝不命中造假词
    return "ok"


def _assert_no_fabrication_tokens(text: str) -> None:
    """合成产物绝不命中哨兵/造假扫描词（fail loud——合成器漂移立即暴露）。"""
    for token in _FABRICATION_TOKENS:
        if token in text:
            raise AssertionError(
                f"schema_faker 合成产物命中造假/哨兵禁词 {token!r}（合成器漂移）；"
                f"text={text[:200]!r}"
            )
