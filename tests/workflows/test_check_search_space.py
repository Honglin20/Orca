"""Unit tests for ns3_expand_supernet/scripts/check_search_space.py.

The script is a deterministic gate verifying that the generated SearchSpace is a
proper "sandwich" around the prepared model's baseline: for every searchable
dimension (depth + internal-width fields), the candidate set must (1) contain the
baseline, (2) offer a value above it, and (3) offer a value below it.

Covered here:
  - pure helpers: _sandwich_errors (3 failure modes + pass), _gather_field
  - _check_contract: isotropic + staged shapes, pass / capped-at-baseline / missing-baseline
  - main(): end-to-end pass, missing .baseline.json -> fail, missing supernet.py -> fail
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "workflows" / "nas-supernet-v3" / "agents" / "ns3_expand_supernet" / "scripts" / "check_search_space.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("check_search_space_under_test", str(_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _write_isotropic(tmp_path: Path, depth: tuple = (4, 6, 8), ffn: tuple = (512, 1024, 2048)) -> Path:
    (tmp_path / "supernet.py").write_text(
        f"""
from dataclasses import dataclass, field


@dataclass
class SearchSpace:
    global_dim: int = 512
    head_dim: int = 64
    depth_candidates: tuple = {depth}
    layer_configs: dict = field(default_factory=lambda: {{
        "cross_fusion": {{"num_heads": (4, 8, 16), "ffn_dim": {ffn}}},
        "relu_attention": {{"num_heads": (4, 8)}},
    }})
""",
        encoding="utf-8",
    )
    return tmp_path


def _write_staged(tmp_path: Path, depth: tuple = ((1, 2, 3), (2, 3, 4)), expand: tuple = ((32, 64, 96), (64, 96, 128))) -> Path:
    (tmp_path / "supernet.py").write_text(
        f"""
from dataclasses import dataclass, field


@dataclass
class SearchSpace:
    stage_widths: tuple = (32, 64)
    stage_names: tuple = ("stage1", "stage2")
    stage_depth_candidates: tuple = {depth}
    stage_layer_configs: tuple = field(default_factory=lambda: (
        {{"res_conv": {{"kernel_size": (3, 5), "expand_channels": {expand[0]}}}}},
        {{"res_conv": {{"kernel_size": (3, 5), "expand_channels": {expand[1]}}}}},
    ))
