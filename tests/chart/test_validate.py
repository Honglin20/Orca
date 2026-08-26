"""tests/chart/test_validate.py —— ChartPayload 校验（phase-13 SPEC §7.2）。

覆盖意图（非仅行为）：
  - 合法 payload 通过（line/bar/area/scatter/pareto/radar/table/heatmap 8 种各 1 例）
  - chart_type 未知 → raise（防 client 拼错字 → 落 tape 脏数据）
  - data 非 list → raise（防传 dict / 单行）
  - label/title 空或非 str → raise（dedup 维度）
  - pareto_direction 非 max/min → raise（types.ts 契约）
  - columns 非 list[str] → raise
  - x/y/hue 非法类型 → raise
  - heatmap 缺 value / value 非法类型 → raise（fail loud）
"""

from __future__ import annotations

import pytest

from orca.chart._validate import validate_payload


def test_validate_line_minimal_passes():
    """合法 line payload（最小）通过校验。"""
    validate_payload({
        "chart_type": "line",
        "data": [{"x": 1, "y": 2}],
        "label": "g1",
        "title": "t1",
    })


def test_validate_all_eight_chart_types_pass():
    """8 种 chart_type 各 1 例都通过（types.ts ChartType 覆盖）。"""
    # heatmap 必带 x/y/value（chart_type 特有校验，见下 test_validate_heatmap_*）。
    cases = {
        "line": {},
        "bar": {},
        "area": {},
        "scatter": {},
        "pareto": {},
        "radar": {},
        "table": {},
        "heatmap": {"x": "col", "y": "row", "value": "v"},
    }
    for ct, extra in cases.items():
        validate_payload({
            "chart_type": ct,
            "data": [{"x": 1, "y": 2}],
            "label": "g1",
            "title": f"t-{ct}",
            **extra,
        })


def test_validate_unknown_chart_type_raises():
    """未知 chart_type → raise（防 client 拼错字 → 落 tape 脏数据）。"""
    with pytest.raises(ValueError, match="未知 chart_type"):
        validate_payload({
            "chart_type": "bubble",  # 不在 8 种
            "data": [],
            "label": "g",
            "title": "t",
        })


def test_validate_data_not_list_raises():
    """data 非 list（dict / str / None）→ raise（防传错 shape）。"""
    for bad in ({"x": 1}, "not a list", None, 42):
        with pytest.raises(ValueError, match="data 必须为 list"):
            validate_payload({
                "chart_type": "line",
                "data": bad,
                "label": "g",
                "title": "t",
            })


def test_validate_empty_label_raises():
    """label 空 → raise（dedup 维度 1 必填）。"""
    for bad in ("", None):
        with pytest.raises(ValueError, match="label 必须非空"):
            validate_payload({
                "chart_type": "line",
                "data": [],
                "label": bad,
                "title": "t",
            })


def test_validate_empty_title_raises():
    """title 空 → raise（dedup 维度 2 必填）。"""
    with pytest.raises(ValueError, match="title 必须非空"):
        validate_payload({
            "chart_type": "line",
            "data": [],
            "label": "g",
            "title": "",
        })


def test_validate_label_not_str_raises():
    """label 非 str（int / list）→ raise（前端 dedup 按字符串 key）。"""
    for bad in (123, ["a", "b"]):
        with pytest.raises(ValueError, match="label 必须非空 str"):
            validate_payload({
                "chart_type": "line",
                "data": [],
                "label": bad,
                "title": "t",
            })


def test_validate_pareto_direction_invalid_raises():
    """pareto_direction 非 max/min → raise（types.ts 契约）。"""
    with pytest.raises(ValueError, match="pareto_direction"):
        validate_payload({
            "chart_type": "pareto",
            "data": [],
            "label": "g",
            "title": "t",
            "pareto_direction": "diagonal",  # 非法
        })


def test_validate_pareto_direction_max_min_passes():
    """pareto_direction = max/min/空 都通过（types.ts 允许值）。"""
    for v in ("max", "min", ""):
        validate_payload({
            "chart_type": "pareto",
            "data": [],
            "label": "g",
            "title": "t",
            "pareto_direction": v,
        })


