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
# ns_retrain first: it also carries the shared metric-harvester functions the
# test below imports; both copies share the same discover_metric_info.
_SEARCH_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "workflows" / "agents" / "ns_run_search" / "scripts"
_RETRAIN_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "workflows" / "agents" / "ns_retrain" / "scripts"
sys.path.insert(0, str(_SEARCH_SCRIPTS_DIR))
sys.path.insert(0, str(_RETRAIN_SCRIPTS_DIR))

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

    def test_nan_sentinels_do_not_flip_polarity(self) -> None:
        """NaN acc encoded as float32 max (3.4e38) must not break sign heuristic.

        Regression: 10 NaN rows mixed with valid negated accuracy used to make
        ``all(v <= 0)`` False → negate_for_display wrongly False → chart showed
        negative accuracy. Invalid overflow sentinels are filtered before polarity
        detection.
        """
        vals = [-0.9884, -0.9876, -0.9891, 3.402823e38, 3.402823e38]
        records = [{"objs": {"acc": v, "latency": 0.2}} for v in vals]
        info = discover_metric_info(Path("/nonexistent"), records)
        assert info is not None
        assert info.name == "acc"
        assert info.display_direction == "higher"
        assert info.negate_for_display is True
        # min over stored (smaller-is-better) = the most negative valid acc.
        assert info.for_display(-0.9891) == pytest.approx(0.9891)

    def test_large_magnitude_negated_metric_not_clipped(self) -> None:
        """Legitimate metrics with magnitude > 1 (reward/BLEU) still negate.

        The garbage filter must target overflow sentinels only, never real
        higher-better metrics stored negated beyond +/-1.
        """
        vals = [-100.0, -95.0, -97.5]
        records = [{"objs": {"acc": v, "latency": 0.2}} for v in vals]
        info = discover_metric_info(Path("/nonexistent"), records)
        assert info is not None
        assert info.negate_for_display is True
        assert info.for_display(-95.0) == pytest.approx(95.0)

    def test_all_nan_falls_back_without_crash(self) -> None:
        """All-garbage metric values: no crash, no false negation."""
        records = [{"objs": {"acc": 3.402823e38, "latency": 0.2}} for _ in range(3)]
        info = discover_metric_info(Path("/nonexistent"), records)
        assert info is not None
        assert info.negate_for_display is False


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


# ---------------------------------------------------------------------------
# Shared metric harvesters (best_val_metric_from_log / final_metric_from_json)
# ---------------------------------------------------------------------------


class TestSharedMetricHarvesters:
    def _load_common(self):
        """Load the ns_retrain _common module fresh (module cache was cleaned at import)."""
        sys.path.insert(0, str(_RETRAIN_SCRIPTS_DIR))
        try:
            import _common
            return _common
        finally:
            sys.path.remove(str(_RETRAIN_SCRIPTS_DIR))
            # Pop the module cache so later tests resolving _quant_scripts/_common
            # are not shadowed by the chart _common (same cross-dir hazard as
            # module import at the top of this file).
            sys.modules.pop("_common", None)

    def test_best_val_metric_from_log_json_and_regex(self, tmp_path: Path) -> None:
        common = self._load_common()
        runs_train = tmp_path / "runs" / "train"
        runs_train.mkdir(parents=True)
        log = runs_train / "train.attempt1.log"
        log.write_text(
            '{"epoch": 3, "loss": 0.2, "test_acc": 0.93}\n'
            '{"epoch": 6, "loss": 0.1, "test_acc": 0.95}\n'
            "epoch 9/10 loss 0.05\n"
            "eval test_acc=0.97 best=0.97\n",
            encoding="utf-8",
        )
        # JSON key match picks 0.95 (max over test_acc JSON rows), regex fallback
        # picks 0.97; higher direction → overall best = 0.97.
        assert common.best_val_metric_from_log(tmp_path, "acc", "higher") == pytest.approx(0.97)

    def test_best_val_metric_lower_direction(self, tmp_path: Path) -> None:
        common = self._load_common()
        runs_train = tmp_path / "runs" / "train"
        runs_train.mkdir(parents=True)
        (runs_train / "train.attempt1.log").write_text(
            '{"epoch": 1, "val_loss": 0.9}\n{"epoch": 2, "val_loss": 0.4}\n',
            encoding="utf-8",
        )
        assert common.best_val_metric_from_log(tmp_path, "loss", "lower") == pytest.approx(0.4)

    def test_best_val_metric_missing_log(self, tmp_path: Path) -> None:
        common = self._load_common()
        assert common.best_val_metric_from_log(tmp_path, "acc", "higher") is None

    def test_final_metric_from_json(self, tmp_path: Path) -> None:
        common = self._load_common()
        retrain = tmp_path / "runs" / "retrain"
        retrain.mkdir(parents=True)
        (retrain / "test_metrics.json").write_text(
            json.dumps({"test_acc": 0.99, "loss": 0.01}), encoding="utf-8"
        )
        assert common.final_metric_from_json(tmp_path, "acc") == pytest.approx(0.99)

    def test_final_metric_from_json_skips_loss(self, tmp_path: Path) -> None:
        common = self._load_common()
        retrain = tmp_path / "runs" / "retrain"
        retrain.mkdir(parents=True)
        (retrain / "test_metrics.json").write_text(
            json.dumps({"loss": 0.01, "mAP": 0.7}), encoding="utf-8"
        )
        assert common.final_metric_from_json(tmp_path, "mAP") == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# compare_table full-supernet metric semantics (problem 3 regression)
# ---------------------------------------------------------------------------


