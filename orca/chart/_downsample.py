"""_downsample.py —— 8 种 chart_type 的降采样策略（phase-13 SPEC §5.1）。

回答「data 行数 > max_points 时怎么降采样才不破坏语义？」：每种 chart_type 各自的策略，
line/area/scatter 按 hue 分组、bar/pareto 按 x 聚合、table/heatmap 取前 N、radar 不降采样。

策略表（SPEC §5.1）：
  - **line / area**：按 hue 分组各自降采样 → 每组 ≤ ``max_points // hue_cardinality``；
    每组按长度分桶取 (x_mean, y_mean)（保持趋势 + 抹平噪声）。
  - **scatter**：按 hue 分组各自均匀随机抽样（保分布）。
  - **bar / pareto**：按 x 分组聚合（sum），取 top ``max_points`` 个 x。hue 存在时按 (x, hue)
    聚合。
  - **table / heatmap**：取前 ``max_points`` 行（top-N 语义，不取 head+tail 以免破坏用户
    排序；heatmap 长格式 record 通常远小于 max_points，cap 仅防极端）。
  - **radar**：不降采样（数据点本质少）。

**透明降采样**：不 raise，仅由 ``_render.render_chart`` 写 stderr warning（原数据保留在 script）。

依赖单向：仅依赖 stdlib + ``_limits`` 常量。无 Orca runtime 依赖。
"""

from __future__ import annotations

import math
from typing import Any

# 注：不 import ``ALLOWED_CHART_TYPES`` —— 分派按 chart_type 字面量（chart_type 集合稳定，
# 8 种；如未来新增需扩展策略表，单点改本文件）。常量真相源在 ``_limits``，与 downsample
# 分派解耦（避免「import 了但不用」误导，下次加 chart_type 时给人「已接好」的错觉）。


def downsample(
    chart_type: str,
    data: list[dict[str, Any]],
    max_points: int,
    hue: str = "",
) -> list[dict[str, Any]]:
    """按 SPEC §5.1 策略表降采样。

    Args:
        chart_type: ``line`` / ``bar`` / ``area`` / ``scatter`` / ``pareto`` / ``radar`` / ``table``
            / ``heatmap``。未知 chart_type 不降采样（``validate_payload`` 已先行 raise，此处兜底
            防误调）。
        data: 原始行列表。
        max_points: 目标上限。若 ``len(data) <= max_points`` 直接返回原 data。
        hue: hue 字段名（line/area/scatter 多系列着色用）。空串视为单系列。

    Returns:
        降采样后的 list。永不 raise（透明降采样）。原 data 不变（返回新 list）。
    """
    if len(data) <= max_points:
        return data
    if chart_type in ("line", "area"):
        return _by_hue_groups(chart_type, data, max_points, hue, _bucket_average)
    if chart_type == "scatter":
        return _by_hue_groups(chart_type, data, max_points, hue, _uniform_sample)
    if chart_type in ("bar", "pareto"):
        return _aggregate_by_x(data, max_points, hue)
    if chart_type in ("table", "heatmap"):
        # table 与 heatmap 都是扁平 record array，top-N 截断保用户排序 / 矩阵完整性。
        # heatmap 长格式通常远 < max_points（行×列单元格数），cap 仅防极端输入。
        return data[:max_points]
    if chart_type == "radar":
        return data
    # 兜底：未知 chart_type（validate_payload 已 raise，此处仅用于防御性）
    return data


# ── 内部策略 ─────────────────────────────────────────────────────────────────


