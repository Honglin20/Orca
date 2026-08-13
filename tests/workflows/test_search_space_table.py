"""Unit tests for ns3_expand_supernet/scripts/search_space_table.py.

The script is a deterministic sidecar that pushes the generated SearchSpace
(supernet.py) to the frontend as a ``table`` chart. These tests cover:
  - staged SearchSpace shape (stage_layer_configs): one row per (stage, block)
  - isotropic shape (layer_configs): global dim/depth on each block row
  - missing supernet.py / missing levers -> fail-soft (exit 0, no chart)
  - render_chart failure -> static HTML fallback under charts/, exit 0
  - pure helpers (_extract_rows / _fmt_config / _fmt_tuple)

The script execs supernet.py then instantiates SearchSpace; the fixtures below
are self-contained (no torch / nas_agent) so they run in the test env.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "workflows" / "agents" / "ns3_expand_supernet" / "scripts" / "search_space_table.py"
)


def _load_fresh(orca_chart: types.SimpleNamespace):
    """Load search_space_table as a fresh module with a stubbed orca.chart.

    The script imports ``render_chart`` at module load; a stub orca package with
    a captured ``render_chart`` lets us assert on the pushed payload without
    touching the real chart socket. Returns ``(mod, calls)``.
    """
    orca_pkg = types.ModuleType("orca")
    orca_pkg.chart = types.SimpleNamespace(render_chart=orca_chart.render_chart)
    sys.modules["orca"] = orca_pkg
    sys.modules["orca.chart"] = orca_pkg.chart
    calls: list[dict] = []
    inner = orca_chart.render_chart or (lambda **kw: 1)

    def _capture(**kw):
        calls.append(kw)
        return inner(**kw)

    orca_pkg.chart.render_chart = _capture
    spec = importlib.util.spec_from_file_location("search_space_table_under_test", str(_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        return mod, calls
    finally:
        sys.modules.pop("orca", None)
        sys.modules.pop("orca.chart", None)


# ---------------------------------------------------------------------------
# supernet.py fixtures (self-contained SearchSpace, no torch/nas_agent)
# ---------------------------------------------------------------------------


@pytest.fixture
def staged_artifacts(tmp_path: Path) -> Path:
    """Staged (CNN-like) SearchSpace: stage_layer_configs + widths/depths."""
    (tmp_path / "supernet.py").write_text(
        """
from dataclasses import dataclass, field


@dataclass
class SearchSpace:
    stage_widths: tuple = (32, 64)
    stage_names: tuple = ("stage1", "stage2")
    stage_depth_candidates: tuple = ((1, 2), (2, 3))
    stage_layer_configs: tuple = field(default_factory=lambda: (
        {"res_conv": {"kernel_size": (3, 5), "expand_channels": (32, 64)}},
        {"mnist_cnn": {"kernel_size": (3, 5, 7)}},
    ))
""",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def isotropic_artifacts(tmp_path: Path) -> Path:
    """Isotropic SearchSpace: layer_configs + global dim/depth."""
    (tmp_path / "supernet.py").write_text(
        """
from dataclasses import dataclass, field


@dataclass
class SearchSpace:
    global_dim: int = 512
    head_dim: int = 64
    depth_candidates: tuple = (6, 8, 12)
    layer_configs: dict = field(default_factory=lambda: {
        "cross_fusion": {"num_heads": (4, 8), "ffn_dim": (1024, 2048)},
        "relu_attention": {"num_heads": (4, 8)},
    })
