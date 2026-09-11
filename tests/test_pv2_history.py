"""test_pv2_history.py — the v2 history layer: facet phrases + the derived
base/history.md view (render_history.py, SPEC §2).

Covers the §7 history matrix: the render block for every outcome, the
in-flight form (pending_launch included, with an import-reuse assertion that
proves the predicate is frontier_snapshot's, not a copy), the zero-proposal
round pointer, signed latency percentages, the facet phrase join, the
80-code-point cap (builder + CLI), the atomic write, the render_stamp (the
zero-row round-1 state included), and the fail-loud matrix (torn history,
missing anchor, impl rows without facet phrases, an unparseable
train_status.json of an in-flight vid, rows outside the rounds/ range).

The v2 modules are loaded under unique names (importlib by file path) so
this suite never pollutes — nor gets polluted by — the prof-opt suites that
import the same module names from workflows/prof-opt.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_V2 = _REPO / "workflows" / "profiling-v2" / "agents" / "_po_scripts"

_SHARED_NAMES = ("history_lib", "round_state", "frontier_snapshot")


def _load_v2(name: str):
    """Load a v2 _po_scripts module under a pv2-unique name, with the shared
    sibling imports resolved from the v2 dir during exec only — the global
    sys.modules/sys.path state is restored afterwards, so the prof-opt
    suites in the same pytest session stay untouched (and vice versa)."""
    spec = importlib.util.spec_from_file_location(
        f"pv2_{name}", _V2 / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    saved_path = sys.path[:]
    saved_mods = {m: sys.modules.pop(m) for m in _SHARED_NAMES
                  if m in sys.modules}
    sys.path.insert(0, str(_V2))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved_path
        for m in _SHARED_NAMES:
            sys.modules.pop(m, None)
        sys.modules.update(saved_mods)
    return mod


history_lib = _load_v2("history_lib")
render_history = _load_v2("render_history")

_BASELINE = 1000
_PHRASES = {"structure_change": "首层收窄 64→32 并同步输入层",
            "feature_change": "去掉与业务无关的冗余特征列",
            "loss_change": "未改"}


def _ws(tmp_path: Path, *, rounds: int = 1, baseline: int = _BASELINE) -> Path:
    art = tmp_path / "art"
    (art / "base").mkdir(parents=True)
    (art / "base" / "origin_anchor.json").write_text(json.dumps({
        "baseline_makespan_cycles": baseline, "latency_reduction_min": 0.5,
        "accuracy_budget": 0.1, "target_cycles": 500, "frozen_at_round": 0}),
        encoding="utf-8")
    for r in range(1, rounds + 1):
        (art / "rounds" / f"{r:03d}").mkdir(parents=True)
    return art


def _impl(art: Path, vid: str = "r1-01", *, rnd: int = 1, seq: int = 1,
          phrases: dict | None = None) -> None:
    history_lib.append_implemented(
        art / "history.jsonl", vid, round=rnd, seq=seq,
        change_sig=f"sig:{vid}", probe_epochs=1, target_modules=["m"],
        absorbs=[], predicted_delta_cycles=-100,
        **(phrases or _PHRASES))


def _render(art: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_V2 / "render_history.py"),
         "--artifacts", str(art)],
        capture_output=True, text=True, timeout=60)


def _render_text(art: Path) -> str:
    proc = _render(art)
    assert proc.returncode == 0, proc.stderr
    return (art / "base" / "history.md").read_text(encoding="utf-8")


# ── outcome blocks (every terminal outcome renders its own block) ────────────

@pytest.mark.parametrize("outcome", ["success", "accuracy_fail",
                                     "latency_fail", "probe_insufficient"])
def test_render_block_per_terminal_outcome(tmp_path: Path, outcome: str):
    art = _ws(tmp_path)
    hist = art / "history.jsonl"
    _impl(art)
    history_lib.append_latency(hist, "r1-01", structural_check="pass",
                               makespan_cycles=800, latency_gate="pass",
                               pred_actual_ratio=None,
                               outcome="latency_improved")
    extras = {"success": dict(outcome="success", gap=0.02, final_acc=0.912,
                              stopped_at_epoch=3),
              "accuracy_fail": dict(outcome="accuracy_fail", gap=0.4,
                                    stopped_at_epoch=2, over_budget_streak=2),
              "latency_fail": dict(outcome="latency_fail",
                                   measured_makespan_cycles=1500, gap=0.05),
              "probe_insufficient": dict(outcome="probe_insufficient",
                                         stage="train",
                                         max_retries_hit=False)}[outcome]
    history_lib.append_terminal(hist, "r1-01", **extras)
    text = _render_text(art)
    head = f"### r1 · r1-01 · {outcome}"
    assert head in text, text
    # the facet line rides every block, joined with the pinned separators
    assert (f"结构: {_PHRASES['structure_change']}；"
            f"特征: {_PHRASES['feature_change']}；"
            f"loss: {_PHRASES['loss_change']}") in text
    if outcome == "success":
        assert "acc 0.912" in text and "gap 0.02" in text


def test_render_success_signed_latency_both_directions(tmp_path: Path):
    for makespan, expected in ((1200, "+20.0%"), (800, "-20.0%")):
        art = _ws(tmp_path / f"ms{makespan}")
        hist = art / "history.jsonl"
        _impl(art)
        history_lib.append_latency(hist, "r1-01", structural_check="pass",
                                   makespan_cycles=makespan,
                                   latency_gate="pass", pred_actual_ratio=None,
                                   outcome="latency_improved")
        history_lib.append_terminal(hist, "r1-01", outcome="success",
                                    gap=0.01, stopped_at_epoch=3,
                                    final_acc=0.9)
        assert f"时延 {expected}" in _render_text(art)


def test_render_latency_fail_uses_measured_makespan(tmp_path: Path):
    """latency_fail rows never carry makespan_cycles — the signed percentage
    must fall back to measured_makespan_cycles (SPEC §2.2)."""
    art = _ws(tmp_path)
    _impl(art)
    history_lib.append_terminal(art / "history.jsonl", "r1-01",
                                outcome="latency_fail",
                                measured_makespan_cycles=1250)
    assert "时延 +25.0%" in _render_text(art)


def test_render_no_makespan_renders_dash(tmp_path: Path):
    art = _ws(tmp_path)
    _impl(art)
    history_lib.append_terminal(art / "history.jsonl", "r1-01",
                                outcome="probe_insufficient", stage="train",
                                max_retries_hit=False)
    text = _render_text(art)
    assert "时延 -" in text and "acc -（gap -）" in text


# ── in-flight (predicate imported from frontier_snapshot) ────────────────────

def _inflight_ws(tmp_path: Path) -> Path:
    art = _ws(tmp_path)
    hist = art / "history.jsonl"
    _impl(art)
    history_lib.append_latency(hist, "r1-01", structural_check="pass",
                               makespan_cycles=900, latency_gate="pass",
                               pred_actual_ratio=None,
                               outcome="latency_improved")
    return art


def test_render_in_flight_pending_launch(tmp_path: Path):
    """No train_status.json yet — the legal window between the latency row
    and the probe launch renders pending_launch."""
    art = _inflight_ws(tmp_path)
    text = _render_text(art)
    assert ("· in_flight · stage - · epoch - · gap -（pending_launch）"
            in text), text


@pytest.mark.parametrize("status_doc,risk", [
    ({"stage": "training", "epoch": 3, "gap": 0.12,
      "over_budget_streak": 0}, "on_track"),
    ({"stage": "training", "epoch": 5, "gap": 0.3,
      "over_budget_streak": 2}, "at_risk"),
    ({"stage": "killed", "epoch": 5, "gap": 0.3,
      "over_budget_streak": 3}, "terminating"),
])
def test_render_in_flight_risk_grading(tmp_path: Path, status_doc, risk):
    art = _inflight_ws(tmp_path)
    (art / "variants" / "r1-01").mkdir(parents=True)
    (art / "variants" / "r1-01" / "train_status.json").write_text(
        json.dumps({"vid": "r1-01", **status_doc}), encoding="utf-8")
    text = _render_text(art)
    assert (f"· in_flight · stage {status_doc['stage']}"
            f" · epoch {status_doc['epoch']}"
            f" · gap {status_doc['gap']}（{risk}）") in text, text


def test_render_in_flight_predicate_is_imported_not_copied(
        tmp_path: Path, monkeypatch):
    """SPEC §2.2 单一实现防漂移：render_history must CALL
    frontier_snapshot._in_flight_row — monkeypatching the frontier function
    must change the rendered block (a copied predicate would not move)."""
    art = _inflight_ws(tmp_path)

    def fake_row(art_arg, vid):
        assert vid == "r1-01"
        return {"vid": vid, "stage": "ZZZ_SENTINEL", "epoch": 9, "metric": None,
                "gap": None, "over_budget_streak": None, "risk": "FAKE_RISK"}

    monkeypatch.setattr(render_history.frontier_snapshot, "_in_flight_row",
                        fake_row)
    text = render_history.render(art)
    assert "ZZZ_SENTINEL" in text and "（FAKE_RISK）" in text, text


def test_render_in_flight_unparseable_train_status_fails_loud(tmp_path: Path):
    art = _inflight_ws(tmp_path)
    vdir = art / "variants" / "r1-01"
    vdir.mkdir(parents=True)
    (vdir / "train_status.json").write_text("{torn", encoding="utf-8")
    proc = _render(art)
    assert proc.returncode == 2
    assert "unparseable" in proc.stderr


# ── round layout ──────────────────────────────────────────────────────────────

def test_render_zero_proposal_round_pointer(tmp_path: Path):
    art = _ws(tmp_path, rounds=2)
    _impl(art, "r1-01", rnd=1)
    text = _render_text(art)
    assert "### r2 · 无提案 · 详见 rounds/002/analysis.md" in text, text
    assert "### r1 · r1-01" in text


def test_render_round1_zero_row_history_is_legal(tmp_path: Path):
    """round 1 with NO history rows at all: the pointer block renders and
    the render_stamp is the zero-row state {"lines": 0, "last_line_no": 0}."""
    art = _ws(tmp_path)
    text = _render_text(art)
    assert "### r1 · 无提案 · 详见 rounds/001/analysis.md" in text
    # json.dumps(sort_keys=True): last_line_no sorts before lines
    assert ('render_stamp: {"last_line_no": 0, "lines": 0}' in text), text


def test_render_row_outside_rounds_range_fails_loud(tmp_path: Path):
    art = _ws(tmp_path)                    # rounds/001 only
    _impl(art, "r2-01", rnd=2)             # a row for a round with no dir
    proc = _render(art)
    assert proc.returncode == 2
    assert "torn workspace" in proc.stderr


def test_render_multi_vid_round_orders_by_seq(tmp_path: Path):
    art = _ws(tmp_path)
    _impl(art, "r1-02", seq=2)
    _impl(art, "r1-01", seq=1)
    text = _render_text(art)
    assert text.index("### r1 · r1-01") < text.index("### r1 · r1-02")


# ── render_stamp + atomic write + CLI surface ─────────────────────────────────

def test_render_stamp_tracks_jsonl_lines(tmp_path: Path):
    art = _ws(tmp_path)
    _impl(art)
    _impl(art, "r1-02", seq=2)
    lines = (art / "history.jsonl").read_text(encoding="utf-8").splitlines()
    text = _render_text(art)
    assert (f'render_stamp: {{"last_line_no": {len(lines)}, '
            f'"lines": {len(lines)}}}') in text
    # the parse side of the same single implementation round-trips
    stamp = render_history.parse_render_stamp(art / "base" / "history.md")
    assert stamp == render_history.history_stamp(art / "history.jsonl")


def test_parse_render_stamp_tolerances(tmp_path: Path):
    art = _ws(tmp_path)
    md = art / "base" / "history.md"
    assert render_history.parse_render_stamp(md) is None      # missing file
    (art / "base").mkdir(parents=True, exist_ok=True)
    md.write_text("### r1 · 无提案 · 详见 rounds/001/analysis.md\n"
                  "render_stamp: {torn\n", encoding="utf-8")
    assert render_history.parse_render_stamp(md) is None      # unparseable
    md.write_text("no stamp at all\n", encoding="utf-8")
    assert render_history.parse_render_stamp(md) is None


def test_render_is_atomic_and_stdout_matches_file(tmp_path: Path):
    art = _ws(tmp_path)
    _impl(art)
    proc = _render(art)
    assert proc.returncode == 0, proc.stderr
    body = (art / "base" / "history.md").read_text(encoding="utf-8")
    assert proc.stdout == body               # stdout prints the same content
    leftovers = [p.name for p in (art / "base").iterdir()
                 if ".tmp." in p.name]
    assert leftovers == []                   # tmp swapped in, never left
    # idempotent: re-rendering the same disk state is byte-stable
    assert _render(art).stdout == body


def test_render_header_pins_the_derived_view_contract(tmp_path: Path):
    art = _ws(tmp_path)
    text = _render_text(art)
    assert "机械派生视图" in text and "history.jsonl" in text
    assert "render_history.py" in text


# ── fail-loud matrix ──────────────────────────────────────────────────────────

def test_render_fails_loud_on_torn_history(tmp_path: Path):
    art = _ws(tmp_path)
    with open(art / "history.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"vid": "r1-01"\n')        # torn JSON line
    proc = _render(art)
    assert proc.returncode == 2
    assert "not valid JSON" in proc.stderr


def test_render_fails_loud_on_missing_anchor(tmp_path: Path):
    art = tmp_path / "art"
    (art / "rounds" / "001").mkdir(parents=True)
    (art / "history.jsonl").write_text("", encoding="utf-8")
    proc = _render(art)
    assert proc.returncode == 2
    assert "origin_anchor" in proc.stderr


def test_render_fails_loud_on_impl_row_without_facet_phrases(tmp_path: Path):
    """§2.1 洁净性：a v2 workspace has no old-schema rows — an impl row
    without the facet phrases is a torn/legacy workspace and fails loud."""
    art = _ws(tmp_path)
    row = {"vid": "r1-01", "round": 1, "seq": 1, "change_sig": "sig",
           "probe_epochs": 1, "target_modules": ["m"], "absorbs": [],
           "implemented": True, "version": 1,
           "ts": "2026-09-11T00:00:00+00:00"}
    with open(art / "history.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    proc = _render(art)
    assert proc.returncode == 2
    assert "facet phrase" in proc.stderr


# ── the facet phrase cap (§2.1) ────────────────────────────────────────────────

def _append_cli(art: Path, phrases: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_V2 / "append_impl_row.py"),
         "--history", str(art / "history.jsonl"), "--vid", "r1-01",
         "--round", "1", "--seq", "1", "--change-sig", "sig:r1-01",
         "--probe-epochs", "1", "--target-modules", '["m"]',
         "--absorbs", "[]",
         "--structure-change", phrases["structure_change"],
         "--feature-change", phrases["feature_change"],
         "--loss-change", phrases["loss_change"]],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "ORCA_ARTIFACTS_DIR": str(art)})


def test_facet_phrase_cap_eighty_code_points(tmp_path: Path):
    art = _ws(tmp_path)
    exactly = "改" * 80                   # 80 code points: legal
    over = "改" * 81                     # 81: exit 2, no row written
    phrases = {**_PHRASES, "structure_change": exactly}
    proc = _append_cli(art, phrases)
    assert proc.returncode == 0, proc.stderr
    row = json.loads((art / "history.jsonl").read_text(encoding="utf-8")
                     .splitlines()[0])
    assert row["structure_change"] == exactly
    assert row["feature_change"] == _PHRASES["feature_change"]

    hist = art / "history.jsonl"
    hist.unlink()
    proc = _append_cli(art, {**_PHRASES, "structure_change": over})
    assert proc.returncode == 2
    assert "80" in proc.stderr and "code-point" in proc.stderr
    assert not hist.exists()              # nothing was appended

    # the builder itself is the cap's single source (CLI exit 2 rides it)
    with pytest.raises(history_lib.HistoryError):
        history_lib.append_implemented(
            hist, "r1-01", round=1, seq=1, change_sig="s", probe_epochs=1,
            target_modules=["m"], absorbs=[],
            feature_change="x" * 81)
    with pytest.raises(history_lib.HistoryError):
        history_lib.append_implemented(
            hist, "r1-01", round=1, seq=1, change_sig="s", probe_epochs=1,
            target_modules=["m"], absorbs=[], loss_change="")


def test_facet_phrase_defaults_are_unchanged(tmp_path: Path):
    art = _ws(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(_V2 / "append_impl_row.py"),
         "--history", str(art / "history.jsonl"), "--vid", "r1-01",
         "--round", "1", "--seq", "1", "--change-sig", "sig:r1-01",
         "--probe-epochs", "1", "--target-modules", '["m"]',
         "--absorbs", "[]"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "ORCA_ARTIFACTS_DIR": str(art)})
    assert proc.returncode == 0, proc.stderr
    row = json.loads((art / "history.jsonl").read_text(encoding="utf-8")
                     .splitlines()[0])
    assert row["structure_change"] == "未改"
    assert row["feature_change"] == "未改"
    assert row["loss_change"] == "未改"