def _by_hue_groups(
    chart_type: str,
    data: list[dict[str, Any]],
    max_points: int,
    hue: str,
    resample_fn,
) -> list[dict[str, Any]]:
    """按 hue 分组各自降采样（line/area/scatter 用，SPEC §5.1）。

    无 hue → 单组，全部走 ``resample_fn``。
    有 hue → 按 hue 值分组，每组 ``max(1, max_points // 组数)`` 行。
    """
    if not hue:
        return resample_fn(data, max_points)
    groups = _group_by(data, hue)
    n_groups = len(groups)
    if n_groups == 0:
        return resample_fn(data, max_points)
    per_group = max(1, max_points // n_groups)
    out: list[dict[str, Any]] = []
    for _, rows in groups.items():
        out.extend(resample_fn(rows, per_group))
    return out


def _group_by(data: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    """按 ``data[i][key]`` 分组（缺失值用 None 桶）。保插入顺序（Python 3.7+ dict）。"""
    groups: dict[Any, list[dict[str, Any]]] = {}
    for row in data:
        k = row.get(key)
        groups.setdefault(k, []).append(row)
    return groups


def _bucket_average(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """line/area 按长度分桶取 (x_mean, y_mean)（SPEC §5.1）。

    桶大小 = ``ceil(len(rows) / n)``；每桶内对所有数值字段求均值。非数值字段取桶内首条
    （保留 x 标签等元信息）。空 rows → 返回 []。
    """
    if not rows:
        return []
    if len(rows) <= n:
        return list(rows)
    bucket_size = max(1, math.ceil(len(rows) / n))
    out: list[dict[str, Any]] = []
    for i in range(0, len(rows), bucket_size):
        bucket = rows[i : i + bucket_size]
        if not bucket:
            break
        out.append(_avg_row(bucket))
    return out


def _avg_row(bucket: list[dict[str, Any]]) -> dict[str, Any]:
    """对桶内 rows 求平均：数值字段求 mean，非数值字段取首条。

    保留所有键（line/area 通常用 x_mean 作为 x 标签）。空 bucket → {}。
    """
    if not bucket:
        return {}
    merged: dict[str, Any] = dict(bucket[0])  # 非数值字段首条占位
    # 数值字段覆盖为均值
    numeric_keys = {
        k for k in merged
        if isinstance(merged[k], (int, float)) and not isinstance(merged[k], bool)
    }
    for k in numeric_keys:
        vals = [r[k] for r in bucket if k in r and isinstance(r[k], (int, float)) and not isinstance(r[k], bool)]
        if vals:
            merged[k] = sum(vals) / len(vals)
    return merged


def _uniform_sample(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """scatter 均匀随机抽样（SPEC §5.1）：等间距取 n 行（保分布）。

    用等间距而非 ``random.sample``：确定性（同输入同输出，便于测试断言 + replay 一致）+
    保分布（首尾都被采到，比 random.sample 更稳定）。
    """
    if len(rows) <= n:
        return list(rows)
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


def _aggregate_by_x(
    data: list[dict[str, Any]],
    max_points: int,
    hue: str,
) -> list[dict[str, Any]]:
    """bar/pareto 按 x 分组聚合（SPEC §5.1）。

    无 hue：按 x 值聚合，y（首个数值字段）sum；取 top ``max_points`` 个 x。
    有 hue：按 (x, hue) 聚合 y sum；取 top ``max_points`` 个 (x, hue) 组合。

    聚合字段推断：取每行**第一个数值字段**作为 y（bar/pareto 一般 y 是数值，x 是分类）。
    若无数值字段，退化为计数（每 x 计数 = 行数）。
    """
    if not data:
        return []
    y_field = _infer_y_field(data)
    if hue:
        return _aggregate_by_xhue(data, max_points, hue, y_field)
    return _aggregate_by_xonly(data, max_points, y_field)


def _infer_y_field(data: list[dict[str, Any]]) -> str | None:
    """推断 y 字段：取首个含数值字段的行的第一个数值键。无 → None（计数模式）。"""
    for row in data:
        for k, v in row.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return k
    return None


def _aggregate_by_xonly(
    data: list[dict[str, Any]],
    max_points: int,
    y_field: str | None,
) -> list[dict[str, Any]]:
    """无 hue：按 x 聚合 y sum（无 y_field 时退化为计数）。取 top max_points 个 x。"""
    sums: dict[Any, float] = {}
    first_rows: dict[Any, dict[str, Any]] = {}
    for row in data:
        x = row.get("x")
        if x not in sums:
            sums[x] = 0.0
            first_rows[x] = dict(row)
        if y_field is not None and isinstance(row.get(y_field), (int, float)):
            sums[x] += float(row[y_field])
        else:
            sums[x] += 1.0
    # 取 top max_points（按 sum 降序保 top）
    top = sorted(sums.items(), key=lambda kv: kv[1], reverse=True)[:max_points]
    out: list[dict[str, Any]] = []
    for x, s in top:
        merged = dict(first_rows[x])
        merged["x"] = x
        if y_field is not None:
            merged[y_field] = s
        out.append(merged)
    return out


def _aggregate_by_xhue(
    data: list[dict[str, Any]],
    max_points: int,
    hue: str,
    y_field: str | None,
) -> list[dict[str, Any]]:
    """有 hue：按 (x, hue) 聚合 y sum。取 top max_points 个组合，保 x + hue + y_field。"""
    sums: dict[tuple[Any, Any], float] = {}
    first_rows: dict[tuple[Any, Any], dict[str, Any]] = {}
    for row in data:
        x = row.get("x")
        h = row.get(hue)
        key = (x, h)
        if key not in sums:
            sums[key] = 0.0
            first_rows[key] = dict(row)
        if y_field is not None and isinstance(row.get(y_field), (int, float)):
            sums[key] += float(row[y_field])
        else:
            sums[key] += 1.0
    top = sorted(sums.items(), key=lambda kv: kv[1], reverse=True)[:max_points]
    out: list[dict[str, Any]] = []
    for (x, h), s in top:
        merged = dict(first_rows[(x, h)])
        merged["x"] = x
        merged[hue] = h
        if y_field is not None:
            merged[y_field] = s
        out.append(merged)
    return out