""",
        encoding="utf-8",
    )
    return tmp_path


def _write_baseline(tmp_path: Path, data: dict) -> None:
    (tmp_path / ".baseline.json").write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


class TestSandwichErrors:
    def test_pass(self) -> None:
        mod = _load()
        assert mod._sandwich_errors([2, 4, 6], 4, "depth") == []

    def test_baseline_missing(self) -> None:
        mod = _load()
        errs = mod._sandwich_errors([2, 6], 4, "depth")
        assert len(errs) == 1
        assert "baseline 4 missing" in errs[0]

    def test_no_above(self) -> None:
        mod = _load()
        errs = mod._sandwich_errors([2, 3, 4], 4, "depth")
        assert any("no candidate > baseline 4" in e for e in errs)

    def test_no_below(self) -> None:
        mod = _load()
        errs = mod._sandwich_errors([4, 5, 6], 4, "depth")
        assert any("no candidate < baseline 4" in e for e in errs)

    def test_single_value_fails_both_sides(self) -> None:
        mod = _load()
        errs = mod._sandwich_errors([4], 4, "depth")
        assert len(errs) == 2


class TestGatherField:
    def test_union_across_choices(self) -> None:
        mod = _load()
        cfg = {"a": {"ffn_dim": (512, 1024)}, "b": {"ffn_dim": (1024, 2048)}}
        assert sorted(mod._gather_field(cfg, "ffn_dim")) == [512, 1024, 2048]

    def test_missing_field_returns_empty(self) -> None:
        mod = _load()
        assert mod._gather_field({"a": {"other": (1, 2)}}, "ffn_dim") == []


# ---------------------------------------------------------------------------
# _check_contract
# ---------------------------------------------------------------------------


class TestCheckContract:
    def test_isotropic_pass(self, tmp_path: Path) -> None:
        mod = _load()
        _write_isotropic(tmp_path)
        ss, err = mod._load_search_space(tmp_path)
        assert err == ""
        errs = mod._check_contract(ss, {"depth": 6, "internal_dims": {"ffn_dim": 1024, "num_heads": 8}})
        assert errs == []

    def test_isotropic_depth_capped(self, tmp_path: Path) -> None:
        mod = _load()
        _write_isotropic(tmp_path, depth=(2, 4, 6))
        ss, _ = mod._load_search_space(tmp_path)
        errs = mod._check_contract(ss, {"depth": 6, "internal_dims": {"ffn_dim": 1024}})
        assert any("no candidate > baseline 6" in e for e in errs)

    def test_isotropic_internal_width_capped(self, tmp_path: Path) -> None:
        mod = _load()
        _write_isotropic(tmp_path, ffn=(512, 1024))
        ss, _ = mod._load_search_space(tmp_path)
        errs = mod._check_contract(ss, {"depth": 6, "internal_dims": {"ffn_dim": 1024}})
        assert any("ffn_dim: no candidate > baseline 1024" in e for e in errs)

    def test_isotropic_baseline_missing_from_candidates(self, tmp_path: Path) -> None:
        mod = _load()
        _write_isotropic(tmp_path, depth=(4, 6, 8))
        ss, _ = mod._load_search_space(tmp_path)
        errs = mod._check_contract(ss, {"depth": 5, "internal_dims": {}})
        assert any("baseline 5 missing" in e for e in errs)

    def test_staged_pass(self, tmp_path: Path) -> None:
        mod = _load()
        _write_staged(tmp_path)
        ss, _ = mod._load_search_space(tmp_path)
        errs = mod._check_contract(
            ss,
            {
                "stage_depths": {"stage1": 2, "stage2": 3},
                "stage_internal_dims": {"stage1": {"expand_channels": 64}, "stage2": {"expand_channels": 96}},
            },
        )
        assert errs == []

    def test_staged_depth_capped_per_stage(self, tmp_path: Path) -> None:
        mod = _load()
        _write_staged(tmp_path, depth=((1, 2), (2, 3, 4)))
        ss, _ = mod._load_search_space(tmp_path)
        errs = mod._check_contract(
            ss,
            {"stage_depths": {"stage1": 2, "stage2": 3}, "stage_internal_dims": {}},
        )
        assert any("depth[stage1]: no candidate > baseline 2" in e for e in errs)

    def test_neither_depth_field(self, tmp_path: Path) -> None:
        mod = _load()
        (tmp_path / "supernet.py").write_text("class SearchSpace:\n    pass\n", encoding="utf-8")
        ss, _ = mod._load_search_space(tmp_path)
        errs = mod._check_contract(ss, {"depth": 4})
        assert any("neither depth_candidates nor stage_depth_candidates" in e for e in errs)


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


class TestMain:
    def _run(self, ad: Path) -> tuple[int, str]:
        mod = _load()
        old_argv = sys.argv
        old_stderr = sys.stderr
        sys.argv = ["check_search_space", "--artifacts-dir", str(ad)]
        try:
            rc = mod.main()
        finally:
            sys.argv = old_argv
            sys.stderr = old_stderr
        return rc, ""

    def test_end_to_end_pass(self, tmp_path: Path, capsys) -> None:
        _write_isotropic(tmp_path)
        _write_baseline(tmp_path, {"depth": 6, "internal_dims": {"ffn_dim": 1024}})
        rc, _ = self._run(tmp_path)
        assert rc == 0

    def test_end_to_end_fail_capped(self, tmp_path: Path, capsys) -> None:
        _write_isotropic(tmp_path, depth=(2, 4, 6))
        _write_baseline(tmp_path, {"depth": 6, "internal_dims": {"ffn_dim": 1024}})
        rc, _ = self._run(tmp_path)
        assert rc == 1

    def test_missing_baseline_fails_loud(self, tmp_path: Path, capsys) -> None:
        _write_isotropic(tmp_path)
        rc, _ = self._run(tmp_path)
        assert rc == 1
        captured = capsys.readouterr()
        assert ".baseline.json missing" in captured.err

    def test_missing_supernet_fails_loud(self, tmp_path: Path, capsys) -> None:
        rc, _ = self._run(tmp_path)
        assert rc == 1
        captured = capsys.readouterr()
        assert "supernet.py missing" in captured.err
