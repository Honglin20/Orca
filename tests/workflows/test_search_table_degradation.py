"""Tests for search_table.py A1 degradation behavior + A4 best_val sentinel filter.

A1 scenarios:
  (a) pareto field + some pareto=yes → only front, title "Pareto Front"
  (b) no pareto field → all deduped, title "All Architectures"
  (c) 0 rows jsonl → skip_reason "missing or empty"
  (d) pareto field but all pareto=no → all deduped, title "All Architectures"

A4: best_val_metric_from_log filters NaN/inf/3.4e38 sentinels via math.isfinite.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

_SEARCH_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "workflows" / "agents" / "ns_run_search" / "scripts"
_RETRAIN_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "workflows" / "agents" / "ns_retrain" / "scripts"

sys.path.insert(0, str(_SEARCH_SCRIPTS_DIR))
sys.path.insert(0, str(_RETRAIN_SCRIPTS_DIR))

from _common import best_val_metric_from_log, CHART_MARKER  # noqa: E402

# Load search_table.py via exec (avoids sys.modules pollution + matches
# the pattern used by test_progress_watcher).
_SEARCH_TABLE_PATH = _SEARCH_SCRIPTS_DIR / "search_table.py"
_ST_NS: dict = {"__file__": str(_SEARCH_TABLE_PATH)}
exec(compile(_SEARCH_TABLE_PATH.read_text(encoding="utf-8"), str(_SEARCH_TABLE_PATH), "exec"), _ST_NS)

# Cleanup: remove sys.path entries + pop _common to avoid polluting other tests.
sys.path.remove(str(_SEARCH_SCRIPTS_DIR))
sys.path.remove(str(_RETRAIN_SCRIPTS_DIR))
sys.modules.pop("_common", None)


def _write_search_results(tmp_path: Path, records: list[dict]) -> Path:
    """Write search_results.jsonl + search_config.yaml to tmp_path."""
    p = tmp_path / "search_results.jsonl"
    with p.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    (tmp_path / "search_config.yaml").write_text(
        'search_space: supernet.SearchSpace\nobjs:\n  - "acc"\n  - "latency"\n'
    )
    return tmp_path


def _run_search_table(artifacts_dir: Path) -> list[dict]:
    """Run search_table main() and return push_chart call kwargs (captures data/title/caption)."""
    calls: list[dict] = []
    _ST_NS["push_chart"] = lambda **kw: calls.append(kw)
    old_argv = sys.argv
    sys.argv = ["search_table", "--artifacts-dir", str(artifacts_dir)]
    try:
        _ST_NS["main"]()
    finally:
        sys.argv = old_argv
    return calls


# ---------------------------------------------------------------------------
# A1: search_table degradation
# ---------------------------------------------------------------------------


class TestSearchTableParetoFront:
    """Scenario (a): has pareto field + some pareto=yes → only front."""

    def test_only_pareto_rows_shown(self, tmp_path: Path) -> None:
        records = [
            {"gene": [0], "objs": {"acc": -0.17, "latency": 0.13}, "pareto": True, "arch": {"d": 3}},
            {"gene": [1], "objs": {"acc": -0.20, "latency": 0.32}, "pareto": True, "arch": {"d": 4}},
            {"gene": [0], "objs": {"acc": -0.09, "latency": 0.45}, "pareto": False, "arch": {"d": 5}},
        ]
        ad = _write_search_results(tmp_path, records)
        results = _run_search_table(ad)
        assert len(results) == 1
        assert "Pareto Front" in results[0]["title"]
        # Only pareto=yes rows shown (2 out of 3 records).
        assert len(results[0].get("data", [])) == 2, \
            "should show only 2 pareto=yes rows, not all 3"


class TestSearchTableNoParetoField:
    """Scenario (b): no pareto field → all deduped, title 'All Architectures'."""

    def test_all_shown_no_pareto(self, tmp_path: Path) -> None:
        records = [
            {"gene": [0], "objs": {"acc": -0.17, "latency": 0.13}, "arch": {"d": 3}},
            {"gene": [1], "objs": {"acc": -0.20, "latency": 0.32}, "arch": {"d": 4}},
        ]
        ad = _write_search_results(tmp_path, records)
        results = _run_search_table(ad)
        assert len(results) == 1
        assert "All Architectures" in results[0]["title"]
        # Intent: degradation shows ALL deduped rows, not empty/partial.
        assert len(results[0].get("data", [])) == 2, \
            "degradation should show all 2 deduped architectures"
        # Caption should mention "未识别" per SPEC.
        assert "未識別" in results[0].get("caption", "") or "未识别" in results[0].get("caption", "")


class TestSearchTableAllParetoNo:
    """Scenario (d): has pareto field but all pareto=no → degrade to all."""

    def test_degrade_when_all_no(self, tmp_path: Path) -> None:
        records = [
            {"gene": [0], "objs": {"acc": -0.17, "latency": 0.13}, "pareto": False, "arch": {"d": 3}},
            {"gene": [1], "objs": {"acc": -0.20, "latency": 0.32}, "pareto": False, "arch": {"d": 4}},
        ]
        ad = _write_search_results(tmp_path, records)
        results = _run_search_table(ad)
        assert len(results) == 1
        assert "All Architectures" in results[0]["title"]
        # Intent: all deduped rows shown despite all pareto=no.
        assert len(results[0].get("data", [])) == 2


class TestSearchTableEmptyJsonl:
    """Scenario (c): 0 rows → skip_reason 'missing or empty'."""

    def test_empty_records_skip(self, tmp_path: Path) -> None:
        ad = _write_search_results(tmp_path, [])
        results = _run_search_table(ad)
        assert len(results) == 1
        # push_chart is called with skip_reason for empty data.
        assert "missing or empty" in results[0].get("skip_reason", "")


# ---------------------------------------------------------------------------
# A4: best_val_metric_from_log NaN/inf/sentinel filter
# ---------------------------------------------------------------------------


class TestBestValSentinelFilter:
    """math.isfinite catches NaN/inf; abs>=1e6 catches float32-max sentinels."""

    def test_filters_sentinels_returns_normal(self, tmp_path: Path) -> None:
        """Log with 3.4e38 sentinel + NaN + inf + normal value → returns normal."""
        log_dir = tmp_path / "runs" / "train"
        log_dir.mkdir(parents=True)
        log = log_dir / "train.attempt1.log"
        lines = [
            "epoch 1/10 val_acc=0.85",
            "epoch 2/10 val_acc=nan",             # NaN → filtered by isfinite
            "epoch 3/10 val_acc=inf",             # inf → filtered by isfinite
            "epoch 4/10 val_acc=3.4e38",          # float32-max sentinel → filtered by abs>=1e6
            "epoch 5/10 val_acc=0.92",            # normal → best (higher direction)
        ]
        log.write_text("\n".join(lines) + "\n")
        result = best_val_metric_from_log(tmp_path, "acc", "higher")
        assert result is not None
        assert math.isfinite(result)
        assert abs(result) < 1e6
        assert result == pytest.approx(0.92)

    def test_all_sentinels_returns_none(self, tmp_path: Path) -> None:
        """All values are NaN/inf/sentinel → returns None."""
        log_dir = tmp_path / "runs" / "train"
        log_dir.mkdir(parents=True)
        log = log_dir / "train.attempt1.log"
        log.write_text("epoch 1/10 val_acc=nan\nepoch 2/10 val_acc=3.4e38\n")
        result = best_val_metric_from_log(tmp_path, "acc", "higher")
        assert result is None