class TestCompareTableFullMetric:
    def _run_compare(self, tmp_path: Path, selected_acc: str = "0.9892") -> dict:
        """Run compare_table.main() with monkeypatched push_chart; return last push."""
        import importlib.util

        # Do NOT stub sys.modules["orca"]: compare_table imports only _common, and
        # _common's `try: from orca.chart import render_chart` harmlessly degrades
        # to render_chart=None when orca is absent. Stubbing "orca" here leaks a
        # broken orca module into the shared sys.modules and breaks later tests
        # (test_workflow_viz_audit_fixes) that import real orca code.
        sys.path.insert(0, str(_RETRAIN_SCRIPTS_DIR))
        try:
            spec = importlib.util.spec_from_file_location(
                "compare_table_under_test",
                str(_RETRAIN_SCRIPTS_DIR / "compare_table.py"),
            )
            ct = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(ct)
        finally:
            # compare_table.py itself inserts its script dir into sys.path at import;
            # drop both our insertion and the module's, so no ns_retrain path leaks.
            sys.path = [p for p in sys.path if p != str(_RETRAIN_SCRIPTS_DIR)]
            # Same cross-dir cleanup: the module body imports _common, which must
            # not linger in sys.modules for later quant/structure tests.
            sys.modules.pop("_common", None)
            sys.modules.pop("compare_table_under_test", None)

        calls: list[dict] = []
        ct.push_chart = lambda **kw: calls.append(kw)
        old_argv = sys.argv
        sys.argv = [
            "compare_table",
            "--artifacts-dir", str(tmp_path),
            "--selected-latency", "0.2558",
            "--selected-acc", selected_acc,
        ]
        try:
            ct.main()
        finally:
            sys.argv = old_argv
        return calls[-1]

    def test_uses_train_log_best_when_available(self, tmp_path: Path) -> None:
        """Train log best validation → full_metric, not candidate extremes."""
        # Copy NAS-format search data (with one NaN sentinel to prove filtering).
        records = [
            {"objs": {"acc": -0.9884, "latency": 0.2}, "pareto": True},
            {"objs": {"acc": 3.402823e38, "latency": 0.5}, "pareto": False},
            {"objs": {"acc": -0.9876, "latency": 0.3}, "pareto": True},
        ]
        for r in records:
            (tmp_path / "search_results.jsonl").open("a").write(json.dumps(r) + "\n")
        (tmp_path / "search_config.yaml").write_text(
            'objs:\n  - "acc"\n  - "latency"\n'
        )
        # Train log present with real best acc.
        runs_train = tmp_path / "runs" / "train"
        runs_train.mkdir(parents=True)
        (runs_train / "train.attempt1.log").write_text(
            '{"epoch": 5, "test_acc": 0.9884}\n', encoding="utf-8"
        )

        pushed = self._run_compare(tmp_path)
        acc_row = next(r for r in pushed["data"] if r["metric"] == "Acc")
        assert acc_row["Full Supernet"] == "0.9884"
        assert acc_row["Selected Subnet"] == "0.9892"
        assert "train-log best validation" in pushed["caption"]

    def test_fallback_to_search_best_when_log_missing(
        self, tmp_path: Path
    ) -> None:
        """No train log → fallback to best search candidate (min negated), NaN filtered."""
        records = [
            {"objs": {"acc": -0.985, "latency": 0.2}, "pareto": True},
            {"objs": {"acc": -0.989, "latency": 0.3}, "pareto": True},
            {"objs": {"acc": 3.402823e38, "latency": 0.9}, "pareto": False},
        ]
        for r in records:
            (tmp_path / "search_results.jsonl").open("a").write(json.dumps(r) + "\n")
        (tmp_path / "search_config.yaml").write_text(
            'objs:\n  - "acc"\n  - "latency"\n'
        )

        pushed = self._run_compare(tmp_path)
        acc_row = next(r for r in pushed["data"] if r["metric"] == "Acc")
        # min negated acc = -0.989 → for_display → 0.989 (NOT the NaN, NOT worst).
        assert acc_row["Full Supernet"] == "0.989"
        assert "search best candidate" in pushed["caption"]

    def test_selected_not_double_negated(self, tmp_path: Path) -> None:
        """selected_acc from select stdout is already natural; never negated again."""
        records = [{"objs": {"acc": -0.98, "latency": 0.2}, "pareto": True}]
        for r in records:
            (tmp_path / "search_results.jsonl").open("a").write(json.dumps(r) + "\n")
        (tmp_path / "search_config.yaml").write_text(
            'objs:\n  - "acc"\n  - "latency"\n'
        )

        pushed = self._run_compare(tmp_path, selected_acc="0.9892")
        acc_row = next(r for r in pushed["data"] if r["metric"] == "Acc")
        assert acc_row["Selected Subnet"] == "0.9892"
        assert not acc_row["Selected Subnet"].startswith("-")


# ---------------------------------------------------------------------------
# search_table: arch digest + per-arch dedup
# ---------------------------------------------------------------------------