""",
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Pure extraction helpers
# ---------------------------------------------------------------------------


class TestFmt:
    def test_fmt_tuple_list(self) -> None:
        mod, _ = _load_fresh(types.SimpleNamespace(render_chart=lambda **kw: None))
        assert mod._fmt_tuple((1, 2, 3)) == "1, 2, 3"
        assert mod._fmt_tuple([4, 5]) == "4, 5"
        assert mod._fmt_tuple(None) == ""

    def test_fmt_config_sorted(self) -> None:
        mod, _ = _load_fresh(types.SimpleNamespace(render_chart=lambda **kw: None))
        cfg = {"ffn_dim": (1024, 2048), "kernel_size": (3, 5)}
        assert mod._fmt_config(cfg) == "ffn_dim=1024, 2048; kernel_size=3, 5"


class TestExtractRows:
    def _extract(self, mod, ss) -> tuple[list[dict], str]:
        return mod._extract_rows(ss)

    def test_staged(self) -> None:
        mod, _ = _load_fresh(types.SimpleNamespace(render_chart=lambda **kw: None))
        import dataclasses

        @dataclasses.dataclass
        class SS:
            stage_widths: tuple = (32, 64)
            stage_names: tuple = ("stage1", "stage2")
            stage_depth_candidates: tuple = ((1, 2), (2, 3))
            stage_layer_configs: tuple = dataclasses.field(default_factory=lambda: (
                {"res_conv": {"kernel_size": (3, 5), "expand_channels": (32, 64)}},
                {"mnist_cnn": {"kernel_size": (3, 5, 7)}},
            ))

        rows, err = self._extract(mod, SS())
        assert err == ""
        assert len(rows) == 2
        r0 = rows[0]
        assert r0["stage"] == "stage1"
        assert r0["block"] == "res_conv"
        assert r0["depth"] == "1, 2"
        assert r0["fixed"] == "width=32"
        assert "kernel_size=3, 5" in r0["config"] and "expand_channels=32, 64" in r0["config"]
        assert rows[1]["fixed"] == "width=64"

    def test_isotropic(self) -> None:
        mod, _ = _load_fresh(types.SimpleNamespace(render_chart=lambda **kw: None))
        import dataclasses

        @dataclasses.dataclass
        class SS:
            global_dim: int = 512
            head_dim: int = 64
            depth_candidates: tuple = (6, 8, 12)
            layer_configs: dict = dataclasses.field(default_factory=lambda: {
                "cross_fusion": {"num_heads": (4, 8), "ffn_dim": (1024, 2048)},
                "relu_attention": {"num_heads": (4, 8)},
            })

        rows, err = self._extract(mod, SS())
        assert err == ""
        assert len(rows) == 2
        assert rows[0]["stage"] == "isotropic"
        assert rows[0]["depth"] == "6, 8, 12"
        assert rows[0]["fixed"] == "global_dim=512; head_dim=64"
        assert "ffn_dim=1024, 2048" in rows[0]["config"]

    def test_no_levers(self) -> None:
        mod, _ = _load_fresh(types.SimpleNamespace(render_chart=lambda **kw: None))

        class Empty:
            pass

        rows, err = mod._extract_rows(Empty())
        assert rows == []
        assert "no stage_layer_configs or layer_configs" in err


# ---------------------------------------------------------------------------
# main(): end-to-end push / fail-soft
# ---------------------------------------------------------------------------


class TestMain:
    def _run(self, ad: Path, orca_chart: types.SimpleNamespace) -> tuple[int, list[dict]]:
        mod, calls = _load_fresh(orca_chart)
        old_argv = sys.argv
        sys.argv = ["search_space_table", "--artifacts-dir", str(ad)]
        try:
            rc = mod.main()
        finally:
            sys.argv = old_argv
        return rc, calls

    def test_staged_pushes_table(self, staged_artifacts: Path) -> None:
        rc, calls = self._run(staged_artifacts, types.SimpleNamespace(render_chart=lambda **kw: None))
        assert rc == 0
        assert len(calls) == 1
        payload = calls[0]
        assert payload["chart_type"] == "table"
        assert payload["label"] == "nas-supernet/search-space"
        assert payload["title"] == "Search Space"
        assert payload["columns"] == ["stage", "block", "depth", "fixed", "config"]
        assert len(payload["data"]) == 2
        assert "2 (stage, block choice)" in payload["caption"]

    def test_isotropic_pushes_table(self, isotropic_artifacts: Path) -> None:
        rc, calls = self._run(isotropic_artifacts, types.SimpleNamespace(render_chart=lambda **kw: None))
        assert rc == 0
        assert len(calls) == 1
        assert all(r["stage"] == "isotropic" for r in calls[0]["data"])

    def test_missing_supernet_fail_soft(self, tmp_path: Path) -> None:
        rc, calls = self._run(tmp_path, types.SimpleNamespace(render_chart=lambda **kw: None))
        assert rc == 0
        assert calls == []
        marker = tmp_path / ".nas-supernet_charts.jsonl"
        assert marker.is_file()
        rec = [r for r in marker.read_text().splitlines() if r.strip()][-1]
        import json
        assert json.loads(rec)["status"] == "skipped"

    def test_render_failure_falls_back_to_static(self, staged_artifacts: Path) -> None:
        def _raise(**kw):
            raise RuntimeError("无法连接 Orca chart socket")

        rc, calls = self._run(staged_artifacts, types.SimpleNamespace(render_chart=_raise))
        assert rc == 0
        assert len(calls) == 1  # render_chart was attempted once, then fell back
        html_path = staged_artifacts / "charts" / "search_space_table.html"
        assert html_path.is_file()
        assert "<table" in html_path.read_text(encoding="utf-8")
        marker = staged_artifacts / ".nas-supernet_charts.jsonl"
        rec = [r for r in marker.read_text().splitlines() if r.strip()][-1]
        import json
        assert json.loads(rec)["status"] == "rendered_static"

    def test_build_supernet_fallback(self, tmp_path: Path) -> None:
        """supernet.py exposing build_supernet() (no SearchSpace class) still works."""
        (tmp_path / "supernet.py").write_text(
            """
def build_supernet():
    from dataclasses import dataclass, field

    @dataclass
    class SS:
        depth_candidates: tuple = (4, 6)
        layer_configs: dict = field(default_factory=lambda: {"blk": {"k": (3, 5)}})
    return SS()
""",
            encoding="utf-8",
        )
        rc, calls = self._run(tmp_path, types.SimpleNamespace(render_chart=lambda **kw: None))
        assert rc == 0
        assert len(calls) == 1
        assert calls[0]["data"][0]["block"] == "blk"
