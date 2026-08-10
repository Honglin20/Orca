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
            "--selected-latency-ms", "0.2558",
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