class TestSearchTable:
    def _load(self):
        """Load the ns_run_search search_table module fresh (cache-safe)."""
        import importlib.util

        sys.path.insert(0, str(_SEARCH_SCRIPTS_DIR))
        try:
            spec = importlib.util.spec_from_file_location(
                "search_table_under_test",
                str(_SEARCH_SCRIPTS_DIR / "search_table.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        finally:
            sys.path = [p for p in sys.path if p != str(_SEARCH_SCRIPTS_DIR)]
            sys.modules.pop("_common", None)
            sys.modules.pop("search_table_under_test", None)

    def test_arch_digest_readable(self):
        """arch dict renders as a readable per-stage digest, not '(see arch)'."""
        st = self._load()
        flat = {
            "arch.layer_configs": {
                "stage1": [
                    {"choice": "res_conv", "config": {"hidden_channels": 32, "kernel_size": 3}},
                    {"choice": "mnist_cnn", "config": {"kernel_size": 3}},
                ],
                "stage2": [{"choice": "mnist_cnn", "config": {"kernel_size": 5}}],
            },
            "arch.stage_depths": [2, 1],
        }
        digest = st._arch_digest(flat, set())
        assert "stage1: res_conv(hidden_channels=32,kernel_size=3)+mnist_cnn(kernel_size=3)" in digest
        assert "stage2: mnist_cnn(kernel_size=5)" in digest
        assert digest != "(see arch)"

    def test_arch_digest_fallback_no_arch(self):
        """No arch keys -> falls back to scalar fields, else '(see arch)'."""
        st = self._load()
        assert st._arch_digest({"acc": -0.9}, {"acc", "objs", "gene"}) == "(see arch)"
        digest = st._arch_digest({"foo": "bar"}, set())
        assert digest == "foo=bar"

    def test_layer_configs_str(self):
        st = self._load()
        lc = {
            "stage1": [{"choice": "a", "config": {"kernel_size": 3}}],
            "stage2": [{"choice": "b", "config": {"expand_channels": 64}}],
        }
        out = st._arch_layer_configs_to_str(lc)
        assert out == "stage1: a(kernel_size=3); stage2: b(expand_channels=64)"

    def test_main_dedups_by_arch(self, tmp_path: Path):
        """640->deduped rows: same arch (across generations) kept once, pareto first."""
        import importlib.util
        import types

        arch = {"layer_configs": {"stage1": [{"choice": "mnist_cnn", "config": {"kernel_size": 3}}]}, "stage_depths": [1, 1]}
        arch2 = {"layer_configs": {"stage1": [{"choice": "res_conv", "config": {"hidden_channels": 16, "kernel_size": 3}}]}, "stage_depths": [1, 1]}
        # 3 records: arch duplicated across generations (non-pareto then pareto), arch2 once.
        recs = [
            {"generation": 0, "gene": [0], "objs": {"acc": -0.95, "latency": 0.2}, "pareto": False, "arch": arch},
            {"generation": 1, "gene": [0], "objs": {"acc": -0.95, "latency": 0.2}, "pareto": True, "arch": arch},
            {"generation": 0, "gene": [1], "objs": {"acc": -0.93, "latency": 0.3}, "pareto": True, "arch": arch2},
        ]
        for r in recs:
            (tmp_path / "search_results.jsonl").open("a").write(json.dumps(r) + "\n")
        (tmp_path / "search_config.yaml").write_text('objs:\n  - "acc"\n  - "latency"\n')

        orca_mod = types.ModuleType("orca")
        orca_mod.chart = types.SimpleNamespace()
        sys.modules["orca"] = orca_mod
        calls: list[dict] = []
        sys.path.insert(0, str(_SEARCH_SCRIPTS_DIR))
        try:
            spec = importlib.util.spec_from_file_location(
                "search_table_under_test2",
                str(_SEARCH_SCRIPTS_DIR / "search_table.py"),
            )
            st = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(st)
        finally:
            sys.path = [p for p in sys.path if p != str(_SEARCH_SCRIPTS_DIR)]
            sys.modules.pop("_common", None)
            sys.modules.pop("search_table_under_test2", None)
            sys.modules.pop("orca", None)
        st.push_chart = lambda **kw: calls.append(kw)
        old_argv = sys.argv
        sys.argv = ["search_table", "--artifacts-dir", str(tmp_path)]
        try:
            st.main()
        finally:
            sys.argv = old_argv
        pushed = calls[-1]
        # arch deduped: 2 unique architectures, not 3 records.
        assert len(pushed["data"]) == 2
        # arch digest readable, no '(see arch)'.
        assert all(r["arch"] != "(see arch)" for r in pushed["data"])
        # The duplicated arch kept the pareto=True representative.
        rep = next(r for r in pushed["data"] if r["arch"].startswith("stage1: mnist_cnn"))
        assert rep["pareto"] == "yes"

    def test_only_pareto_rows_shown(self, tmp_path: Path):
        """Non-pareto architectures with no pareto=yes rows → degrade to all (A1 fix)."""
        import importlib.util
        import types

        arch = {"layer_configs": {"stage1": [{"choice": "mnist_cnn", "config": {"kernel_size": 3}}]}, "stage_depths": [1, 1]}
        recs = [
            {"generation": 0, "gene": [0], "objs": {"acc": -0.95, "latency": 0.2}, "pareto": False, "arch": arch},
        ]
        for r in recs:
            (tmp_path / "search_results.jsonl").open("a").write(json.dumps(r) + "\n")
        (tmp_path / "search_config.yaml").write_text('objs:\n  - "acc"\n  - "latency"\n')

        orca_mod = types.ModuleType("orca")
        orca_mod.chart = types.SimpleNamespace()
        sys.modules["orca"] = orca_mod
        calls: list[dict] = []
        sys.path.insert(0, str(_SEARCH_SCRIPTS_DIR))
        try:
            spec = importlib.util.spec_from_file_location(
                "search_table_under_test3",
                str(_SEARCH_SCRIPTS_DIR / "search_table.py"),
            )
            st = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(st)
        finally:
            sys.path = [p for p in sys.path if p != str(_SEARCH_SCRIPTS_DIR)]
            sys.modules.pop("_common", None)
            sys.modules.pop("search_table_under_test3", None)
            sys.modules.pop("orca", None)
        st.push_chart = lambda **kw: calls.append(kw)
        old_argv = sys.argv
        sys.argv = ["search_table", "--artifacts-dir", str(tmp_path)]
        try:
            st.main()
        finally:
            sys.argv = old_argv
        pushed = calls[-1]
        # A1 fix: all rows are pareto=no → degrade to showing all, NOT empty.
        assert len(pushed["data"]) >= 1, "degradation should show all deduped rows, not empty"
        assert "All Architectures" in pushed["title"]




# ---------------------------------------------------------------------------
# SPEC: latency_unit passthrough + full-supernet measurement + subnet structure
# (docs/specs/2026-08-11-nas-supernet-latency-unit-and-subnet-display.md)
# Acceptance: A1-A6 / B1-B4 / C1-C3 / D1-D5 / byte-identical CI gate (E1).
# ---------------------------------------------------------------------------


def _write_search_record_schema(ad: Path, latency_unit: str | None = "ms") -> None:
    """Write a minimal search_record_schema.json with optional latency_unit."""
    schema = {
        "metric_name": "acc",
        "metric_direction": "higher-better",
        "latency_ms_field": "latency",
    }
    if latency_unit is not None:
        schema["latency_unit"] = latency_unit
    (ad / "search_record_schema.json").write_text(json.dumps(schema), encoding="utf-8")


def _nas_records(ad: Path, lats: list[float], accs: list[float] | None = None) -> None:
    """Write NAS-format search_results.jsonl with given latency values."""
    if accs is None:
        accs = [-0.9 - 0.01 * i for i in range(len(lats))]
    for lat, acc in zip(lats, accs):
        (ad / "search_results.jsonl").open("a").write(
            json.dumps({"objs": {"acc": acc, "latency": lat}, "pareto": True}) + "\n"
        )
    (ad / "search_config.yaml").write_text("objs:\n  - \"acc\"\n  - \"latency\"\n")


def _load_script_fresh(script_path: Path, mod_name: str):
    """Load a chart script as a fresh module, cache-safe."""
    import importlib.util
    sys.path.insert(0, str(script_path.parent))
    try:
        spec = importlib.util.spec_from_file_location(mod_name, str(script_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path = [p for p in sys.path if p != str(script_path.parent)]
        sys.modules.pop("_common", None)
        sys.modules.pop(mod_name, None)


def _load_common_cached():
    """Load the chart _common fresh (cache-safe)."""
    sys.path.insert(0, str(_SEARCH_SCRIPTS_DIR))
    try:
        import _common
        return _common
    finally:
        sys.path.remove(str(_SEARCH_SCRIPTS_DIR))
        sys.modules.pop("_common", None)


class TestDiscoverLatencyUnit:
    """SPEC A3 - discover_latency_unit: schema-driven unit discovery."""

    def test_schema_us(self, tmp_path: Path) -> None:
        _write_search_record_schema(tmp_path, "us")
        common = _load_common_cached()
        assert common.discover_latency_unit(tmp_path) == "us"

    def test_schema_s(self, tmp_path: Path) -> None:
        _write_search_record_schema(tmp_path, "s")
        common = _load_common_cached()
        assert common.discover_latency_unit(tmp_path) == "s"

    def test_no_schema_file(self, tmp_path: Path) -> None:
        common = _load_common_cached()
        assert common.discover_latency_unit(tmp_path) == "ms"

    def test_schema_missing_key(self, tmp_path: Path) -> None:
        _write_search_record_schema(tmp_path, latency_unit=None)
        common = _load_common_cached()
        assert common.discover_latency_unit(tmp_path) == "ms"

    def test_schema_illegal_value_falls_back_with_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        _write_search_record_schema(tmp_path, "xyz")
        common = _load_common_cached()
        assert common.discover_latency_unit(tmp_path) == "ms"
        captured = capsys.readouterr()
        assert "not in" in captured.err


class TestLatencyUnitLabelsA1A2:
    """SPEC A1/A2 - unit appears in labels/columns; default -> ms."""

    def _run(self, script: str, ad: Path, *extra: str) -> dict:
        if script == "compare_table":
            path = _RETRAIN_SCRIPTS_DIR / "compare_table.py"
        else:
            path = _SEARCH_SCRIPTS_DIR / f"{script}.py"
        mod = _load_script_fresh(path, f"{script}_a1a2")
        calls: list[dict] = []
        mod.push_chart = lambda **kw: calls.append(kw)
        old_argv = sys.argv
        sys.argv = [script, "--artifacts-dir", str(ad), *extra]
        try:
            mod.main()
        finally:
            sys.argv = old_argv
        return calls[-1]

    def test_a1_us_unit_propagates_to_all_chart_labels(self, tmp_path: Path) -> None:
        """A1: latency_unit=us - latency_dist/pareto/search_table/compare_table carry us."""
        _write_search_record_schema(tmp_path, "us")
        _nas_records(tmp_path, [0.1, 0.2, 0.3])

        ld = self._run("latency_dist", tmp_path, "--latency-unit", "us")
        assert "us" in ld["x_label"]

        pa = self._run("pareto", tmp_path, "--latency-unit", "us",
                       "--selected-latency", "0.2", "--selected-acc", "0.9")
        assert "us" in pa["x_label"]
        assert "latency=0.20us" in pa["caption"]

        st = self._run("search_table", tmp_path, "--latency-unit", "us")
        assert "latency_us" in st["columns"]
        assert all("latency_us" in row for row in st["data"])

        ct = self._run("compare_table", tmp_path, "--latency-unit", "us",
                       "--selected-latency", "0.2", "--selected-acc", "0.9")
        assert any("us" in r["metric"] for r in ct["data"])

    def test_a2_default_ms_when_schema_absent(self, tmp_path: Path) -> None:
        """A2: no latency_unit declared -> ms everywhere (regression)."""
        _nas_records(tmp_path, [0.1, 0.2])
        pa = self._run("pareto", tmp_path,
                       "--selected-latency", "0.1", "--selected-acc", "0.9")
        assert "ms" in pa["x_label"]
        assert "latency=0.10ms" in pa["caption"]


class TestF7RenameCliBackcompat:
    """SPEC F7/F4 - selected_latency_ms -> selected_latency CLI rename + caption keeps metric clause."""

    def test_pareto_accepts_selected_latency_flag(self, tmp_path: Path) -> None:
        _write_search_record_schema(tmp_path)
        _nas_records(tmp_path, [0.1, 0.2])
        mod = _load_script_fresh(_SEARCH_SCRIPTS_DIR / "pareto.py", "pareto_rename")
        calls: list[dict] = []
        mod.push_chart = lambda **kw: calls.append(kw)
        old_argv = sys.argv
        sys.argv = ["pareto", "--artifacts-dir", str(tmp_path),
                    "--selected-latency", "0.15", "--selected-acc", "0.95"]
        try:
            mod.main()
        finally:
            sys.argv = old_argv
        assert "Selected arch: latency=0.15ms" in calls[-1]["caption"]
        # F4: metric clause `, acc=<number>` is kept (only literal ms is parameterized).
        # The value sign follows pareto.py's for_display rule for selected_acc (pre-existing).
        assert "acc=" in calls[-1]["caption"]


class TestLatencyDistDiagnosticC1C2C3:
    """SPEC C1/C2/C3 - all-sentinel / all-zero / normal diagnostics (F6: push_chart not skip)."""

    def _run(self, ad: Path) -> dict:
        mod = _load_script_fresh(_SEARCH_SCRIPTS_DIR / "latency_dist.py", "latency_dist_diag")
        calls: list[dict] = []
        mod.push_chart = lambda **kw: calls.append(kw)
        old_argv = sys.argv
        sys.argv = ["latency_dist", "--artifacts-dir", str(ad)]
        try:
            mod.main()
        finally:
            sys.argv = old_argv
        return calls[-1]

    def test_c1_all_sentinel_placeholder_bar(self, tmp_path: Path) -> None:
        """C1/F6: all values NaN/overflow sentinel -> placeholder bar via push_chart (NOT skip)."""
        _write_search_record_schema(tmp_path)
        _nas_records(tmp_path, [3.402823e38, 3.402823e38])
        pushed = self._run(tmp_path)
        assert pushed.get("skip_reason", "") == ""
        assert pushed["data"] == [{"bin": "(no valid data)", "count": 0}]
        cap = pushed["caption"]
        assert "NaN/overflow sentinels" in cap
        assert "measurement likely failed" in cap

    def test_c2_all_zero_placeholder_bar(self, tmp_path: Path) -> None:
        """C2: every latency = 0.0 -> placeholder bar + timer-resolution diagnostic."""
        _write_search_record_schema(tmp_path)
        _nas_records(tmp_path, [0.0, 0.0, 0.0])
        pushed = self._run(tmp_path)
        assert pushed.get("skip_reason", "") == ""
        assert pushed["data"] == [{"bin": "(no valid data)", "count": 0}]
        cap = pushed["caption"]
        assert "All latency values are 0.0" in cap
        assert "timer resolution" in cap

    def test_c3_normal_data_no_diagnostic(self, tmp_path: Path) -> None:
        """C3: normal latency distribution -> no diagnostic strings in caption."""
        _write_search_record_schema(tmp_path)
        _nas_records(tmp_path, [0.1, 0.2, 0.3, 0.4])
        pushed = self._run(tmp_path)
        cap = pushed["caption"]
        assert "NaN/overflow sentinels" not in cap
        assert "timer resolution" not in cap
        assert len(pushed["data"]) > 1


class TestCompareTableFullSupernetLatencyB1B2B3:
    """SPEC B1/B2/B3 - compare_table prefers .full_supernet_latency.json, proxy fallback."""

    def _run_compare(self, ad: Path, sel_lat: str = "0.2", sel_acc: str = "0.95") -> dict:
        mod = _load_script_fresh(_RETRAIN_SCRIPTS_DIR / "compare_table.py", "compare_b1b3")
        calls: list[dict] = []
        mod.push_chart = lambda **kw: calls.append(kw)
        old_argv = sys.argv
        sys.argv = ["compare_table", "--artifacts-dir", str(ad),
                    "--selected-latency", sel_lat, "--selected-acc", sel_acc]
        try:
            mod.main()
        finally:
            sys.argv = old_argv
        return calls[-1]

    def test_b2_prefers_full_supernet_latency_file(self, tmp_path: Path) -> None:
        """B2: .full_supernet_latency.json present -> use its value + real-measurement caption."""
        _write_search_record_schema(tmp_path)
        _nas_records(tmp_path, [0.1, 0.2, 0.3])
        (tmp_path / ".full_supernet_latency.json").write_text(
            json.dumps({"latency": 0.45, "unit": "ms", "source": "estimator"}),
            encoding="utf-8",
        )
        pushed = self._run_compare(tmp_path)
        lat_row = next(r for r in pushed["data"] if r["metric"].startswith("Latency"))
        assert lat_row["Full Supernet"] == "0.45"
        assert "real measurement" in pushed["caption"]
        assert ".full_supernet_latency.json" in pushed["caption"]

    def test_b2_falls_back_to_proxy_when_file_missing(self, tmp_path: Path) -> None:
        """B2: no .full_supernet_latency.json -> max(candidate) proxy + proxy caption."""
        _write_search_record_schema(tmp_path)
        _nas_records(tmp_path, [0.1, 0.2, 0.3])
        pushed = self._run_compare(tmp_path)
        lat_row = next(r for r in pushed["data"] if r["metric"].startswith("Latency"))
        assert lat_row["Full Supernet"] == "0.3"
        assert "proxy" in pushed["caption"]

    def test_b3_proxy_filters_sentinel(self, tmp_path: Path) -> None:
        """B3: 3.4e38 sentinel mixed in candidates -> proxy does not surface it."""
        _write_search_record_schema(tmp_path)
        _nas_records(tmp_path, [0.1, 3.402823e38, 0.2])
        pushed = self._run_compare(tmp_path)
        lat_row = next(r for r in pushed["data"] if r["metric"].startswith("Latency"))
        assert lat_row["Full Supernet"] == "0.2"

    def test_b1_zero_latency_is_legitimate_n6(self, tmp_path: Path) -> None:
        """N6: latency=0.0 from estimator is legitimate (NOT filtered); file is used."""
        _write_search_record_schema(tmp_path)
        _nas_records(tmp_path, [0.1, 0.2])
        (tmp_path / ".full_supernet_latency.json").write_text(
            json.dumps({"latency": 0.0, "unit": "ms", "source": "estimator"}),
            encoding="utf-8",
        )
        pushed = self._run_compare(tmp_path)
        lat_row = next(r for r in pushed["data"] if r["metric"].startswith("Latency"))
        assert lat_row["Full Supernet"] == "0"


class TestFullSupernetLatencyFailSoftB4:
    """SPEC B4 - torch/supernet missing -> no file + exit 0 (fail-soft N3)."""

    def test_b4_no_supernet_writes_stderr_no_file(self, tmp_path: Path) -> None:
        _write_search_record_schema(tmp_path, "ms")
        mod = _load_script_fresh(
            _SEARCH_SCRIPTS_DIR / "full_supernet_latency.py", "full_latency_b4",
        )
        old_argv = sys.argv
        sys.argv = ["full_supernet_latency", "--artifacts-dir", str(tmp_path)]
        try:
            rc = mod.main()
        finally:
            sys.argv = old_argv
        assert rc == 0
        assert not (tmp_path / ".full_supernet_latency.json").is_file()


class TestSelectLatencyUnitA4:
    """SPEC A4/N1 - select --target-latency + --latency-unit behaves numerically identical
    regardless of unit label (no value conversion; unit is metadata only).

    Uses a fixture select_architecture.py matching the new contract (N1: real select is
    runtime-generated, can't be tested directly).
    """

    @staticmethod
    def _write_fixture_select(tmp_path: Path) -> Path:
        fixture = tmp_path / "select_architecture.py"
        fixture.write_text(
            "import argparse, json\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument('--target-latency', type=float)\n"
            "ap.add_argument('--latency-unit', default='ms')\n"
            "ap.add_argument('--search-results', required=True)\n"
            "args = ap.parse_args()\n"
            "best = None\n"
            "for line in open(args.search_results):\n"
            "    r = json.loads(line)\n"
            "    lat = r['objs']['latency']\n"
            "    if lat <= args.target_latency:\n"
            "        acc = -r['objs']['acc']\n"
            "        if best is None or acc > best[0]:\n"
            "            best = (acc, lat, r['arch'])\n"
            "if best is None:\n"
            "    out = {'selected_arch': {}, 'selected_acc': 0, 'selected_latency': 0,\n"
            "           'latency_unit': args.latency_unit, 'pareto_size': 0,\n"
            "           'select_reason': 'none'}\n"
            "else:\n"
            "    out = {'selected_arch': best[2], 'selected_acc': best[0],\n"
            "           'selected_latency': best[1], 'latency_unit': args.latency_unit,\n"
            "           'pareto_size': 1, 'select_reason': 'max-acc-under-target'}\n"
            "print(json.dumps(out))\n",
            encoding="utf-8",
        )
        return fixture

    def test_a4_same_value_same_selection_regardless_of_unit_label(self, tmp_path: Path) -> None:
        """Numerical latency <= target is unit-agnostic - same selection, unit only differs as label."""
        records = [
            {"objs": {"acc": -0.90, "latency": 0.1}, "arch": {"d": 1}},
            {"objs": {"acc": -0.95, "latency": 0.2}, "arch": {"d": 2}},
            {"objs": {"acc": -0.93, "latency": 0.3}, "arch": {"d": 3}},
        ]
        for r in records:
            (tmp_path / "search_results.jsonl").open("a").write(json.dumps(r) + "\n")
        fixture = self._write_fixture_select(tmp_path)

        import subprocess
        r_us = subprocess.run(
            ["python3", str(fixture), "--target-latency", "0.5", "--latency-unit", "us",
             "--search-results", str(tmp_path / "search_results.jsonl")],
            capture_output=True, text=True,
        )
        r_ms = subprocess.run(
            ["python3", str(fixture), "--target-latency", "0.5", "--latency-unit", "ms",
             "--search-results", str(tmp_path / "search_results.jsonl")],
            capture_output=True, text=True,
        )
        assert r_us.returncode == 0 and r_ms.returncode == 0, \
            f"us stderr={r_us.stderr!r}; ms stderr={r_ms.stderr!r}"
        out_us = json.loads(r_us.stdout)
        out_ms = json.loads(r_ms.stdout)
        assert out_us["selected_arch"] == out_ms["selected_arch"]
        assert out_us["selected_latency"] == out_ms["selected_latency"] == 0.2
        assert out_us["latency_unit"] == "us"
        assert out_ms["latency_unit"] == "ms"


class TestF1BootstrapInvariantA6:
    """SPEC A6/F1 - latency_unit in {us,s} + empty latency_script_path -> fail-loud."""

    def test_a6_us_unit_without_script_rejected(self) -> None:
        from orca.iface.in_session.cli import _validate_input_invariants
        from orca.schema.workflow import InputInvariant
        inv = InputInvariant(
            when_field="latency_unit",
            when_in=["us", "s"],
            require_nonempty=["latency_script_path"],
            message="non-ms unit requires user script",
        )
        ok, err = _validate_input_invariants(
            {"latency_unit": "us", "latency_script_path": ""}, [inv],
        )
        assert not ok
        assert "non-ms unit requires user script" in err
        assert "latency_unit" in err and "us" in err

    def test_a6_us_unit_with_script_passes(self) -> None:
        from orca.iface.in_session.cli import _validate_input_invariants
        from orca.schema.workflow import InputInvariant
        inv = InputInvariant(
            when_field="latency_unit", when_in=["us", "s"],
            require_nonempty=["latency_script_path"], message="x",
        )
        ok, _ = _validate_input_invariants(
            {"latency_unit": "us", "latency_script_path": "/x/onnx.py"}, [inv],
        )
        assert ok

    def test_a6_ms_unit_without_script_passes(self) -> None:
        """ms + no script is the default path - estimator returns ms natively."""
        from orca.iface.in_session.cli import _validate_input_invariants
        from orca.schema.workflow import InputInvariant
        inv = InputInvariant(
            when_field="latency_unit", when_in=["us", "s"],
            require_nonempty=["latency_script_path"], message="x",
        )
        ok, _ = _validate_input_invariants(
            {"latency_unit": "ms", "latency_script_path": ""}, [inv],
        )
        assert ok

    def test_a6_orchestrator_invariant_path(self) -> None:
        """Orchestrator.__init__ mirrors the invariant on tars run / TUI / daemon paths.

        Constructs a real Workflow + invokes Orchestrator(...) — catches any future
        drift in the Orchestrator's invariant loop (a hand-rolled mirror test would not).
        """
        from orca.schema.workflow import Workflow, InputDef, InputInvariant, AgentNode
        from orca.run.orchestrator import Orchestrator
        wf = Workflow(
            name="test_wf",
            entry="n",
            inputs={
                "latency_unit": InputDef(type="string", required=False, default="ms"),
                "latency_script_path": InputDef(type="string", required=False, default=""),
            },
            input_invariants=[InputInvariant(
                when_field="latency_unit", when_in=["us", "s"],
                require_nonempty=["latency_script_path"], message="bad",
            )],
            nodes=[AgentNode(name="n", prompt="placeholder")],
        )
        # Violation: latency_unit=us + empty script -> ValueError raised by __init__.
        with pytest.raises(ValueError, match="input invariant violated"):
            Orchestrator(wf, bus=None, inputs={"latency_unit": "us", "latency_script_path": ""})


class TestCheckReportShGate:
    """Regression: check_report.sh required-field list must match ns2_report output_schema.

    The reviewer caught a previous bug where the F7 rename updated ns2_report agent.md
    to emit `selected_latency` but check_report.sh still required the old `selected_latency_ms`,
    causing every reporter run to fail the deterministic gate. This test feeds a canonical
    reporter JSON through check_report.sh and asserts it PASSES — catches future schema drift.
    """

    _REPORT_PATH = Path(__file__).resolve().parents[2] / "workflows" / "agents" / "ns2_report" / "scripts" / "check_report.sh"

    def test_check_report_passes_with_new_schema_fields(self, tmp_path: Path) -> None:
        """Reporter JSON with selected_latency/latency_unit/subnet_structure passes the gate."""
        import subprocess
        report = {
            "status": "success",
            "stage": "retrain",
            "reason": "ok",
            "selected_arch": {"d": 3},
            "selected_acc": 0.95,
            "selected_latency": 0.25,
            "latency_unit": "ms",
            "subnet_structure": "subnet_structure.md",
            "pareto_size": 12,
            "supernet_path": "supernet.py",
            "output_dir": str(tmp_path),
            "final_metrics": "acc=0.95",
            "artifacts": ["supernet.py"],
            "charts_summary": "no chart files found",
            "error": "",
        }
        (tmp_path / ".report.json").write_text(json.dumps(report), encoding="utf-8")
        env = {**__import__("os").environ, "ORCA_ARTIFACTS_DIR": str(tmp_path)}
        result = subprocess.run(
            ["bash", str(self._REPORT_PATH)],
            capture_output=True, text=True, env=env, cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"gate failed: {result.stdout}\n{result.stderr}"
        assert "PASS" in result.stdout

    def test_check_report_rejects_old_field_name_only(self, tmp_path: Path) -> None:
        """Reporter JSON with ONLY the old `selected_latency_ms` (no `selected_latency`) fails the gate."""
        import subprocess
        report = {
            "status": "success", "stage": "retrain", "reason": "ok",
            "selected_arch": {"d": 3}, "selected_acc": 0.95,
            "selected_latency_ms": 0.25,  # OLD name only
            "pareto_size": 12, "supernet_path": "supernet.py",
            "output_dir": str(tmp_path), "final_metrics": "", "artifacts": [],
            "charts_summary": "", "error": "",
            # Missing required new fields: latency_unit, subnet_structure, selected_latency.
        }
        (tmp_path / ".report.json").write_text(json.dumps(report), encoding="utf-8")
        env = {**__import__("os").environ, "ORCA_ARTIFACTS_DIR": str(tmp_path)}
        result = subprocess.run(
            ["bash", str(self._REPORT_PATH)],
            capture_output=True, text=True, env=env, cwd=str(tmp_path),
        )
        assert result.returncode != 0, "gate should reject JSON missing new required fields"
        assert "FAIL" in result.stdout


class TestParetoCaptionParseF4:
    """SPEC F4 - _parse_selected_from_caption regex matches all 3 units."""

    def test_ms_caption_parses(self) -> None:
        common = _load_common_cached()
        cap = "Selected arch: latency=0.25ms, acc=0.9500."
        assert common._parse_selected_from_caption(cap) == (0.25, 0.95)

    def test_us_caption_parses(self) -> None:
        common = _load_common_cached()
        cap = "Selected arch: latency=12.50us, acc=0.9500."
        assert common._parse_selected_from_caption(cap) == (12.5, 0.95)

    def test_s_caption_parses(self) -> None:
        common = _load_common_cached()
        cap = "Selected arch: latency=0.001s, acc=0.9500."
        assert common._parse_selected_from_caption(cap) == (0.001, 0.95)

    def test_metric_clause_is_kept_f4(self) -> None:
        """F4: caption keeps `, {metric}={val:.4f}` clause (only literal ms -> unit)."""
        cap = "Selected arch: latency=0.25us, my_metric=0.1234."
        common = _load_common_cached()
        assert common._parse_selected_from_caption(cap) == (0.25, 0.1234)


class TestCommonPyByteIdenticalE1:
    """SPEC E1/N5 - CI gate: all ns*_run_search/ns*_retrain _common.py byte-identical."""

    def test_common_py_byte_identical_all_versions(self) -> None:
        import hashlib
        agents_dir = _SEARCH_SCRIPTS_DIR.parent.parent
        sub_dirs = (
            "ns_run_search", "ns2_run_search", "ns3_run_search",
            "ns_retrain", "ns2_retrain", "ns3_retrain",
        )
        copies = [agents_dir / sub / "scripts" / "_common.py" for sub in sub_dirs]
        for path in copies:
            assert path.is_file(), f"missing _common.py: {path}"
        hashes = {str(p): hashlib.md5(p.read_bytes()).hexdigest() for p in copies}
        unique = set(hashes.values())
        assert len(unique) == 1, (
            f"all _common.py copies must be byte-identical (SPEC E1/N5); got {hashes}"
        )


class TestNewScriptsByteIdenticalAllVersions:
    """SPEC E1 - chart scripts byte-identical across all nas-supernet versions."""

    @staticmethod
    def _assert_identical(sub_dirs, fname) -> None:
        import hashlib
        agents_dir = _SEARCH_SCRIPTS_DIR.parent.parent
        copies = [agents_dir / sub / "scripts" / fname for sub in sub_dirs]
        for path in copies:
            assert path.is_file(), f"missing {fname}: {path}"
        hashes = {str(p): hashlib.md5(p.read_bytes()).hexdigest() for p in copies}
        unique = set(hashes.values())
        assert len(unique) == 1, f"{fname} must be byte-identical across versions; got {hashes}"

    def test_full_supernet_latency_identical(self) -> None:
        self._assert_identical(
            ("ns_run_search", "ns2_run_search", "ns3_run_search"), "full_supernet_latency.py"
        )

    def test_subnet_profile_identical(self) -> None:
        self._assert_identical(
            ("ns_retrain", "ns2_retrain", "ns3_retrain"), "subnet_profile.py"
        )

    def test_compare_table_identical(self) -> None:
        self._assert_identical(
            ("ns_retrain", "ns2_retrain", "ns3_retrain"), "compare_table.py"
        )

    def test_run_search_chart_scripts_identical(self) -> None:
        for fname in ("latency_dist.py", "pareto.py", "search_table.py"):
            self._assert_identical(
                ("ns_run_search", "ns2_run_search", "ns3_run_search"), fname
            )


class TestSearchResultsUnconvertedA5:
    """SPEC A5 - search_results.jsonl latency values NOT converted (unit is metadata)."""

    def test_a5_extract_numeric_values_unchanged_across_units(self, tmp_path: Path) -> None:
        """Same records + schema with latency_unit=ms vs us -> identical numeric values."""
        common = _load_common_cached()
        records = [{"objs": {"acc": -0.9, "latency": 0.123}, "pareto": True}]
        _write_search_record_schema(tmp_path, "ms")
        vals_ms = common.extract_numeric_values(records, "objs.latency")
        _write_search_record_schema(tmp_path, "us")
        vals_us = common.extract_numeric_values(records, "objs.latency")
        assert vals_ms == vals_us == [0.123]


class TestSubnetProfileStructureD1D2D3D4:
    """SPEC D1-D4 - subnet_structure.md shape + content.

    Uses the REAL v2 run-2 artifacts (supernet.py + .selected_arch.json + latency_estimator.py).
    Skips if torch/nas_agent unavailable (subnet_profile.py is fail-soft there).
    """

    _REAL_AD = Path(__file__).resolve().parents[2] / "runs" / "nas-supernet-v2-20260811-020518-9a613e" / "artifacts"

    def _has_torch(self) -> bool:
        try:
            import torch  # noqa: F401
            from nas_agent.blocks.choice_layer import ChoiceLayer  # noqa: F401
            return True
        except ImportError:
            return False

    def test_d1_d2_d3_d4_subnet_structure_md_shape(self, tmp_path: Path) -> None:
        """D1-D4: end-to-end subnet_profile.py against real run-2 supernet.py."""
        if not self._REAL_AD.is_dir():
            pytest.skip(f"real artifacts dir absent: {self._REAL_AD}")
        if not self._has_torch():
            pytest.skip("torch/nas_agent unavailable - D tests need real supernet materialization")

        import shutil
        for fname in ("supernet.py", "latency_estimator.py", "search_config.yaml",
                      "project_manifest.md"):
            src = self._REAL_AD / fname
            if src.is_file():
                shutil.copy(src, tmp_path / fname)
        shutil.copy(self._REAL_AD / ".selected_arch.json", tmp_path / ".selected_arch.json")
        _write_search_record_schema(tmp_path, "ms")

        mod = _load_script_fresh(_RETRAIN_SCRIPTS_DIR / "subnet_profile.py", "subnet_d1d4")
        calls: list[dict] = []
        mod.push_chart = lambda **kw: calls.append(kw)
        old_argv = sys.argv
        sys.argv = ["subnet_profile", "--artifacts-dir", str(tmp_path)]
        try:
            rc = mod.main()
        finally:
            sys.argv = old_argv
        assert rc == 0

        md_path = tmp_path / "subnet_structure.md"
        assert md_path.is_file(), "subnet_structure.md must be written on success"
        text = md_path.read_text(encoding="utf-8")

        # D1: fixed section headers.
        assert "# Selected Subnet Structure" in text
        assert "== Module repr ==" in text
        assert "== Per-layer ==" in text
        assert "layer_name | type | params | out_shape" in text
        # D2: repr contains str(subnet) (>=1 layer class name).
        repr_section = text.split("== Module repr ==")[1].split("== Per-layer ==")[0]
        assert any(tok in repr_section for tok in ("Conv", "Linear", "Pool", "Subnet", "Block"))
        # D3: per-layer >=1 row; total_params positive int.
        per_layer = text.split("== Per-layer ==")[1].strip().splitlines()
        data_rows = [ln for ln in per_layer if "|" in ln and "layer_name" not in ln]
        assert len(data_rows) >= 1
        total_params_line = next(ln for ln in text.splitlines() if ln.startswith("- total_params:"))
        total_params = int(total_params_line.split(":")[1].strip())
        assert total_params > 0
        # D4: weights line present.
        weights_line = next(ln for ln in text.splitlines() if ln.startswith("- weights:"))
        assert "weights:" in weights_line
        # latency_unit passthrough.
        assert "- latency_unit: ms" in text
        # Chart push happened.
        assert len(calls) >= 1
