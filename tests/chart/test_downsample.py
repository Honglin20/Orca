"""tests/chart/test_downsample.py —— 6 种 chart_type 降采样策略（phase-13 SPEC §5.1）。

覆盖意图（非仅行为）：
  - line 按 hue 分组降采样：3 系列 × N 行 → 各组 ≤ ceil(max/n_groups)
  - line 无 hue 单系列：分桶取均值（趋势保留）
  - scatter 均匀抽样：保分布、确定性（同输入同输出）
  - table 取前 N：top-N 语义，不破坏用户排序
  - radar 不降采样：原数据返回
  - bar/pareto 按 x 聚合：sum 后取 top max_points
  - data 行数 ≤ max_points → 直接返回原 data（透明）
  - hue 缺失时单组降采样
"""

from __future__ import annotations

import pytest

from orca.chart._downsample import downsample


# ── line / area：按 hue 分组、按长度分桶取均值 ─────────────────────────────


def test_downsample_line_no_hue_buckets_to_max_points():
    """line 无 hue 单系列：分桶取均值，输出 ≤ max_points 行。

    意图：3-point 数据 max_points=2 → 输出 ≤ 2 行，y 字段是均值（保留趋势）。
    """
    data = [
        {"x": 0, "y": 0.0},
        {"x": 1, "y": 2.0},
        {"x": 2, "y": 4.0},
    ]
    out = downsample("line", data, max_points=2)
    assert len(out) <= 2
    # 验证均值：3 行分 2 桶（bucket_size=ceil(3/2)=2），第 1 桶 (0+2)/2=1.0
    assert out[0]["y"] == pytest.approx(1.0)


def test_downsample_line_with_hue_groups():
    """line 有 hue 3 系列 × 6000 行 → 各组 ≤ 666 行（max_points // n_groups）。

    意图：每组独立分桶，hue 字段保留，组内行数受 max_points//n_groups 限制。
    """
    series = ["a", "b", "c"]
    data = [
        {"x": i, "y": float(i), "hue": s}
        for s in series
        for i in range(2000)
    ]
    out = downsample("line", data, max_points=1998, hue="hue")  # 故意 < 总行数
    # 每组 ≤ ceil(1998 / 3) = 666 行
    from collections import Counter
    counts = Counter(r["hue"] for r in out)
    for s in series:
        assert counts[s] <= 666, f"series {s} 超 max_points//n_groups: {counts[s]}"


def test_downsample_area_same_as_line_strategy():
    """area 与 line 同降采样策略（按 hue 分组、分桶取均值）。"""
    data = [{"x": i, "y": float(i)} for i in range(100)]
    out_line = downsample("line", data, max_points=10)
    out_area = downsample("area", data, max_points=10)
    # 同策略 → 同长度（不比对内容因 random-free 但分桶一致）
    assert len(out_line) == len(out_area) <= 10


# ── scatter：均匀抽样 ────────────────────────────────────────────────────────


def test_downsample_scatter_uniform_sample_deterministic():
    """scatter 等间距抽样：确定性（同输入同输出）+ 保分布（首点必含）。

    意图：抽样算法稳定（replay 一致 + 测试可断言），且均匀（防聚集）。等间距采样从 i=0
    开始按 step=len/n 抽，第一个必是 data[0]；最后一个 index 是 int((n-1)*step)，不一定
    是 data[-1]（这是等间距采样的标准行为）。
    """
    data = [{"x": i, "y": float(i)} for i in range(10000)]
    out1 = downsample("scatter", data, max_points=2000)
    out2 = downsample("scatter", data, max_points=2000)
    assert out1 == out2  # 确定性
    assert len(out1) == 2000
    # 首点必采到（保分布起点）
    assert out1[0]["x"] == 0
    # 等间距：相邻采样点的 index 差恒定（step = 10000/2000 = 5）
    indices = [r["x"] for r in out1]
    diffs = {indices[i + 1] - indices[i] for i in range(len(indices) - 1)}
    assert diffs == {5}, f"采样不等间距: diffs={diffs}"


def test_downsample_scatter_with_hue_groups():
    """scatter 有 hue → 按 hue 分组各自均匀抽样。"""
    data = [
        {"x": i, "y": float(i), "hue": s}
        for s in ("a", "b")
        for i in range(4000)
    ]
    out = downsample("scatter", data, max_points=2000, hue="hue")
    from collections import Counter
    counts = Counter(r["hue"] for r in out)
    # 每组 ≤ 2000//2 = 1000
    for s in ("a", "b"):
        assert counts[s] <= 1000


# ── table：取前 N（top-N 语义）───────────────────────────────────────────────


def test_downsample_table_takes_first_n_preserve_user_order():
    """table 取前 max_points 行：top-N 语义，**不破坏用户排序**。

    意图：若用户按降序排，head+tail 会乱序；取前 N 保用户排序意图。
    """
    data = [{"i": i, "label": f"row-{i}"} for i in range(5000)]
    out = downsample("table", data, max_points=2000)
    assert len(out) == 2000
    assert out[0]["i"] == 0
    assert out[-1]["i"] == 1999  # 前 2000 行，不是 head+tail


# ── radar：不降采样 ──────────────────────────────────────────────────────────


def test_downsample_radar_returns_original():
    """radar 数据点本质少，不降采样（原数据返回）。"""
    data = [{"axis": f"a{i}", "value": float(i)} for i in range(100)]
    out = downsample("radar", data, max_points=10)
    assert out == data


# ── bar / pareto：按 x 聚合 ─────────────────────────────────────────────────


def test_downsample_bar_aggregate_by_x_top_max_points():
    """bar 按 x 聚合 y sum，取 top max_points 个 x（按 sum 降序）。

    意图：bar 的 x 是分类，多行同 x 需 sum；行数过多时取 top N 分类。
    """
    data = [
        {"x": "a", "y": 10.0},
        {"x": "a", "y": 20.0},  # a sum=30
        {"x": "b", "y": 5.0},   # b sum=5
        {"x": "c", "y": 100.0}, # c sum=100（top 1）
    ]
    out = downsample("bar", data, max_points=2)
    assert len(out) == 2
    # top 2 by sum：c (100) > a (30)
    x_values = {r["x"] for r in out}
    assert x_values == {"c", "a"}
    # y 字段为聚合后的 sum
    a_row = next(r for r in out if r["x"] == "a")
    assert a_row["y"] == 30.0


def test_downsample_pareto_with_hue_aggregate_by_xhue():
    """pareto 有 hue → 按 (x, hue) 聚合，取 top max_points 个组合。"""
    data = [
        {"x": "a", "y": 10.0, "hue": "red"},
        {"x": "a", "y": 5.0, "hue": "blue"},
        {"x": "a", "y": 20.0, "hue": "red"},  # (a, red) sum=30
    ]
    out = downsample("pareto", data, max_points=2, hue="hue")
    assert len(out) <= 2
    # top 1：(a, red) sum=30
    assert any(r["x"] == "a" and r["hue"] == "red" and r["y"] == 30.0 for r in out)


# ── 边界 ────────────────────────────────────────────────────────────────────


def test_downsample_data_smaller_than_max_returns_original():
    """data 行数 ≤ max_points → 直接返回原 data（透明降采样，无副作用）。"""
    data = [{"x": i, "y": float(i)} for i in range(10)]
    out = downsample("line", data, max_points=100)
    assert out == data


def test_downsample_empty_data():
    """空 data → 空 list（不抛、不分桶）。"""
    assert downsample("line", [], max_points=100) == []
    assert downsample("table", [], max_points=100) == []