def test_validate_columns_not_list_of_str_raises():
    """columns 存在但非 list[str] → raise（防前端 table 派生崩溃）。"""
    for bad in ("not list", [1, 2, 3], ["ok", 42]):
        with pytest.raises(ValueError, match="columns 必须为 list\\[str\\]"):
            validate_payload({
                "chart_type": "table",
                "data": [],
                "label": "g",
                "title": "t",
                "columns": bad,
            })


def test_validate_x_y_hue_wrong_type_raises():
    """x/y/hue 存在但非 str → raise（types.ts 契约）。"""
    for field in ("x", "y", "hue"):
        with pytest.raises(ValueError, match=f"{field} 必须为 str"):
            validate_payload({
                "chart_type": "line",
                "data": [],
                "label": "g",
                "title": "t",
                field: 123,
            })


# ── heatmap value / x / y（chart_type 特有校验，fail loud）──


def test_validate_heatmap_without_value_raises():
    """heatmap 缺 value 字段 → raise（cell 着色字段名必填，防 agent 误调）。"""
    with pytest.raises(ValueError, match="heatmap.*value"):
        validate_payload({
            "chart_type": "heatmap",
            "data": [{"r": "a", "b": "w4", "v": 0.9}],
            "label": "g",
            "title": "t",
            "x": "b",
            "y": "r",
            # value 故意缺
        })


def test_validate_heatmap_without_x_raises():
    """heatmap 缺 x → raise（列轴字段名必填，防 pivot 退化 1×1）。"""
    with pytest.raises(ValueError, match="heatmap.*x"):
        validate_payload({
            "chart_type": "heatmap",
            "data": [{"r": "a", "b": "w4", "v": 0.9}],
            "label": "g",
            "title": "t",
            "y": "r",
            "value": "v",
            # x 故意缺
        })


def test_validate_heatmap_without_y_raises():
    """heatmap 缺 y → raise（行轴字段名必填，防 pivot 退化 1×1）。"""
    with pytest.raises(ValueError, match="heatmap.*y"):
        validate_payload({
            "chart_type": "heatmap",
            "data": [{"r": "a", "b": "w4", "v": 0.9}],
            "label": "g",
            "title": "t",
            "x": "b",
            "value": "v",
            # y 故意缺
        })


def test_validate_heatmap_empty_value_raises():
    """heatmap + value='' → raise（显式空串等同未传）。"""
    with pytest.raises(ValueError, match="heatmap.*value"):
        validate_payload({
            "chart_type": "heatmap",
            "data": [],
            "label": "g",
            "title": "t",
            "x": "b",
            "y": "r",
            "value": "",
        })


def test_validate_heatmap_value_non_str_raises():
    """heatmap + value 非法类型（int）→ raise（type 校验）。"""
    with pytest.raises(ValueError, match="value 必须为 str"):
        validate_payload({
            "chart_type": "heatmap",
            "data": [],
            "label": "g",
            "title": "t",
            "x": "b",
            "y": "r",
            "value": 123,
        })


def test_validate_heatmap_with_value_passes():
    """heatmap + value/x/y 非空 str → 通过（happy path）。"""
    validate_payload({
        "chart_type": "heatmap",
        "data": [
            {"recipe": "smooth", "bitwidth": "w4a4", "accuracy": 0.92},
        ],
        "label": "g",
        "title": "t",
        "x": "bitwidth",
        "y": "recipe",
        "value": "accuracy",
    })


def test_validate_value_optional_for_non_heatmap():
    """非 heatmap chart_type + 不传 value → 通过（value 仅 heatmap 必填）。"""
    # line + 完全不传 value（应通过）
    validate_payload({
        "chart_type": "line",
        "data": [{"x": 1, "y": 2}],
        "label": "g",
        "title": "t",
    })
    # scatter + value=''（空串允许，scatter 不消费此字段）
    validate_payload({
        "chart_type": "scatter",
        "data": [{"x": 1, "y": 2}],
        "label": "g",
        "title": "t",
        "value": "",
    })
