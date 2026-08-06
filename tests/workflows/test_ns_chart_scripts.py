"""Unit tests for nas-supernet chart script helpers.

Tests the NAS-aware discovery layer (_common.py) and individual chart script helpers
against the REAL NAS data convention:
  - search_config.yaml objs is a list of strings (``["acc", "latency"]``)
  - search_results.jsonl records have nested ``objs`` dict
  - all stored objectives are smaller-is-better (accuracy is negated)

Scripts live in the producing agents' resources (no separate visualize agent):
  - ns_run_search/scripts/: _common.py / pareto.py / search_table.py / latency_dist.py
  - ns_retrain/scripts/: _common.py / metrics_bar.py / compare_table.py
Both ``_common.py`` copies are identical; this test imports from either.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Add both chart-script dirs to path (duplicate _common is identical in either).
_SEARCH_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "workflows" / "agents" / "ns_run_search" / "scripts"
_RETRAIN_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "workflows" / "agents" / "ns_retrain" / "scripts"
sys.path.insert(0, str(_RETRAIN_SCRIPTS_DIR))
sys.path.insert(0, str(_SEARCH_SCRIPTS_DIR))

from _common import (  # noqa: E402
    CHART_MARKER,
    LATENCY_FIELDS,
    PARETO_FIELDS,
    MetricInfo,
    discover_metric_info,
    extract_numeric_values,
    find_field,
    flatten_record,
    parse_loss_log,
    safe_float,
    _metric_objective_name,
)
from compare_table import _parse_si  # noqa: E402
from latency_dist import _histogram  # noqa: E402

# import 完成后立即清理全局态，防污染同 session 的其他测试：
# 1. 移除 sys.path 里插入的脚本目录；
# 2. 弹出 ``_common`` / ``compare_table`` / ``latency_dist`` 模块缓存——这三个是同名
#    跨目录模块（``_quant_scripts/_common`` / ``_struct_scripts/_device`` 等），本文件
#    顶层的 `from _common import ...` 会把 chart 版 `_common` 注册进 sys.modules，
#    不清会截胡后续测试对 `_quant_scripts/_common` 的解析（viz_audit P2-5 全崩）。
#    模块级已绑定的函数引用不受影响（bound names 与 sys.modules 解耦）。
sys.path.remove(str(_SEARCH_SCRIPTS_DIR))
sys.path.remove(str(_RETRAIN_SCRIPTS_DIR))
sys.modules.pop("_common", None)
sys.modules.pop("compare_table", None)
sys.modules.pop("latency_dist", None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def nas_artifacts(tmp_path: Path) -> Path:
    """Create a temp artifacts dir with REAL NAS-format mock data."""
    records = [
        {"generation": 0, "gene": [0, 1], "objs": {"acc": -0.17, "latency": 0.13}, "pareto": True, "arch": {"d": 3}},
        {"generation": 0, "gene": [1, 0], "objs": {"acc": -0.20, "latency": 0.32}, "pareto": True, "arch": {"d": 4}},
        {"generation": 0, "gene": [0, 0], "objs": {"acc": -0.09, "latency": 0.45}, "pareto": False, "arch": {"d": 5}},
    ]
    for r in records:
        (tmp_path / "search_results.jsonl").open("a").write(json.dumps(r) + "\n")

    (tmp_path / "search_config.yaml").write_text(
        'search_space: supernet.SearchSpace\nobjs:\n  - "acc"\n  - "latency"\n'
    )
    return tmp_path


@pytest.fixture
def flat_artifacts(tmp_path: Path) -> Path:
    """Temp artifacts with FLAT (non-NAS) record schema."""
    records = [
        {"depth": 3, "latency_ms": 4.2, "nmse": 0.05, "pareto": True},
        {"depth": 4, "latency_ms": 6.8, "nmse": 0.03, "pareto": True},
    ]
    for r in records:
        (tmp_path / "search_results.jsonl").open("a").write(json.dumps(r) + "\n")

    (tmp_path / "search_config.yaml").write_text(
        "objs:\n  - name: nmse\n    direction: min\n  - name: latency\n    direction: min\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# _metric_objective_name: search_config.yaml objs parsing
# ---------------------------------------------------------------------------


class TestMetricObjectiveName:
    def test_list_of_strings(self, nas_artifacts: Path) -> None:
        """REAL NAS format: objs is list of strings."""
        name = _metric_objective_name(nas_artifacts)
        assert name == "acc"

    def test_list_of_dicts(self, flat_artifacts: Path) -> None:
        """Defensive format: objs is list of dicts."""
        name = _metric_objective_name(flat_artifacts)
        assert name == "nmse"

    def test_missing_config(self, tmp_path: Path) -> None:
        assert _metric_objective_name(tmp_path) == ""

    def test_empty_objs(self, tmp_path: Path) -> None:
        (tmp_path / "search_config.yaml").write_text("objs: []\n")
        assert _metric_objective_name(tmp_path) == ""


# ---------------------------------------------------------------------------
# discover_metric_info: end-to-end discovery
# ---------------------------------------------------------------------------


class TestDiscoverMetricInfo:
    def test_nas_nested_negated(self, nas_artifacts: Path) -> None:
        """REAL NAS: nested objs.acc, negated accuracy."""
        info = discover_metric_info(nas_artifacts)
        assert info is not None
        assert info.name == "acc"
        assert info.field_path == "objs.acc"
        assert info.latency_path == "objs.latency"
        assert info.pareto_y_direction == "min"
        assert info.display_direction == "higher"
        assert info.negate_for_display is True
        assert info.for_display(-0.17) == pytest.approx(0.17)

    def test_flat_positive(self, flat_artifacts: Path) -> None:
        """FLAT schema with positive values: lower-better (NMSE)."""
        info = discover_metric_info(flat_artifacts)
        assert info is not None
        assert info.name == "nmse"
        assert info.field_path == "nmse"
        assert info.latency_path == "latency_ms"
        assert info.display_direction == "lower"
        assert info.negate_for_display is False
        assert info.for_display(0.05) == pytest.approx(0.05)

    def test_no_records(self, tmp_path: Path) -> None:
        """Missing search_results.jsonl → returns None or minimal info."""
        (tmp_path / "search_config.yaml").write_text('objs:\n  - "acc"\n  - "latency"\n')
        info = discover_metric_info(tmp_path)
        # No records → field paths empty but name discovered from config.
        assert info is not None
        assert info.name == "acc"
        assert info.field_path == ""


# ---------------------------------------------------------------------------
# flatten_record + find_field
# ---------------------------------------------------------------------------


class TestRecordAccess:
    def test_flatten_nested(self) -> None:
        rec = {"objs": {"acc": -0.17, "latency": 0.13}, "pareto": True}
        flat = flatten_record(rec)
        assert flat["objs.acc"] == -0.17
        assert flat["objs.latency"] == 0.13
        assert flat["pareto"] is True

    def test_find_field_nested(self) -> None:
        records = [{"objs": {"latency": 0.1}}]
        assert find_field(records, LATENCY_FIELDS) == "objs.latency"

    def test_find_field_flat(self) -> None:
        records = [{"latency_ms": 4.2}]
        assert find_field(records, LATENCY_FIELDS) == "latency_ms"

    def test_find_field_missing(self) -> None:
        records = [{"depth": 3}]
        assert find_field(records, LATENCY_FIELDS) == ""

    def test_extract_numeric_values(self) -> None:
        records = [
            {"objs": {"acc": -0.17}},
            {"objs": {"acc": -0.20}},
            {"objs": {"acc": "invalid"}},
            {"objs": {}},
        ]
        vals = extract_numeric_values(records, "objs.acc")
        assert vals == [-0.17, -0.20]


# ---------------------------------------------------------------------------
# safe_float
# ---------------------------------------------------------------------------


class TestSafeFloat:
    def test_plain_number(self) -> None:
        assert safe_float("0.92") == pytest.approx(0.92)

    def test_quoted(self) -> None:
        assert safe_float('"0.92"') == pytest.approx(0.92)
        assert safe_float("'0.92'") == pytest.approx(0.92)

    def test_negative(self) -> None:
        assert safe_float("-0.17") == pytest.approx(-0.17)

    def test_empty(self) -> None:
        assert safe_float("") is None
        assert safe_float('""') is None

    def test_invalid(self) -> None:
        assert safe_float("abc") is None


# ---------------------------------------------------------------------------
# parse_loss_log
# ---------------------------------------------------------------------------


class TestParseLossLog:
    def test_text_format(self, tmp_path: Path) -> None:
        log = tmp_path / "train.log"
        log.write_text("step 10: loss=0.9\nstep 20: loss=0.7\nstep 30: loss=0.5\n")
        points = parse_loss_log(log)
        assert len(points) == 3
        assert points[0] == {"step": 10.0, "loss": 0.9}
        assert points[2] == {"step": 30.0, "loss": 0.5}

    def test_json_format(self, tmp_path: Path) -> None:
        log = tmp_path / "train.log"
        log.write_text(
            json.dumps({"step": 100, "loss": 0.5}) + "\n"
            + json.dumps({"step": 200, "loss": 0.3}) + "\n"
        )
        points = parse_loss_log(log)
        assert len(points) == 2
        assert points[0] == {"step": 100.0, "loss": 0.5}

    def test_missing_file(self, tmp_path: Path) -> None:
        assert parse_loss_log(tmp_path / "nonexistent.log") == []

    def test_no_loss_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "train.log"
        log.write_text("starting training...\nepoch 1 done\n")
        assert parse_loss_log(log) == []


# ---------------------------------------------------------------------------
# _parse_si (SI suffix parsing)
# ---------------------------------------------------------------------------


class TestParseSi:
    def test_plain(self) -> None:
        assert _parse_si("1250000") == pytest.approx(1250000)

    def test_k_suffix(self) -> None:
        assert _parse_si("1.5K") == pytest.approx(1500)

    def test_m_suffix(self) -> None:
        assert _parse_si("1.25M") == pytest.approx(1.25e6)

    def test_g_suffix(self) -> None:
        assert _parse_si("2.3G") == pytest.approx(2.3e9)

    def test_empty(self) -> None:
        assert _parse_si("") is None
        assert _parse_si("abc") is None


# ---------------------------------------------------------------------------
# _histogram (latency distribution)
# ---------------------------------------------------------------------------


class TestHistogram:
    def test_basic_bins(self) -> None:
        vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        hist = _histogram(vals)
        assert len(hist) == 10  # default _NUM_BINS
        total = sum(row["count"] for row in hist)
        assert total == 5

    def test_uniform_value(self) -> None:
        vals = [3.0, 3.0, 3.0]
        hist = _histogram(vals)
        assert len(hist) == 1
        assert hist[0]["count"] == 3

    def test_bin_labels(self) -> None:
        vals = [0.0, 10.0]
        hist = _histogram(vals)
        assert "bin" in hist[0]
        assert "count" in hist[0]


# ---------------------------------------------------------------------------
# MetricInfo for_display polarity
# ---------------------------------------------------------------------------


class TestMetricInfoForDisplay:
    def test_negated(self) -> None:
        info = MetricInfo(
            name="acc", field_path="objs.acc", latency_path="objs.latency",
            pareto_y_direction="min", display_direction="higher", negate_for_display=True,
        )
        assert info.for_display(-0.17) == pytest.approx(0.17)
        assert info.for_display(-0.95) == pytest.approx(0.95)

    def test_not_negated(self) -> None:
        info = MetricInfo(
            name="nmse", field_path="nmse", latency_path="latency_ms",
            pareto_y_direction="min", display_direction="lower", negate_for_display=False,
        )
        assert info.for_display(0.05) == pytest.approx(0.05)
