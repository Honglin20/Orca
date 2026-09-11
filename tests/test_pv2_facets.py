"""test_pv2_facets.py — the v2 facet layer (SPEC §1/§3/§7).

Covers: the emit gate's R1-R5 matrix (positive and negative), the joint
facet-phrase truth table (n/a pinned by availability, 未改 by the
declaration, both directions), the structure-only positive path with both
facets unavailable (the rules hang idle), distillation_free_ack missing/
false rejection, the conditional feature.md candidate sentinel, the
history.md freshness check (stale tolerated once via the built-in rerender,
still-stale fail loud, digest seal retired), facet_check duties ①/②
(output welded / inputs meet, structure-only walks ① only), the recheck's
facets diff==facet_edited_files equivalence gate, check_contracts' facets
fingerprint (reuse face) and per-face <<facet_dir>> token contract, the
lock/enumeration zero-change regression (facets/ present + second reuse
passes; list_shadow_pkgs never sees facets/), the docs-manifest digest-row
replacement + capability disclosure + conditional feature.md row, the
per-round facets archive, render_run's optional facet_dir token, and the
gate node's stdout purity with render_history mounted.

The v2 modules are loaded under unique names (importlib by file path) so
this suite never pollutes — nor gets polluted by — the prof-opt suites that
import the same module names from workflows/prof-opt.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_V2 = _REPO / "workflows" / "profiling-v2" / "agents" / "_po_scripts"
_CONTRACTS_SH = (_REPO / "workflows" / "profiling-v2" / "agents"
                 / "po_contract" / "scripts" / "check_contracts.sh")
_RECHECK_SH = (_REPO / "workflows" / "profiling-v2" / "agents" / "po_propose"
               / "scripts" / "run_latency_recheck.sh")

_SHARED_NAMES = ("history_lib", "round_state", "frontier_snapshot",
                 "push_curves")


def _load_v2(name: str):
    """See test_pv2_history._load_v2 — unique module names + scoped sys.path
    so the prof-opt suites in the same pytest session stay untouched."""
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
push_curves = _load_v2("push_curves")

VAS_SENTINEL = "[subagent:variant-assessor v1 VAS4K9]"
FEATURE_SENTINEL = "[subagent:feature-architect v1 FAA8R2]"
CANDIDATE_SENTINELS = {
    "semantic.md": "[subagent:semantic-architecture-proposer v1 SAP1A1]",
    "hardware.md": "[subagent:hardware-architecture-proposer v1 HAP1B1]",
    "sota.md": "[subagent:sota-architecture-proposer v1 SOTA1C1]",
}
SELECTOR_SENTINEL = "[subagent:architecture-selector v1 ASC1D1]"
ASSESSMENT_HEADINGS = ("## 任务语义", "## 输入输出", "## 架构动机",
                       "## 逐模块职责与物理意义", "## 训练目标与指标方向",
                       "## 与基线差异")
ASSESSMENT_SUB = "### 被牺牲信息与预期精度代价"
FEATURE_FILE = "feat/pipeline.py"
LOSS_FILE = "loss/def.py"


def _facets_block(*, features: bool, loss: bool,
                  export_consumes: bool = False) -> dict:
    return {"features": features, "loss": loss,
            "export_consumes_features": export_consumes,
            "facets_dir": "facets",
            "feature_files": [FEATURE_FILE], "loss_files": [LOSS_FILE],
            "origin_hashes": {}, "evidence": "contract_work/facet_dryrun.json"}


# ── the emit-gate workspace (v2 schema: facet contract on every path) ─────────

def _assessment_doc() -> str:
    lines = [VAS_SENTINEL]
    for heading in ASSESSMENT_HEADINGS:
        lines.append(heading)
        lines.append(f"content for {heading}")
        if heading == "## 与基线差异":
            lines.append(ASSESSMENT_SUB)
            lines.append("sacrificed information and expected cost")
    return "\n".join(lines) + "\n"


def _default_proposal(*, declared: dict, phrases: dict,
                      facet_edited: list[str]) -> dict:
    return {"vid": "r1-01", "lever": "activation", "change_sig": "sig:r1-01",
            "target_modules": ["m"], "target_pattern_id": "low-mfu-matmul",
            "rationale": "why", "change_spec": "edit", "absorbs": [],
            "predicted_delta_cycles": -600, "prediction_basis": "predictor",
            "edited_files": ["pkg/model.py"], "predicted_acc_impact": "low",
            "accuracy_evidence": "rule-0001", "sota_reference": "ref",
            "facets": declared,
            "structure_change": phrases["structure_change"],
            "feature_change": phrases["feature_change"],
            "loss_change": phrases["loss_change"],
            "facet_edited_files": facet_edited,
            "distillation_free_ack": True}


def _emit_ws(tmp_path: Path, *, facets: dict,
             proposal: dict | None = None,
             decl_overrides: dict | None = None,
             with_feature_candidate: bool | None = None,
             render_history_md: bool = True,
             ending: str = "latency_improved") -> Path:
    """A green single-variant v2 round. ``facets`` is the contracts.json
    block; the proposal defaults to the LEGAL declaration for that block
    (structure-only when both facets are off). ``ending`` selects the
    round's legal emit-time outcome (the phrase equality must hold on
    every one of them)."""
    art = tmp_path / "ws"
    (art / "scripts").mkdir(parents=True)
    for src in ("history_lib.py", "round_state.py", "frontier_snapshot.py",
                "render_history.py"):
        shutil.copy(_V2 / src, art / "scripts" / src)
    (art / "base" / "profile" / "model").mkdir(parents=True)
    (art / "base" / "profile" / "model" / "schedule_result.json").write_text(
        json.dumps({"parallel_cycles": 1000}), encoding="utf-8")
    (art / "base" / "origin_anchor.json").write_text(json.dumps({
        "baseline_makespan_cycles": 1000, "latency_reduction_min": 0.5,
        "accuracy_budget": 0.1, "target_cycles": 500, "frozen_at_round": 0}),
        encoding="utf-8")
    (art / "contracts.json").write_text(
        json.dumps({"facets": facets}), encoding="utf-8")

    features = facets.get("features") is True
    if proposal is None:
        if features:
            proposal = _default_proposal(
                declared={"structure": True, "features": True, "loss": False},
                phrases={"structure_change": "首层收窄并前移昂贵变换",
                         "feature_change": "剔除与业务无关的冗余特征列",
                         "loss_change": "n/a (facet unavailable)"},
                facet_edited=[FEATURE_FILE])
        else:
            proposal = _default_proposal(
                declared={"structure": True, "features": False, "loss": False},
                phrases={"structure_change": "首层收窄并前移昂贵变换",
                         "feature_change": "n/a (facet unavailable)",
                         "loss_change": "n/a (facet unavailable)"},
                facet_edited=[])
    if with_feature_candidate is None:
        with_feature_candidate = features

    rd = art / "rounds" / "001"
    (rd / "candidates").mkdir(parents=True)
    sentinels = dict(CANDIDATE_SENTINELS)
    if with_feature_candidate:
        sentinels["feature.md"] = FEATURE_SENTINEL
    for name, sentinel in sentinels.items():
        (rd / "candidates" / name).write_text(
            sentinel + "\ncontent\n", encoding="utf-8")
    (rd / "architecture_decision.md").write_text(
        f"{SELECTOR_SENTINEL}\ncontent\n## absorbs\nnone\n"
        "## avoids\nnone\n", encoding="utf-8")
    (rd / "proposals.json").write_text(json.dumps({
        "round": 1, "filtered_count": 0, "exhausted_rationale": [],
        "proposals": [proposal]}), encoding="utf-8")
    (rd / "verdicts.jsonl").write_text(json.dumps(
        {"vid": "r1-01", "round": 1, "outcome": ending}) + "\n",
        encoding="utf-8")
    (rd / "analysis.md").write_text(
        "## latency\nreached the line; disclosed\n", encoding="utf-8")

    (art / "shadow" / "pkg").mkdir(parents=True)
    (art / "shadow" / "pkg" / "model.py").write_text("model", encoding="utf-8")
    vd = art / "variants" / "r1-01"
    vd.mkdir(parents=True)
    declaration = {"vid": "r1-01", "change_sig": "sig:r1-01",
                   "predicted_delta_cycles": -600,
                   "facets": proposal["facets"],
                   "structure_change": proposal["structure_change"],
                   "feature_change": proposal["feature_change"],
                   "loss_change": proposal["loss_change"],
                   "facet_edited_files": proposal.get(
                       "facet_edited_files", [])}
    declaration.update(decl_overrides or {})
    (vd / "declaration.json").write_text(json.dumps(declaration),
                                         encoding="utf-8")
    (vd / "assessment.md").write_text(_assessment_doc(), encoding="utf-8")
    (vd / ".analysis_stamp.json").write_text(json.dumps(
        {"key": f"r1-01|sig:r1-01|"
                f"{hashlib.sha256((vd / 'declaration.json').read_bytes()).hexdigest()}"}),
        encoding="utf-8")

    hist = art / "history.jsonl"
    history_lib.append_implemented(
        hist, "r1-01", round=1, seq=1, change_sig="sig:r1-01",
        probe_epochs=1, target_modules=["m"], absorbs=[],
        predicted_delta_cycles=-600,
        structure_change=proposal["structure_change"],
        feature_change=proposal["feature_change"],
        loss_change=proposal["loss_change"])
    if ending == "latency_improved":
        history_lib.append_latency(hist, "r1-01", structural_check="pass",
                                   makespan_cycles=400, latency_gate="pass",
                                   pred_actual_ratio=None,
                                   outcome="latency_improved")
    elif ending == "latency_fail":
        history_lib.append_latency(hist, "r1-01", structural_check="pass",
                                   makespan_cycles=1500, latency_gate="fail",
                                   pred_actual_ratio=None,
                                   outcome="latency_fail")
    elif ending in ("structural_mismatch", "variant_broken"):
        history_lib.append_outcome(hist, "r1-01", ending)
    else:
        raise ValueError(f"unsupported fixture ending {ending!r}")
    if render_history_md:
        proc = subprocess.run(
            [sys.executable, str(_V2 / "render_history.py"),
             "--artifacts", str(art)],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
    return art


def _check_emit(art: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_V2 / "check_propose_emit.py"),
         "--artifacts", str(art)],
        capture_output=True, text=True, timeout=120)


# ── green paths ───────────────────────────────────────────────────────────────

def test_emit_gate_green_structure_only(tmp_path: Path):
    """Both facets unavailable: R2-R5 hang idle, the feature candidate is
    absent, the n/a phrases are the legal values — the round emits."""
    art = _emit_ws(tmp_path, facets=_facets_block(features=False, loss=False))
    proc = _check_emit(art)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ok"] is True


def test_emit_gate_green_with_features_facet(tmp_path: Path):
    art = _emit_ws(tmp_path, facets=_facets_block(features=True, loss=False))
    proc = _check_emit(art)
    assert proc.returncode == 0, proc.stderr
    # the digest layer is retired: no digest file was ever created and none
    # is demanded
    assert not (art / "base" / "history_digest.md").exists()
    assert not (art / "base" / "history_digest.stamp.json").exists()


def test_emit_gate_green_with_loss_facet(tmp_path: Path):
    """The loss-side full green path: loss capability true, declared true,
    a real summary phrase, the loss facet file edited (R2/R3/R5 positives
    for the loss facet specifically — features stays off)."""
    proposal = _default_proposal(
        declared={"structure": True, "features": False, "loss": True},
        phrases={"structure_change": "首层收窄",
                 "feature_change": "n/a (facet unavailable)",
                 "loss_change": "加入与业务挂钩的去相关正则项"},
        facet_edited=[LOSS_FILE])
    art = _emit_ws(tmp_path, facets=_facets_block(features=False, loss=True),
                   proposal=proposal)
    proc = _check_emit(art)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("ending", ["latency_fail", "structural_mismatch",
                                    "variant_broken"])
def test_emit_gate_green_on_every_legal_ending_path(tmp_path: Path,
                                                    ending: str):
    """The row-proposal phrase equality + the facet contract hold on ALL
    emit-time legal endings — an eliminated round carries its phrases the
    same way an admitted one does (merged-snapshot rows)."""
    art = _emit_ws(tmp_path, facets=_facets_block(features=False, loss=False),
                   ending=ending)
    proc = _check_emit(art)
    assert proc.returncode == 0, (ending, proc.stderr)


# ── R1-R5 (the deterministic emit-gate rules, negative matrix) ────────────────

def test_r1_structure_false_is_illegal(tmp_path: Path):
    facets = _facets_block(features=True, loss=False)
    base = _default_proposal(
        declared={"structure": False, "features": True, "loss": False},
        phrases={"structure_change": "x", "feature_change": "y",
                 "loss_change": "n/a (facet unavailable)"},
        facet_edited=[FEATURE_FILE])
    art = _emit_ws(tmp_path, facets=facets, proposal=base,
                   decl_overrides={"facets": base["facets"]})
    proc = _check_emit(art)
    assert proc.returncode == 1
    assert "R1 FAIL" in proc.stderr and "facets.structure must be true" \
        in proc.stderr


@pytest.mark.parametrize("facet", ["features", "loss"])
def test_r2_claiming_an_absent_capability(tmp_path: Path, facet: str):
    facets = _facets_block(features=False, loss=False)
    declared = {"structure": True, "features": False, "loss": False}
    declared[facet] = True
    phrases = {"structure_change": "s", "feature_change": "n/a (facet unavailable)",
               "loss_change": "n/a (facet unavailable)"}
    edited = [FEATURE_FILE] if facet == "features" else [LOSS_FILE]
    proposal = _default_proposal(declared=declared, phrases=phrases,
                                 facet_edited=edited)
    art = _emit_ws(tmp_path, facets=facets, proposal=proposal,
                   decl_overrides={"facets": declared,
                                   "facet_edited_files": edited})
    proc = _check_emit(art)
    assert proc.returncode == 1
    assert f"R2 FAIL: facets.{facet} declared true" in proc.stderr


def test_r3_declared_true_but_no_facet_file_edited(tmp_path: Path):
    facets = _facets_block(features=True, loss=False)
    proposal = _default_proposal(
        declared={"structure": True, "features": True, "loss": False},
        phrases={"structure_change": "s", "feature_change": "f",
                 "loss_change": "n/a (facet unavailable)"},
        facet_edited=[])
    art = _emit_ws(tmp_path, facets=facets, proposal=proposal,
                   decl_overrides={"facet_edited_files": []})
    proc = _check_emit(art)
    assert proc.returncode == 1
    assert "R3 FAIL: facets.features declared true" in proc.stderr


def test_r3_declared_false_but_facet_file_edited(tmp_path: Path):
    facets = _facets_block(features=True, loss=False)
    proposal = _default_proposal(
        declared={"structure": True, "features": False, "loss": False},
        phrases={"structure_change": "s",
                 "feature_change": "n/a (facet unavailable)",
                 "loss_change": "n/a (facet unavailable)"},
        facet_edited=[])
    art = _emit_ws(tmp_path, facets=facets, proposal=proposal,
                   decl_overrides={"facet_edited_files": [FEATURE_FILE]})
    proc = _check_emit(art)
    assert proc.returncode == 1
    assert "R3 FAIL: facets.features declared false" in proc.stderr


def test_r4_na_pinned_by_availability_both_directions(tmp_path: Path):
    facets = _facets_block(features=True, loss=False)
    # available + n/a -> illegal
    proposal = _default_proposal(
        declared={"structure": True, "features": True, "loss": False},
        phrases={"structure_change": "s",
                 "feature_change": "n/a (facet unavailable)",
                 "loss_change": "n/a (facet unavailable)"},
        facet_edited=[FEATURE_FILE])
    art = _emit_ws(tmp_path / "a", facets=facets, proposal=proposal)
    proc = _check_emit(art)
    assert proc.returncode == 1
    assert "R4 FAIL: facet features is available" in proc.stderr

    # unavailable + a real summary (not n/a) -> illegal
    proposal2 = _default_proposal(
        declared={"structure": True, "features": True, "loss": False},
        phrases={"structure_change": "s", "feature_change": "f",
                 "loss_change": "加了去相关正则项"},
        facet_edited=[FEATURE_FILE])
    art2 = _emit_ws(tmp_path / "b", facets=facets, proposal=proposal2,
                    decl_overrides={"loss_change": "加了去相关正则项"})
    proc2 = _check_emit(art2)
    assert proc2.returncode == 1
    assert "R4 FAIL: facet loss is unavailable" in proc2.stderr


def test_r5_unchanged_pinned_by_declaration_both_directions(tmp_path: Path):
    facets = _facets_block(features=True, loss=False)
    # declared true + 未改 -> illegal
    proposal = _default_proposal(
        declared={"structure": True, "features": True, "loss": False},
        phrases={"structure_change": "s", "feature_change": "未改",
                 "loss_change": "n/a (facet unavailable)"},
        facet_edited=[FEATURE_FILE])
    art = _emit_ws(tmp_path / "a", facets=facets, proposal=proposal,
                   decl_overrides={"feature_change": "未改"})
    proc = _check_emit(art)
    assert proc.returncode == 1
    assert "R5 FAIL: facets.features declared true" in proc.stderr

    # declared false (capability available) + a summary -> illegal
    proposal2 = _default_proposal(
        declared={"structure": True, "features": False, "loss": False},
        phrases={"structure_change": "s", "feature_change": "改了特征列",
                 "loss_change": "n/a (facet unavailable)"},
        facet_edited=[])
    art2 = _emit_ws(tmp_path / "b", facets=facets, proposal=proposal2,
                    decl_overrides={"feature_change": "改了特征列"})
    proc2 = _check_emit(art2)
    assert proc2.returncode == 1
    assert "R5 FAIL: facets.features declared false" in proc2.stderr


def test_phrase_cap_and_presence(tmp_path: Path):
    facets = _facets_block(features=True, loss=False)
    legal = _default_proposal(
        declared={"structure": True, "features": True, "loss": False},
        phrases={"structure_change": "s", "feature_change": "f",
                 "loss_change": "n/a (facet unavailable)"},
        facet_edited=[FEATURE_FILE])
    art = _emit_ws(tmp_path, facets=facets, proposal=legal)
    # make the PROPOSAL illegal after the fixture (the typed builder refuses
    # to write an over-cap row — the row/proposal equality co-fires, the cap
    # problem is what this test pins)
    props_path = art / "rounds" / "001" / "proposals.json"
    props = json.loads(props_path.read_text(encoding="utf-8"))
    props["proposals"][0]["structure_change"] = "改" * 81
    props_path.write_text(json.dumps(props, ensure_ascii=False),
                          encoding="utf-8")
    proc = _check_emit(art)
    assert proc.returncode == 1
    assert "code-point cap" in proc.stderr

    art2 = _emit_ws(tmp_path / "b", facets=facets, proposal=legal)
    props2_path = art2 / "rounds" / "001" / "proposals.json"
    props2 = json.loads(props2_path.read_text(encoding="utf-8"))
    props2["proposals"][0]["feature_change"] = ""
    props2_path.write_text(json.dumps(props2, ensure_ascii=False),
                           encoding="utf-8")
    proc2 = _check_emit(art2)
    assert proc2.returncode == 1
    assert "must be a non-empty facet phrase" in proc2.stderr

    # a blank (whitespace-only) phrase carries no information either — the
    # same rejection, on both the gate and the typed builder
    art3 = _emit_ws(tmp_path / "c", facets=facets, proposal=legal)
    props3_path = art3 / "rounds" / "001" / "proposals.json"
    props3 = json.loads(props3_path.read_text(encoding="utf-8"))
    props3["proposals"][0]["structure_change"] = "   "
    props3_path.write_text(json.dumps(props3, ensure_ascii=False),
                           encoding="utf-8")
    proc3 = _check_emit(art3)
    assert proc3.returncode == 1
    assert "must be a non-empty facet phrase" in proc3.stderr
    with pytest.raises(history_lib.HistoryError):
        history_lib.append_implemented(
            art3 / "history.jsonl", "r9-01", round=9, seq=1, change_sig="s",
            probe_epochs=1, target_modules=["m"], absorbs=[],
            loss_change="   ")


def test_distillation_free_ack_missing_or_false(tmp_path: Path):
    facets = _facets_block(features=False, loss=False)
    no_ack = _default_proposal(
        declared={"structure": True, "features": False, "loss": False},
        phrases={"structure_change": "s",
                 "feature_change": "n/a (facet unavailable)",
                 "loss_change": "n/a (facet unavailable)"},
        facet_edited=[])
    no_ack.pop("distillation_free_ack")
    art = _emit_ws(tmp_path / "a", facets=facets, proposal=no_ack)
    proc = _check_emit(art)
    assert proc.returncode == 1
    assert "distillation_free_ack must be exactly true" in proc.stderr

    art2 = _emit_ws(tmp_path / "b", facets=facets,
                    proposal={**no_ack, "distillation_free_ack": False})
    proc2 = _check_emit(art2)
    assert proc2.returncode == 1
    assert "distillation_free_ack" in proc2.stderr


def test_row_proposal_phrase_equality_is_symmetric(tmp_path: Path):
    facets = _facets_block(features=True, loss=False)
    proposal = _default_proposal(
        declared={"structure": True, "features": True, "loss": False},
        phrases={"structure_change": "s", "feature_change": "f",
                 "loss_change": "n/a (facet unavailable)"},
        facet_edited=[FEATURE_FILE])
    art = _emit_ws(tmp_path, facets=facets, proposal=proposal)
    # hand-edit ONE side (the LATEST history row — read_latest is last-wins
    # over full-snapshot rows) — the equality check is a single comparison,
    # symmetric by construction, same pattern as absorbs
    hist = art / "history.jsonl"
    rows = [json.loads(l) for l in hist.read_text(encoding="utf-8")
            .splitlines() if l.strip()]
    rows[-1]["feature_change"] = "row side phrase"
    hist.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                            for r in rows), encoding="utf-8")
    # history.md is now stale too — the freshness rerender brings it back
    proc = _check_emit(art)
    assert proc.returncode == 1
    assert "history row feature_change" in proc.stderr
    assert "does not match the proposal's phrase" in proc.stderr


def test_declaration_verbatim_mismatch(tmp_path: Path):
    facets = _facets_block(features=True, loss=False)
    proposal = _default_proposal(
        declared={"structure": True, "features": True, "loss": False},
        phrases={"structure_change": "s", "feature_change": "f",
                 "loss_change": "n/a (facet unavailable)"},
        facet_edited=[FEATURE_FILE])
    art = _emit_ws(tmp_path, facets=facets, proposal=proposal,
                   decl_overrides={"facets": {"structure": True,
                                              "features": False,
                                              "loss": False}})
    proc = _check_emit(art)
    assert proc.returncode == 1
    assert "declaration.json facets" in proc.stderr \
        and "not verbatim" in proc.stderr


def test_contracts_without_facets_block_fails_loud(tmp_path: Path):
    art = _emit_ws(tmp_path, facets=_facets_block(features=False, loss=False))
    doc = json.loads((art / "contracts.json").read_text(encoding="utf-8"))
    doc.pop("facets")
    (art / "contracts.json").write_text(json.dumps(doc), encoding="utf-8")
    proc = _check_emit(art)
    assert proc.returncode == 1
    assert "no 'facets' block" in proc.stderr


# ── conditional feature.md candidate sentinel ─────────────────────────────────

def test_feature_md_sentinel_conditional(tmp_path: Path):
    facets = _facets_block(features=True, loss=False)
    art = _emit_ws(tmp_path, facets=facets, with_feature_candidate=False)
    proc = _check_emit(art)
    assert proc.returncode == 1
    assert "candidates/feature.md" in proc.stderr      # required, absent

    # wrong sentinel first line fails the same first-line contract
    art2 = _emit_ws(tmp_path / "b", facets=facets,
                    with_feature_candidate=True)
    (art2 / "rounds" / "001" / "candidates" / "feature.md").write_text(
        "[wrong sentinel]\n", encoding="utf-8")
    proc2 = _check_emit(art2)
    assert proc2.returncode == 1
    assert FEATURE_SENTINEL in proc2.stderr

    # features=false: the file is not required (an extra file is not an error)
    facets_off = _facets_block(features=False, loss=False)
    art3 = _emit_ws(tmp_path / "c", facets=facets_off,
                    with_feature_candidate=True)
    proc3 = _check_emit(art3)
    assert proc3.returncode == 0, proc3.stderr


# ── history.md freshness (replaces the retired digest seal) ───────────────────

def test_history_md_stale_is_tolerated_once_via_rerender(tmp_path: Path):
    art = _emit_ws(tmp_path, facets=_facets_block(features=False, loss=False))
    md = art / "base" / "history.md"
    md.write_text(md.read_text(encoding="utf-8").replace(
        '"lines": 2', '"lines": 99'), encoding="utf-8")
    proc = _check_emit(art)
    assert proc.returncode == 0, proc.stderr      # one rerender converged
    assert '"lines": 2' in md.read_text(encoding="utf-8")

    # a MISSING history.md is the same stale state — also tolerated once
    md.unlink()
    proc = _check_emit(art)
    assert proc.returncode == 0, proc.stderr
    assert md.is_file()


def test_history_md_rerender_failure_fails_loud(tmp_path: Path):
    art = _emit_ws(tmp_path, facets=_facets_block(features=False, loss=False))
    (art / "history.jsonl").write_text("{torn\n", encoding="utf-8")
    (art / "base" / "history.md").unlink()
    proc = _check_emit(art)
    assert proc.returncode == 1
    assert "rerender failed" in proc.stderr


def test_history_md_still_stale_after_rerender_fails_loud(tmp_path: Path):
    """The one-tolerance semantics: if the stamp STILL disagrees after the
    rerender the gate refuses. Simulated by poisoning the deployed render's
    stamp write (it renders, rc 0, but always stamps lines=999)."""
    art = _emit_ws(tmp_path, facets=_facets_block(features=False, loss=False))
    deployed = art / "scripts" / "render_history.py"
    src = deployed.read_text(encoding="utf-8")
    assert "json.dumps(stamp, ensure_ascii=False, sort_keys=True)" in src
    deployed.write_text(
        src.replace('json.dumps(stamp, ensure_ascii=False, sort_keys=True)',
                    'json.dumps({"lines": 999, "last_line_no": 999})'),
        encoding="utf-8")
    (art / "base" / "history.md").unlink()      # force the stale trigger
    proc = _check_emit(art)
    assert proc.returncode == 1
    assert "stays stale" in proc.stderr


# ── facet_check (duty ① unconditional / duty ② features-only) ────────────────

def _tiny_onnx(path: Path, in_shape, out_shape, in_name="x",
               out_name="output"):
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper
    inp = helper.make_tensor_value_info(in_name, TensorProto.FLOAT, in_shape)
    out = helper.make_tensor_value_info(out_name, TensorProto.FLOAT, out_shape)
    node = helper.make_node("Identity", [in_name], [out_name])
    graph = helper.make_graph([node], "g", [inp], [out])
    model = helper.make_model(
        graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    path.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(path))


def _facet_ws(tmp_path: Path, *, features: bool,
              variant_out_shape=(1, 2), variant_in_shape=(1, 8),
              builder_body: str | None = None) -> Path:
    art = tmp_path / "art"
    (art / "base").mkdir(parents=True)
    _tiny_onnx(art / "base" / "model.onnx", (1, 8), (1, 2))
    vd = art / "variants" / "r1-01"
    _tiny_onnx(vd / "onnx" / "model.onnx", variant_in_shape, variant_out_shape)
    eval_c: dict = {"metric_direction": "higher_better"}
    if features:
        (art / "variants" / "r1-01" / "facets" / "feat").mkdir(parents=True)
        (vd / "facets" / "feat" / "pipeline.py").write_text(
            builder_body or "def build(x):\n    return {'x': x}\n",
            encoding="utf-8")
        eval_c["sample_inputs"] = [
            {"name": "x", "shape": [1, 8], "dtype": "float32"}]
        eval_c["facet_builder"] = {"module": "feat.pipeline",
                                   "factory": "build"}
    (art / "contracts.json").write_text(json.dumps({
        "facets": _facets_block(features=features, loss=False),
        "eval": eval_c}), encoding="utf-8")
    return art


def _facet_check(art: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_V2 / "facet_check.py"),
         "--artifacts", str(art), "--vid", "r1-01"],
        capture_output=True, text=True, timeout=60)


def test_facet_check_duty1_passes_and_duty2_skipped_structure_only(
        tmp_path: Path):
    pytest.importorskip("onnx")
    art = _facet_ws(tmp_path, features=False)
    proc = _facet_check(art)
    assert proc.returncode == 0, proc.stderr
    assert "duty ② skipped" in proc.stderr


def test_facet_check_duty1_output_welded(tmp_path: Path):
    pytest.importorskip("onnx")
    art = _facet_ws(tmp_path, features=True, variant_out_shape=(1, 3))
    proc = _facet_check(art)
    assert proc.returncode == 2
    assert "duty ① FAIL" in proc.stderr and "welded" in proc.stderr


def test_facet_check_duty2_input_meets(tmp_path: Path):
    pytest.importorskip("onnx")
    art = _facet_ws(tmp_path, features=True, variant_in_shape=(1, 9),
                    builder_body=("import torch\n\n"
                                  "def build(x):\n"
                                  "    return {'x': torch.zeros(1, 9)}\n"))
    proc = _facet_check(art)
    assert proc.returncode == 0, proc.stderr


def test_facet_check_duty2_input_mismatch(tmp_path: Path):
    pytest.importorskip("onnx")
    art = _facet_ws(tmp_path, features=True, variant_in_shape=(1, 9),
                    builder_body=("import torch\n\n"
                                  "def build(x):\n"
                                  "    return {'x': torch.zeros(1, 8)}\n"))
    proc = _facet_check(art)
    assert proc.returncode == 2
    assert "duty ② FAIL" in proc.stderr and "dims" in proc.stderr


def test_facet_check_duty2_dtype_mismatch(tmp_path: Path):
    pytest.importorskip("onnx")
    art = _facet_ws(tmp_path, features=True,
                    builder_body=("import torch\n\n"
                                  "def build(x):\n"
                                  "    return {'x': torch.zeros(1, 8, dtype=torch.int64)}\n"))
    proc = _facet_check(art)
    assert proc.returncode == 2
    assert "duty ② FAIL" in proc.stderr and "dtype" in proc.stderr


def test_facet_check_duty2_builder_raises(tmp_path: Path):
    pytest.importorskip("onnx")
    art = _facet_ws(tmp_path, features=True,
                    builder_body="def build(x):\n    raise RuntimeError('boom')\n")
    proc = _facet_check(art)
    assert proc.returncode == 2
    assert "raised" in proc.stderr and "boom" in proc.stderr


def test_facet_check_fails_loud_without_facets_block(tmp_path: Path):
    pytest.importorskip("onnx")
    art = _facet_ws(tmp_path, features=False)
    doc = json.loads((art / "contracts.json").read_text(encoding="utf-8"))
    doc.pop("facets")
    (art / "contracts.json").write_text(json.dumps(doc), encoding="utf-8")
    proc = _facet_check(art)
    assert proc.returncode == 2
    assert "facets" in proc.stderr


def test_facet_check_duty2_requires_sample_spec(tmp_path: Path):
    pytest.importorskip("onnx")
    art = _facet_ws(tmp_path, features=True)
    doc = json.loads((art / "contracts.json").read_text(encoding="utf-8"))
    doc["eval"].pop("sample_inputs")
    (art / "contracts.json").write_text(json.dumps(doc), encoding="utf-8")
    proc = _facet_check(art)
    assert proc.returncode == 2
    assert "sample_inputs" in proc.stderr


def test_facet_check_duty1_output_name_mismatch(tmp_path: Path):
    """The output signature is name+order sensitive — a renamed output is a
    broken weld even with identical dims."""
    pytest.importorskip("onnx")
    art = _facet_ws(tmp_path, features=False)
    _tiny_onnx(art / "variants" / "r1-01" / "onnx" / "model.onnx",
               (1, 8), (1, 2), out_name="renamed_output")
    proc = _facet_check(art)
    assert proc.returncode == 2
    assert "duty ① FAIL" in proc.stderr


def test_facet_check_missing_variant_onnx(tmp_path: Path):
    pytest.importorskip("onnx")
    art = _facet_ws(tmp_path, features=False)
    (art / "variants" / "r1-01" / "onnx" / "model.onnx").unlink()
    proc = _facet_check(art)
    assert proc.returncode == 2
    assert "ONNX model missing" in proc.stderr


def test_facet_check_features_workspace_requires_variant_facets_dir(
        tmp_path: Path):
    pytest.importorskip("onnx")
    art = _facet_ws(tmp_path, features=True)
    shutil.rmtree(art / "variants" / "r1-01" / "facets")
    proc = _facet_check(art)
    assert proc.returncode == 2
    assert "facets is missing" in proc.stderr


def test_facet_check_duty2_builder_return_shapes(tmp_path: Path):
    """The builder's return contract: positional arity, dict name set, and
    real tensors — each violation names its root cause."""
    pytest.importorskip("onnx")
    cases = {
        "arity": "def build(x):\n    return [x, x]\n",
        "names": "def build(x):\n    return {'wrong_name': x}\n",
        "nontensor": "def build(x):\n    return {'x': 5}\n",
    }
    for label, body in cases.items():
        art = _facet_ws(tmp_path / label, features=True, builder_body=body)
        proc = _facet_check(art)
        assert proc.returncode == 2, (label, proc.stderr)
        assert "duty ② FAIL" in proc.stderr, (label, proc.stderr)


def test_facet_check_duty2_missing_builder_module(tmp_path: Path):
    pytest.importorskip("onnx")
    art = _facet_ws(tmp_path, features=True)
    doc = json.loads((art / "contracts.json").read_text(encoding="utf-8"))
    doc["eval"]["facet_builder"] = {"module": "feat.absent",
                                    "factory": "build"}
    (art / "contracts.json").write_text(json.dumps(doc), encoding="utf-8")
    proc = _facet_check(art)
    assert proc.returncode == 2
    assert "builder module is missing" in proc.stderr


def test_facet_check_duty2_rejects_unknown_sample_dtype(tmp_path: Path):
    pytest.importorskip("onnx")
    art = _facet_ws(tmp_path, features=True)
    doc = json.loads((art / "contracts.json").read_text(encoding="utf-8"))
    doc["eval"]["sample_inputs"] = [
        {"name": "x", "shape": [1, 8], "dtype": "bfloat16"}]
    (art / "contracts.json").write_text(json.dumps(doc), encoding="utf-8")
    proc = _facet_check(art)
    assert proc.returncode == 2
    assert "outside the supported set" in proc.stderr


# ── recheck facets diff == facet_edited_files (§3 Step 3) ─────────────────────

def _recheck_ws(tmp_path: Path, *, facet_edited: list[str],
                edit_variant_facets: bool) -> Path:
    """Recheck workspace with REAL schedule_result.json raw products (the
    measurement path reads the raw JSON — no profiling run needed here) and
    a facets pair $ART/facets vs variants/r1-01/facets."""
    art = tmp_path / "art"
    (art / "scripts").mkdir(parents=True)
    for src in ("diff_check.py", "history_lib.py", "emit_result.py",
                "round_state.py", "check_verdict.py"):
        shutil.copy(_V2 / src, art / "scripts" / src)
    (art / "base" / "profile" / "model").mkdir(parents=True)
    (art / "base" / "profile" / "model" / "schedule_result.json").write_text(
        json.dumps({"parallel_cycles": 1000}), encoding="utf-8")
    (art / "base" / "profile" / "mfu_bottleneck_report.md").write_text(
        "report\n", encoding="utf-8")
    (art / "base" / "model.onnx").write_bytes(b"onnx")
    (art / "base" / "origin_anchor.json").write_text(json.dumps({
        "baseline_makespan_cycles": 1000, "latency_reduction_min": 0.5,
        "accuracy_budget": 0.1, "target_cycles": 500, "frozen_at_round": 0}),
        encoding="utf-8")
    (art / "shadow" / "pkg").mkdir(parents=True)
    (art / "shadow" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (art / "shadow" / "pkg" / "model.py").write_text("m", encoding="utf-8")

    vd = art / "variants" / "r1-01"
    shutil.copytree(art / "shadow", vd / "shadow")
    (vd / "profile" / "model").mkdir(parents=True)
    (vd / "profile" / "model" / "schedule_result.json").write_text(
        json.dumps({"parallel_cycles": 900}), encoding="utf-8")
    (vd / "profile" / "mfu_bottleneck_report.md").write_text(
        "report\n", encoding="utf-8")
    (vd / "onnx").mkdir()
    (vd / "onnx" / "model.onnx").write_bytes(b"onnx")
    (vd / "declaration.json").write_text(json.dumps({
        "vid": "r1-01", "edited_files": [], "predicted_delta_cycles": -100,
        "facet_edited_files": facet_edited}), encoding="utf-8")
    (vd / "DONE").write_text("", encoding="utf-8")

    # facets pair: origin copy at the workspace root, variant copy under the vid
    origin_body = "# origin feature pipeline\n"
    (art / "facets" / "feat").mkdir(parents=True)
    (art / "facets" / "feat" / "pipeline.py").write_text(
        origin_body, encoding="utf-8")
    (vd / "facets" / "feat").mkdir(parents=True)
    (vd / "facets" / "feat" / "pipeline.py").write_text(
        ("# edited variant feature pipeline\n" if edit_variant_facets
         else origin_body), encoding="utf-8")

    (art / "rounds" / "001").mkdir(parents=True)
    (art / "contracts.json").write_text(json.dumps(
        {"interpreter": {"sys_executable": sys.executable}}), encoding="utf-8")
    history_lib.append_implemented(
        art / "history.jsonl", "r1-01", round=1, seq=1,
        change_sig="sig:r1-01", probe_epochs=1, target_modules=["m"],
        absorbs=[], predicted_delta_cycles=-100,
        structure_change="首层收窄", feature_change="剔除冗余特征列",
        loss_change="未改")
    return art


def _recheck(art: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    return subprocess.run(["bash", str(_RECHECK_SH)], capture_output=True,
                          text=True, timeout=120, env=env)


def test_recheck_facets_diff_equivalence_positive(tmp_path: Path):
    """An EDITED variant facets tree with the edit declared exactly ->
    structural pass -> the measurement runs (900 < the frozen 1000 line)."""
    art = _recheck_ws(tmp_path, facet_edited=["feat/pipeline.py"],
                      edit_variant_facets=True)
    proc = _recheck(art)
    assert proc.returncode == 0, proc.stderr
    verdict = json.loads((art / "variants" / "r1-01" / "verdict.json")
                         .read_text(encoding="utf-8"))
    assert verdict["outcome"] == "latency_improved"

    # the untouched twin: identical trees + empty declaration -> also passes
    art2 = _recheck_ws(tmp_path / "b", facet_edited=[],
                       edit_variant_facets=False)
    proc2 = _recheck(art2)
    assert proc2.returncode == 0, proc2.stderr
    verdict2 = json.loads((art2 / "variants" / "r1-01" / "verdict.json")
                          .read_text(encoding="utf-8"))
    assert verdict2["outcome"] == "latency_improved"


def test_recheck_facets_diff_equivalence_negative(tmp_path: Path):
    """An EDITED variant facets tree with an EMPTY declaration = a fabricated
    facet edit -> structural_mismatch (the facet twin of diff==edited)."""
    art = _recheck_ws(tmp_path, facet_edited=[], edit_variant_facets=True)
    proc = _recheck(art)
    assert proc.returncode == 0, proc.stderr
    verdict = json.loads((art / "variants" / "r1-01" / "verdict.json")
                         .read_text(encoding="utf-8"))
    assert verdict["outcome"] == "structural_mismatch"
    notes = [note
             for line in (art / "rounds" / "001" / "verdicts.jsonl")
             .read_text(encoding="utf-8").splitlines() if line.strip()
             for note in json.loads(line).get("mismatch_layers", [])]
    assert any("facet: not_declared=feat/pipeline.py" in n for n in notes), notes

    # the mirror: declared-but-absent (identical trees + a declared edit)
    art2 = _recheck_ws(tmp_path / "b", facet_edited=["feat/pipeline.py"],
                       edit_variant_facets=False)
    proc2 = _recheck(art2)
    assert proc2.returncode == 0, proc2.stderr
    verdict2 = json.loads((art2 / "variants" / "r1-01" / "verdict.json")
                          .read_text(encoding="utf-8"))
    assert verdict2["outcome"] == "structural_mismatch"


def test_recheck_structure_only_workspace_skips_facet_layer(tmp_path: Path):
    """No $ART/facets at all (structure-only workspace): the facet layer
    skips and a declaration WITHOUT facet_edited_files still measures."""
    art = _recheck_ws(tmp_path, facet_edited=[], edit_variant_facets=False)
    shutil.rmtree(art / "facets")
    shutil.rmtree(art / "variants" / "r1-01" / "facets")
    proc = _recheck(art)
    assert proc.returncode == 0, proc.stderr
    verdict = json.loads((art / "variants" / "r1-01" / "verdict.json")
                         .read_text(encoding="utf-8"))
    assert verdict["outcome"] == "latency_improved"


def test_recheck_missing_facet_edited_files_is_fatal(tmp_path: Path):
    """$ART/facets exists but the declaration lacks facet_edited_files ->
    hard error (a v2 declaration always carries the list)."""
    art = _recheck_ws(tmp_path, facet_edited=[], edit_variant_facets=False)
    decl = json.loads((art / "variants" / "r1-01" / "declaration.json")
                      .read_text(encoding="utf-8"))
    decl.pop("facet_edited_files")
    (art / "variants" / "r1-01" / "declaration.json").write_text(
        json.dumps(decl), encoding="utf-8")
    proc = _recheck(art)
    assert proc.returncode == 2
    assert "facet_edited_files" in proc.stderr


# ── check_contracts: facets fingerprint (reuse face) + token contract ─────────

def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _contracts_ws(tmp_path: Path, *, facets: dict | None,
                  facet_templates: bool = True) -> tuple[Path, Path]:
    """A v7-shaped contract workspace + the v2 facets block; returns
    (art, project_root) — the facet origin files live in the fake user
    project, the copies under $ART/facets/."""
    art = tmp_path / "art"
    proj = tmp_path / "proj"
    (art / "templates").mkdir(parents=True)
    (art / "contract_work").mkdir(parents=True)
    (art / "scripts").mkdir(parents=True)
    shutil.copy(_V2 / "metric_curve.py", art / "scripts" / "metric_curve.py")
    for name in ("train.py", "eval.py", "exporter.py"):
        (art / name).write_text("# entry\n", encoding="utf-8")
    for rel in (FEATURE_FILE, LOSS_FILE):
        (proj / rel).parent.mkdir(parents=True, exist_ok=True)
        (proj / rel).write_text(f"# user facet {rel}\n", encoding="utf-8")
        (art / "facets" / rel).parent.mkdir(parents=True, exist_ok=True)
        (art / "facets" / rel).write_text(f"# user facet {rel}\n",
                                          encoding="utf-8")

    contracts = {
        "viable": True, "reason": "tier A, measured",
        "interpreter": {"sys_executable": sys.executable, "flags_check": "pass"},
        "shadow": {"shadow_root": str(art / "shadow"), "shadow_pkgs": ["pkg"]},
        "model_facts": {"module": "pkg.model", "factory": "build",
                        "args": [], "kwargs": {},
                        "dummy_inputs": [{"name": "x", "shape": [1, 4],
                                          "dtype": "float32"}]},
        "train": {"tier": "A", "entry": str(art / "train.py"),
                  "entry_sha256": _sha(art / "train.py"),
                  "flags": {"epochs": "--epochs", "out_dir": "--out-dir",
                            "seed": "--seed"},
                  "ckpt_output_rule": "{out_dir}/epoch_*.pth",
                  "ckpt_per_epoch": True,
                  "epoch_metric_extraction": {
                      "kind": "stdout_regex",
                      "pattern": r"epoch (?P<epoch>\d+) metric=(?P<metric>[0-9.]+)"},
                  "train_epochs_full": 10},
        "full_train_budget": {"epochs": 2, "seed": 0},
        "eval": {"tier": "A", "entry": str(art / "eval.py"),
                 "entry_sha256": _sha(art / "eval.py"),
                 "flags": {"ckpt": "--ckpt"}, "ckpt_container": "bare",
                 "metric_extraction": {"kind": "stdout_regex",
                                       "pattern": "acc=([0-9.]+)"},
                 "metric_direction": "higher_better"},
        "export": {"entry": str(art / "exporter.py"),
                   "entry_sha256": _sha(art / "exporter.py"),
                   "generated": False, "argv_facts": "pinned"},
        "proxy_budget": {"epochs": 1, "seed": 0},
        "probe_cap_mechanism": "stop-at-k",
        "profile": {"chip": "6613", "precision": "INT8", "core_num": 1},
        "early_stop": {"warmup_frac": 0.1, "streak_frac": 0.3},
        "admission_clause_ack": True, "exemptions": [],
        "sitecustomize_merge": {"found": False, "path": "", "merged": False},
    }
    if facets is not None:
        block = dict(facets)
        # FLAT writer shape, verbatim as the po_contract agent document pins
        # it: {<origin absolute path>: sha256} — the reader pairs origins to
        # copies through the located-file lists (this fixture is the
        # write-side shape pin: a nested shape here would silently test the
        # reader against itself)
        block["origin_hashes"] = {str(proj / rel): _sha(proj / rel)
                                  for rel in (FEATURE_FILE, LOSS_FILE)}
        contracts["facets"] = block
    (art / "contracts.json").write_text(json.dumps(contracts), encoding="utf-8")

    train_body = ('"<<python>>" train.py --epochs <<epochs>> --out-dir '
                  '<<out_dir>> --seed <<seed>> --device <<device>>')
    eval_body = '"<<python>>" eval.py --ckpt <<ckpt>> > <<log>> 2>&1'
    export_body = '"<<python>>" exporter.py --out <<out>> --seed <<seed>>'
    if facet_templates:
        feat = facets and facets.get("features") is True
        loss = facets and facets.get("loss") is True
        exp = facets and facets.get("export_consumes_features") is True
        if feat or loss:
            train_body += " --facet-dir <<facet_dir>>"
        if feat:
            eval_body += " --facet-dir <<facet_dir>>"
        if exp:
            export_body += " --facet-dir <<facet_dir>>"
    (art / "templates" / "run_full_finetune.template.sh").write_text(
        train_body + "\n", encoding="utf-8")
    (art / "templates" / "run_eval.template.sh").write_text(
        eval_body + "\n", encoding="utf-8")
    (art / "templates" / "export_onnx.template.sh").write_text(
        export_body + "\n", encoding="utf-8")

    cw = art / "contract_work"
    (cw / "quickrun_train.log").write_text(
        "epoch 1 metric=0.9\nepoch 2 metric=0.8\n", encoding="utf-8")
    (cw / "train_quickrun.json").write_text(
        json.dumps({"status": "runs_minimal_budget",
                    "train_log": str(cw / "quickrun_train.log"),
                    "epoch_metric_extraction_check": "pass"}), encoding="utf-8")
    (cw / "eval_dual_ckpt.json").write_text(
        json.dumps({"metric_seed0": 0.1, "metric_seed1": 0.6, "moved": True,
                    "ckpt_container": "bare"}), encoding="utf-8")
    (cw / "export_check.json").write_text(
        json.dumps({"loaded": True}), encoding="utf-8")
    (cw / "proxy_budget_selection.json").write_text(
        json.dumps({"epochs": 1, "seed": 0,
                    "rationale": "epoch-only probe depth k=1"}), encoding="utf-8")
    return art, proj


def _contracts_run(art: Path, *extra: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    return subprocess.run(["bash", str(_CONTRACTS_SH), *extra],
                          capture_output=True, text=True, timeout=60, env=env)


def test_contracts_gate_green_with_facets_and_second_reuse(tmp_path: Path):
    """The 锁/枚举零改动 regression: facets/ present at the workspace root
    (outside shadow/) — the gate passes AND a second reuse passes (the facet
    fingerprint rides the profile-parameter track)."""
    art, _ = _contracts_ws(tmp_path, facets=_facets_block(features=True,
                                                          loss=False))
    assert _contracts_run(art).returncode == 0
    assert _contracts_run(art, "--reuse-check").returncode == 0     # 2nd reuse


def test_contracts_reuse_facet_origin_drift_needs_fresh_start(tmp_path: Path):
    art, proj = _contracts_ws(tmp_path, facets=_facets_block(features=True,
                                                             loss=False))
    (proj / FEATURE_FILE).write_text("# user edited it later\n",
                                     encoding="utf-8")
    proc = _contracts_run(art, "--reuse-check")
    assert proc.returncode == 3
    assert "facet origin sha256 drift" in proc.stderr
    assert "fresh_start" in proc.stderr


def test_contracts_reuse_facet_copy_drift_needs_fresh_start(tmp_path: Path):
    art, _ = _contracts_ws(tmp_path, facets=_facets_block(features=True,
                                                          loss=False))
    (art / "facets" / FEATURE_FILE).write_text("# workspace copy edited\n",
                                               encoding="utf-8")
    proc = _contracts_run(art, "--reuse-check")
    assert proc.returncode == 3
    assert "facets copy hash mismatch" in proc.stderr


def test_contracts_reuse_missing_facets_copy(tmp_path: Path):
    art, _ = _contracts_ws(tmp_path, facets=_facets_block(features=True,
                                                          loss=False))
    (art / "facets" / FEATURE_FILE).unlink()
    proc = _contracts_run(art, "--reuse-check")
    assert proc.returncode == 3
    assert "facets copy missing" in proc.stderr


def test_contracts_reuse_unpairable_origin_hashes(tmp_path: Path):
    """The origin->copy pairing rides the located-file lists: an origin
    file that EXISTS with the recorded sha but whose absolute path pairs
    with NO recorded rel is a torn fingerprint — the pairing failure is
    the ONLY failure here (no missing-file drift to mask it)."""
    art, proj = _contracts_ws(tmp_path, facets=_facets_block(features=True,
                                                             loss=False))
    # a real, sha-matching origin at a path that does NOT end with any
    # recorded rel (elsewhere/pipeline.py vs feat/pipeline.py)
    moved = proj / "elsewhere" / "pipeline.py"
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(proj / FEATURE_FILE, moved)
    doc = json.loads((art / "contracts.json").read_text(encoding="utf-8"))
    doc["facets"]["origin_hashes"] = {
        str(moved): _sha(proj / FEATURE_FILE)}
    (art / "contracts.json").write_text(json.dumps(doc), encoding="utf-8")
    proc = _contracts_run(art, "--reuse-check")
    assert proc.returncode == 3
    assert "does not pin exactly one origin" in proc.stderr
    assert "'feat/pipeline.py'" in proc.stderr      # the rel that lost its pin


def test_contracts_version_discriminator_rejects_blockless_facets(
        tmp_path: Path):
    art, _ = _contracts_ws(tmp_path, facets=None)
    proc = _contracts_run(art, "--reuse-check")
    assert proc.returncode == 3
    assert "predates the current workflow version" in proc.stderr
    assert "facets" in proc.stderr
    # the gate mode demands it as a plain required field too
    assert _contracts_run(art).returncode == 1


def test_contracts_token_contract_per_consumption_face(tmp_path: Path):
    """train ⇔ features∨loss; eval ⇔ features; export ⇔ export_consumes —
    both directions, plus the v8 no-token baseline."""
    # loss only: train carries the token, eval must NOT
    art, _ = _contracts_ws(tmp_path / "lossonly",
                           facets=_facets_block(features=False, loss=True))
    assert _contracts_run(art).returncode == 0

    # export consumes features: the export template grows the token
    art2, _ = _contracts_ws(
        tmp_path / "expfeat",
        facets=_facets_block(features=True, loss=False, export_consumes=True))
    assert _contracts_run(art2).returncode == 0

    # capabilities without the token on eval -> fail
    art3, _ = _contracts_ws(tmp_path / "notoken", facets=_facets_block(
        features=True, loss=False), facet_templates=False)
    proc = _contracts_run(art3)
    assert proc.returncode == 1
    assert "lacks <<facet_dir>>" in proc.stderr

    # token without the capability -> fail (eval template carries it while
    # features=false and loss=false)
    art4, _ = _contracts_ws(tmp_path / "nocap", facets=_facets_block(
        features=False, loss=False), facet_templates=False)
    tpl = art4 / "templates" / "run_eval.template.sh"
    tpl.write_text(tpl.read_text(encoding="utf-8").rstrip("\n")
                   + " --facet-dir <<facet_dir>>\n", encoding="utf-8")
    proc4 = _contracts_run(art4)
    assert proc4.returncode == 1
    assert "without the matching facets capability" in proc4.stderr

    # no capability at all: three templates without the token pass as v8
    art5, _ = _contracts_ws(tmp_path / "plain", facets=_facets_block(
        features=False, loss=False), facet_templates=False)
    assert _contracts_run(art5).returncode == 0


# ── lock/enumeration zero-change regression ──────────────────────────────────

_LIST_PKGS_SH = (_REPO / "workflows" / "profiling-v2" / "agents" / "po_flatten"
                 / "scripts" / "list_shadow_pkgs.sh")


def test_list_shadow_pkgs_ignores_facets_dir(tmp_path: Path):
    """facets/ lives OUTSIDE shadow/ — the mechanical enumeration never sees
    it (the §0 shadow-isolation invariant, kept by construction)."""
    shadow = tmp_path / "shadow"
    (shadow / "pkg").mkdir(parents=True)
    (shadow / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (shadow / "solo.py").write_text("", encoding="utf-8")
    proc = subprocess.run(["bash", str(_LIST_PKGS_SH), str(shadow)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["pkg", "solo"]

    art = tmp_path / "art"
    (art / "facets" / "feat").mkdir(parents=True)
    (art / "facets" / "feat" / "pipeline.py").write_text("", encoding="utf-8")
    proc2 = subprocess.run(["bash", str(_LIST_PKGS_SH), str(art / "facets")],
                           capture_output=True, text=True, timeout=30)
    assert proc2.returncode == 0, proc2.stderr
    # the facets tree is a valid package enumeration target of its own —
    # what matters is that nothing ever passes it off as the shadow
    assert proc2.stdout.split() == ["feat"]


# ── push_curves docs manifest (§2.3) ──────────────────────────────────────────

def _docs_ws(tmp_path: Path, *, features: bool) -> Path:
    art = tmp_path / "art"
    (art / "base").mkdir(parents=True)
    (art / "base" / "history.md").write_text(
        "# derived view\nrender_stamp: {\"last_line_no\": 0, \"lines\": 0}\n",
        encoding="utf-8")
    (art / "base" / "history_digest.md").write_text(
        "[subagent:history-curator v1 HDC7Q2]\nstale v8 digest\n",
        encoding="utf-8")                    # retired: must NOT be listed
    (art / "base" / "accuracy_rules_snapshot.json").write_text("{}\n",
                                                               encoding="utf-8")
    (art / "contracts.json").write_text(json.dumps(
        {"facets": _facets_block(features=features, loss=False)}),
        encoding="utf-8")
    rd = art / "rounds" / "001"
    (rd / "candidates").mkdir(parents=True)
    for name, sentinel in {**CANDIDATE_SENTINELS,
                           "feature.md": FEATURE_SENTINEL}.items():
        (rd / "candidates" / name).write_text(sentinel + "\n", encoding="utf-8")
    (rd / "analysis.md").write_text("## latency\n", encoding="utf-8")
    (rd / "architecture_decision.md").write_text("dec\n", encoding="utf-8")
    return art


def test_docs_manifest_history_row_replaces_digest(tmp_path: Path):
    art = _docs_ws(tmp_path, features=False)
    rows = push_curves.collect_docs(art)
    paths = {(r["vid"], r["doc"], r["path"]) for r in rows}
    assert ("digest", "history.md", "base/history.md") in paths
    assert not any("history_digest" in p for _, _, p in paths)
    cap = next(r for r in rows if r["path"] == "contracts.json")
    assert cap["doc"] == "facets capability"
    assert cap["status"] == "features:no;loss:no"
    assert not any(r["path"].endswith("feature.md") for r in rows)


def test_docs_manifest_feature_row_and_capability_conditional(tmp_path: Path):
    art = _docs_ws(tmp_path, features=True)
    rows = push_curves.collect_docs(art)
    feature_rows = [r for r in rows if r["path"].endswith("feature.md")]
    assert len(feature_rows) == 1
    assert feature_rows[0]["status"] == "candidate"
    cap = next(r for r in rows if r["path"] == "contracts.json")
    assert cap["status"] == "features:yes;loss:no"


# ── per-round facets archive (§3 Step 3) ─────────────────────────────────────

def _seed_round(art: Path, vid: str, *, facets: bool) -> None:
    rd = art / "rounds" / "003"
    (rd / "candidates").mkdir(parents=True, exist_ok=True)
    props_path = rd / "proposals.json"
    props = (json.loads(props_path.read_text(encoding="utf-8"))
             if props_path.is_file() else {"round": 3, "proposals": []})
    props["proposals"].append({"vid": vid})       # accumulate per-vid seeds
    props_path.write_text(json.dumps(props), encoding="utf-8")
    sdir = art / "variants" / vid / "shadow"
    (sdir / "sub").mkdir(parents=True)
    (sdir / "model.py").write_text(f"# {vid} model\n", encoding="utf-8")
    if facets:
        fdir = art / "variants" / vid / "facets" / "feat"
        fdir.mkdir(parents=True)
        (fdir / "pipeline.py").write_text(f"# {vid} pipeline\n",
                                          encoding="utf-8")


def _archive(art: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_V2 / "archive_round_shadow.py"),
         "--artifacts", str(art)],
        capture_output=True, text=True, timeout=60)


def test_archive_includes_facets_and_skips_absent(tmp_path: Path):
    art = tmp_path / "art"
    art.mkdir()
    _seed_round(art, "r1-01", facets=True)
    _seed_round(art, "r2-01", facets=False)      # no facets copy — legal
    proc = _archive(art)
    assert proc.returncode == 0, proc.stderr
    r1 = art / "rounds" / "003" / "r1-01"
    assert (r1 / "shadow" / "model.py").is_file()
    assert (r1 / "facets" / "feat" / "pipeline.py").is_file()
    assert (art / "rounds" / "003" / "r2-01" / "shadow").is_dir()
    assert not (art / "rounds" / "003" / "r2-01" / "facets").exists()
    assert not (art / "rounds" / "003" / "shadow_archive_error.json").exists()

    # idempotent replay: nothing re-copied, no error entries
    before = (r1 / "facets" / "feat" / "pipeline.py").read_text("utf-8")
    (art / "variants" / "r1-01" / "facets" / "feat" / "pipeline.py").write_text(
        "# mutated later\n", encoding="utf-8")
    assert _archive(art).returncode == 0
    assert (r1 / "facets" / "feat" / "pipeline.py").read_text("utf-8") == before


# ── render_run's optional facet_dir token (§4) ───────────────────────────────

def _render_run_deployed(tmp_path: Path) -> Path:
    art = tmp_path / "art"
    (art / "scripts").mkdir(parents=True)
    (art / "orca_inject").mkdir(parents=True)
    shutil.copy(_V2 / "render_run.sh", art / "scripts" / "render_run.sh")
    shutil.copy(_V2 / "assert_shadow.py", art / "scripts" / "assert_shadow.py")
    shutil.copy(_V2 / "orca_inject" / "header.env",
                art / "orca_inject" / "header.env")
    return art


def _render(art: Path, template: Path, out: Path, *sets: str
            ) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "ORCA_PYTHON"}
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    return subprocess.run(
        ["bash", str(art / "scripts" / "render_run.sh"),
         "--template", str(template), "--out", str(out),
         *(arg for pair in sets for arg in ("--set", pair))],
        capture_output=True, text=True, timeout=60, env=env)


def test_render_run_facet_dir_optional_token(tmp_path: Path):
    art = _render_run_deployed(tmp_path)
    facet_tpl = art / "facet.template.sh"
    facet_tpl.write_text(
        '"<<python>>" train.py --facet-dir <<facet_dir>>\n', encoding="utf-8")
    plain_tpl = art / "plain.template.sh"
    plain_tpl.write_text('"<<python>>" train.py\n', encoding="utf-8")

    base_sets = ("shadow_dir=/tmp/shadow", "shadow_pkgs=pkg",
                 "project_root=/tmp/proj")

    # supplied -> substituted literally
    proc = _render(art, facet_tpl, art / "a.rendered.sh",
                   *base_sets, "facet_dir=/ws/variants/r1-01/facets")
    assert proc.returncode == 0, proc.stderr
    body = (art / "a.rendered.sh").read_text(encoding="utf-8")
    assert "--facet-dir /ws/variants/r1-01/facets" in body
    assert "<<facet_dir>>" not in body

    # template carries the token, the call site supplies nothing -> the
    # unreplaced-token fail loud (a rendered run must never carry an empty
    # placeholder)
    proc2 = _render(art, facet_tpl, art / "b.rendered.sh", *base_sets)
    assert proc2.returncode == 2
    assert "unreplaced template tokens" in proc2.stderr
    assert "<<facet_dir>>" in proc2.stderr
    assert not (art / "b.rendered.sh").exists()

    # token-less template + a supplied key: the extra --set is simply unused
    proc3 = _render(art, plain_tpl, art / "c.rendered.sh",
                    *base_sets, "facet_dir=/unused")
    assert proc3.returncode == 0, proc3.stderr


# ── gate stdout purity with render_history mounted (§4 po_gate) ───────────────

def test_gate_node_stdout_stays_pure_json(tmp_path: Path):
    """render_history prints the whole markdown on stdout (positive control
    below) — the gate node mount must redirect it so po_gate's parse_json
    face sees exactly one JSON line."""
    art = tmp_path / "art"
    art.mkdir(parents=True)
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    deploy = subprocess.run(["bash", str(_V2 / "deploy_scripts.sh")],
                            capture_output=True, text=True, timeout=60,
                            env=env)
    assert deploy.returncode == 0, deploy.stderr
    (art / "base").mkdir(parents=True)
    (art / "base" / "origin_anchor.json").write_text(json.dumps({
        "baseline_makespan_cycles": 1000, "latency_reduction_min": 0.5,
        "accuracy_budget": 0.1, "target_cycles": 500, "frozen_at_round": 0}),
        encoding="utf-8")
    (art / "rounds" / "001").mkdir(parents=True)
    (art / "rounds" / "001" / "proposals.json").write_text(
        json.dumps({"round": 1, "proposals": []}), encoding="utf-8")

    proc = subprocess.run(
        ["bash", str(_V2 / "gate_node.sh"), "--max-rounds", "5",
         "--idle-round-cap", "2"],
        capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == 0, proc.stderr
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    assert len(lines) == 1, proc.stdout          # exactly one JSON line
    payload = json.loads(lines[0])
    assert payload["decision"] in ("loop", "report")
    # the derived view was refreshed as part of the gate pass
    assert (art / "base" / "history.md").is_file()

    # positive control: render_history itself IS a markdown-printing stdout
    direct = subprocess.run(
        [sys.executable, str(_V2 / "render_history.py"), "--artifacts",
         str(art)], capture_output=True, text=True, timeout=60)
    assert direct.returncode == 0
    assert direct.stdout.count("\n") > 1
    assert direct.stdout.startswith("#")


def test_gate_node_render_history_failure_maps_to_finish_failed(
        tmp_path: Path):
    """A render that fails loud must land in the same finish-failed payload
    as the frontier snapshot failure — never a half-parsed stdout. The
    failure is render-specific (a history row whose round has no rounds/
    directory: the frontier snapshot tolerates it, the render does not)."""
    art = tmp_path / "art"
    art.mkdir(parents=True)
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    deploy = subprocess.run(["bash", str(_V2 / "deploy_scripts.sh")],
                            capture_output=True, text=True, timeout=60,
                            env=env)
    assert deploy.returncode == 0, deploy.stderr
    (art / "base").mkdir(parents=True)
    (art / "base" / "origin_anchor.json").write_text(json.dumps({
        "baseline_makespan_cycles": 1000, "latency_reduction_min": 0.5,
        "accuracy_budget": 0.1, "target_cycles": 500, "frozen_at_round": 0}),
        encoding="utf-8")
    (art / "rounds" / "001").mkdir(parents=True)
    (art / "rounds" / "001" / "proposals.json").write_text(
        json.dumps({"round": 1, "proposals": []}), encoding="utf-8")
    # a well-formed row for round 5 while rounds/ tops at 001 — torn for the
    # render only
    (art / "history.jsonl").write_text(json.dumps({
        "vid": "r5-01", "round": 5, "seq": 1, "change_sig": "sig:r5-01",
        "probe_epochs": 1, "target_modules": ["m"], "absorbs": [],
        "implemented": True, "version": 1,
        "ts": "2026-09-11T00:00:00+00:00",
        "structure_change": "s", "feature_change": "未改",
        "loss_change": "未改"}) + "\n", encoding="utf-8")

    proc = subprocess.run(
        ["bash", str(_V2 / "gate_node.sh"), "--max-rounds", "5",
         "--idle-round-cap", "2"],
        capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == 0, proc.stderr
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    assert len(lines) == 1, proc.stdout
    payload = json.loads(lines[0])
    assert payload["decision"] == "finish-failed"
    assert payload["reason"] == "history render failed"


def test_watch_variant_supplies_facet_dir_conditionally():
    """Structural pin (the functional face is the E2E watchdog chain): the
    eval render's --set list grows facet_dir ONLY behind the FACET_DIR
    guard, and FACET_DIR is derived from contracts.json facets."""
    src = (_V2 / "watch_variant.py").read_text(encoding="utf-8")
    render_eval = src[src.index("def render_eval"):src.index("def run_rendered")]
    assert "if FACET_DIR:" in render_eval
    assert 'sets.append(f"facet_dir={FACET_DIR}")' in render_eval
    load_contracts = src[src.index("def _load_contracts"):
                         src.index("def _load_budget")]
    assert 'facets.get("features") is True' in load_contracts
    assert 'facets.get("loss") is True' in load_contracts
    assert 'FACET_DIR = str(ART / "variants" / VID / "facets")' \
        in load_contracts
