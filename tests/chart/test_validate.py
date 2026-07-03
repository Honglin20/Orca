"""tests/chart/test_validate.py —— ChartPayload 校验（phase-13 SPEC §7.2）。

覆盖意图（非仅行为）：
  - 合法 payload 通过（line/bar/area/scatter/pareto/radar/table 7 种各 1 例）
  - chart_type 未知 → raise（防 client 拼错字 → 落 tape 脏数据）
  - data 非 list → raise（防传 dict / 单行）
  - label/title 空或非 str → raise（dedup 维度）
  - pareto_direction 非 max/min → raise（types.ts 契约）
  - columns 非 list[str] → raise
  - x/y/hue 非法类型 → raise
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


def test_validate_all_seven_chart_types_pass():
    """7 种 chart_type 各 1 例都通过（types.ts ChartType 覆盖）。"""
    for ct in ("line", "bar", "area", "scatter", "pareto", "radar", "table"):
        validate_payload({
            "chart_type": ct,
            "data": [{"x": 1, "y": 2}],
            "label": "g1",
            "title": f"t-{ct}",
        })


def test_validate_unknown_chart_type_raises():
    """未知 chart_type → raise（防 client 拼错字 → 落 tape 脏数据）。"""
    with pytest.raises(ValueError, match="未知 chart_type"):
        validate_payload({
            "chart_type": "heatmap",  # 不在 7 种
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
