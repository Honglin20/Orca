"""_validate.py —— ChartPayload 校验（phase-13 SPEC §7.2，client 端 fail loud）。

校验在 **client lib** 做（写 tape 前）—— 错误信息回到 script / agent，可修；落 tape 后再
发现已是脏数据。ingestor 端只复核大小（防绕过 client lib 直接写 socket）。

校验规则（SPEC §7.2 + types.ts 契约）：
  - ``chart_type`` ∈ 7 种允许值
  - ``data`` 是 list（每行可以是任意 dict，由前端 chart_type 决定字段）
  - ``label`` / ``title`` 非空 str（dedup 维度 1/2）
  - ``pareto_direction`` / ``pareto_x_direction`` / ``pareto_y_direction`` ∈ {"max","min",""}（如
    存在）
  - ``columns``（如存在）是 list[str]

依赖单向：仅依赖 ``_limits``（常量），不依赖 schema/events 等 Orca runtime。
"""

from __future__ import annotations

from typing import Any

from orca.chart._limits import (
    ALLOWED_CHART_TYPES,
    ALLOWED_PARETO_DIRECTIONS,
)


def validate_payload(payload: dict[str, Any]) -> None:
    """校验 ChartPayload（SPEC §7.2）。fail loud：缺字段 / 类型错 / 未知 chart_type → raise ValueError。

    Args:
        payload: 形如 ``{"chart_type": "line", "data": [...], "label": "...", "title": "...",
            "x": "...", "y": "...", "hue": "...", "columns": [...], "pareto_direction": "max"}``。

    Raises:
        ValueError: 任一字段不合规（错误信息回 script / agent，可见可修）。
    """
    # chart_type：7 种之一（types.ts ChartType）
    ct = payload.get("chart_type")
    if ct not in ALLOWED_CHART_TYPES:
        raise ValueError(
            f"未知 chart_type: {ct!r}，允许：{sorted(ALLOWED_CHART_TYPES)}"
        )

    # data：必须是 list（行内字段由前端 chart_type 解释，此处不强约束 dict-shape）
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError(
            f"data 必须为 list，got {type(data).__name__}"
        )

    # label / title：dedup 维度 1/2，必须非空 str
    label = payload.get("label")
    if not isinstance(label, str) or not label:
        raise ValueError(
            f"label 必须非空 str，got {label!r}"
        )
    title = payload.get("title")
    if not isinstance(title, str) or not title:
        raise ValueError(
            f"title 必须非空 str，got {title!r}"
        )

    # pareto_direction 系列（types.ts）：仅 "max" / "min" / 不存在 / 空串
    for key in ("pareto_direction", "pareto_x_direction", "pareto_y_direction"):
        v = payload.get(key)
        if v == "" or v is None:
            continue
        if v not in ALLOWED_PARETO_DIRECTIONS:
            raise ValueError(
                f"{key} 仅允许 'max'/'min'/空，got {v!r}"
            )

    # columns（types.ts，可选）：table 派生列名用，存在时必须是 list[str]
    columns = payload.get("columns")
    if columns is not None:
        if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
            raise ValueError(
                f"columns 必须为 list[str]，got {columns!r}"
            )

    # x / y / hue：可选 str（如存在则必须 str）。types.ts 三者均 ``string | undefined``。
    for key in ("x", "y", "hue"):
        v = payload.get(key)
        if v is not None and not isinstance(v, str):
            raise ValueError(
                f"{key} 必须为 str 或省略，got {type(v).__name__}"
            )
