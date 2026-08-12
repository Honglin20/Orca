"""test_puzzle_evaluator_recall.py -- puzzle evaluator recall AC (SPEC v2 §16.6 / §16.7).

Two layers:

1. ``TestEvaluatorFixtureIntegrity`` (always runs, no LLM) -- for every seeded
   case under ``tests/e2e_puzzle/fixtures/evaluator_cases/``, asserts the case is
   well-formed and the seeded error is *deterministically present* (so a fixtures
   regression is caught without needing an LLM). This is the always-on guard.

2. ``TestEvaluatorRecall`` (LLM-gated, skips without a backend) -- drives the
   ``block-map-evaluator`` / ``search-space-evaluator`` subagent bodies over the
   fixture suite and asserts:
     * block-map-evaluator recall on its seeded-error cases >= 0.90,
     * search-space-evaluator schema-violation recall == 1.0,
     * the clean baseline is LGTM for both (no false positive).

The recall measurement is environment-gated (needs an LLM backend). The
``evaluator_driver`` prefers the anthropic SDK (Claude) and falls back to opencode
+ deepseek; both are unavailable in this environment today, so the recall class
skips here and is re-measured when a backend is present.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR / "e2e_puzzle"))

import evaluator_driver as ev  # noqa: E402

CASES_ROOT = TESTS_DIR / "e2e_puzzle" / "fixtures" / "evaluator_cases"
SUBAGENTS_DIR = TESTS_DIR.parent / "workflows" / "subagents" / "puzzle"
CATALOG_PATH = TESTS_DIR.parent / "workflows" / "agents" / "_puzzle_scripts" / "candidate_catalog.yaml"

_BLOCK_MAP_CASES = [
    "01_conv_as_attention",
    "02_wrong_path",
    "03_shape_mismatch",
    "04_identity_missing",
    "06_mask_blind_on_mask_slot",
    "07_return_arity_violation",
    "12_candidate_wrong_kind",
]
_SEARCH_SPACE_CASES = [
    "05_eval_kind_mislabel",
    "09_duplicate_id",
    "10_unknown_candidate",
    "11_axes_residual",
]
_ALL_FLAG_CASES = _BLOCK_MAP_CASES + _SEARCH_SPACE_CASES


def _list_case_dirs() -> list[Path]:
    return sorted(p for p in CASES_ROOT.iterdir() if p.is_dir() and (p / "expected.yaml").is_file())


def _load_expected(case_dir: Path) -> dict:
    return yaml.safe_load((case_dir / "expected.yaml").read_text(encoding="utf-8"))


def _load_search_space(case_dir: Path) -> dict:
    return yaml.safe_load((case_dir / "search_space.yaml").read_text(encoding="utf-8"))


def _catalog_names() -> set[str]:
    raw = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    return {entry["name"] for entry in raw}


# ── layer 1: fixture integrity (deterministic, always runs) ──────────────────


class TestEvaluatorFixtureIntegrity:
    """Each seeded case must carry the error it claims to carry."""

    @pytest.mark.parametrize("case_id", [c.name for c in _list_case_dirs()])
    def test_case_well_formed(self, case_id):
        case_dir = CASES_ROOT / case_id
        expected = _load_expected(case_dir)
        # mandatory expected.yaml keys
        for key in ("case_id", "primary_evaluator", "flat_relpath", "expect"):
            assert key in expected, f"{case_id}: expected.yaml missing {key!r}"
        assert expected["case_id"] == case_id
        assert expected["expect"] in ("FLAG", "LGTM")
        assert expected["primary_evaluator"] in (
            "block-map-evaluator",
            "search-space-evaluator",
            "both",
        )
        # the referenced flat model must exist
        flat_path = CASES_ROOT / expected["flat_relpath"]
        assert flat_path.is_file(), f"{case_id}: flat missing at {flat_path}"
        # search_space.yaml + manifest.yaml must parse
        assert (case_dir / "search_space.yaml").is_file()
        assert (case_dir / "manifest.yaml").is_file()
        _load_search_space(case_dir)  # raises on malformed yaml
        yaml.safe_load((case_dir / "manifest.yaml").read_text(encoding="utf-8"))

    @pytest.mark.parametrize(
        "case_id",
        _BLOCK_MAP_CASES + _SEARCH_SPACE_CASES + ["08_clean_baseline"],
    )
    def test_seeded_error_present(self, case_id):
        case_dir = CASES_ROOT / case_id
        ss = _load_search_space(case_dir)
        slots = ss["slots"]
        candidates = ss.get("candidates", {})
        catalog = _catalog_names()
        checker = _SEEDED_ERROR_CHECKS.get(case_id)
        assert checker is not None, f"no integrity check registered for {case_id}"
        ok, msg = checker(slots, candidates, ss, case_dir, catalog)
        assert ok, f"{case_id}: seeded error not present -- {msg}"


def _check_clean(slots, candidates, ss, case_dir, catalog):
    ids = [s["id"] for s in slots]
    if len(set(ids)) != len(ids):
        return False, "ids not unique in clean baseline"
    for kind, names in candidates.items():
        if "identity" not in names:
            return False, f"clean baseline missing identity in {kind}"
        for n in names:
            if n not in catalog:
                return False, f"clean baseline references unknown candidate {n}"
    for s in slots:
        if "axes" in s:
            return False, "clean baseline carries deprecated axes"
    return True, ""


def _check_conv_as_attention(slots, candidates, ss, case_dir, catalog):
    attn = next(s for s in slots if s["kind"] == "attention")
    if attn["source_class"] != "ConvMixer":
        return False, "attention slot source_class is not ConvMixer"
    import importlib.util

    import torch.nn as nn

    flat_abs = CASES_ROOT / "flats" / "conv_attn_flat.py"
    spec = importlib.util.spec_from_file_location("_u2b_conv_attn_flat", flat_abs)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    mixer = mod.build_model().get_submodule("blocks.0.attn")
    has_conv = any(
        isinstance(m, (nn.Conv1d, nn.Conv2d, nn.Conv3d)) for m in mixer.modules()
    )
    has_qkv = any(name.split(".")[-1] == "qkv" for name, _ in mixer.named_modules())
    if not has_conv:
        return False, "attn module has no Conv submodule"
    if has_qkv:
        return False, "attn module has qkv (looks like real attention)"
    return True, ""


def _check_wrong_path(slots, candidates, ss, case_dir, catalog):
    attn = next(s for s in slots if s["kind"] == "attention")
    if "nonexistent" not in attn["path"]:
        return False, "attention slot path is not the unresolvable one"
    return True, ""


def _check_shape_mismatch(slots, candidates, ss, case_dir, catalog):
    attn = next(s for s in slots if s["kind"] == "attention")
    if attn["in_dim"] != 64 or attn["out_dim"] != 64:
        return False, "attention slot does not declare the wrong dims 64/64"
    return True, ""


def _check_identity_missing(slots, candidates, ss, case_dir, catalog):
    if "identity" in candidates.get("attention", []):
        return False, "identity is present (the seeded omission is gone)"
    return True, ""


def _check_eval_kind_mislabel(slots, candidates, ss, case_dir, catalog):
    """manifest carries eval_kind=classification while every other paradigm
    signal (loss / metric / output shape) describes an embedding model."""
    manifest = yaml.safe_load((case_dir / "manifest.yaml").read_text(encoding="utf-8"))
    te = manifest["training_and_evaluation"]
    if te.get("eval_kind") != "classification":
        return False, "manifest eval_kind is not classification"
    embedding_signals = (
        te.get("paradigm", "").lower().find("metric") >= 0
        or "infonce" in str(te.get("loss", "")).lower()
        or "k-nn" in str(te.get("metric", {}).get("name", "")).lower()
        or "knn" in str(te.get("metric", {}).get("name", "")).lower()
    )
    if not embedding_signals:
        return False, "manifest has no embedding signal contradicting classification"
    return True, ""


def _check_mask_blind(slots, candidates, ss, case_dir, catalog):
    attn = next(s for s in slots if s["kind"] == "attention")
    if not attn.get("mask_load_bearing"):
        return False, "attention slot is not mask_load_bearing"
    if "fnet" not in candidates.get("attention", []):
        return False, "mask-blind candidate fnet not offered"
    return True, ""


def _check_return_arity(slots, candidates, ss, case_dir, catalog):
    attn = next(s for s in slots if s["kind"] == "attention")
    if attn.get("return_arity") != "multi":
        return False, "attention slot return_arity is not multi"
    return True, ""


def _check_duplicate_id(slots, candidates, ss, case_dir, catalog):
    ids = [s["id"] for s in slots]
    if len(set(ids)) == len(ids):
        return False, "ids are unique (duplicate seeded away)"
    return True, ""


def _check_unknown_candidate(slots, candidates, ss, case_dir, catalog):
    names = {n for ns in candidates.values() for n in ns}
    unknown = names - catalog
    if not unknown:
        return False, "no unknown candidate name present"
    return True, ""


def _check_axes_residual(slots, candidates, ss, case_dir, catalog):
    if not any("axes" in s for s in slots):
        return False, "no slot carries the deprecated axes field"
    return True, ""


def _check_candidate_wrong_kind(slots, candidates, ss, case_dir, catalog):
    """a candidate whose catalog kind list excludes the kind key it was listed under."""
    catalog_path = CATALOG_PATH
    raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    kinds_by_name = {entry["name"]: set(entry["kind"]) for entry in raw}
    for kind, names in candidates.items():
        for n in names:
            applicable = kinds_by_name.get(n)
            if applicable is not None and kind not in applicable:
                return True, ""  # the seeded mismatch is present
    return False, "no candidate-kind mismatch present"


_SEEDED_ERROR_CHECKS = {
    "08_clean_baseline": _check_clean,
    "01_conv_as_attention": _check_conv_as_attention,
    "02_wrong_path": _check_wrong_path,
    "03_shape_mismatch": _check_shape_mismatch,
    "04_identity_missing": _check_identity_missing,
    "05_eval_kind_mislabel": _check_eval_kind_mislabel,
    "06_mask_blind_on_mask_slot": _check_mask_blind,
    "07_return_arity_violation": _check_return_arity,
    "09_duplicate_id": _check_duplicate_id,
    "10_unknown_candidate": _check_unknown_candidate,
    "11_axes_residual": _check_axes_residual,
    "12_candidate_wrong_kind": _check_candidate_wrong_kind,
}


# ── layer 2: LLM recall (gated) ──────────────────────────────────────────────


def _score(verdict: str, expected: dict) -> bool:
    if expected["expect"] == "LGTM":
        return verdict.strip().upper().startswith("LGTM")
    if verdict.strip().upper().startswith("LGTM"):
        return False  # the evaluator missed the seeded error
    text = verdict.lower()
    return any(kw.lower() in text for kw in expected["must_mention_one_of"])


_BACKEND_BROKEN_REASON: str | None = None  # latch: once a backend call fails, skip the rest fast


def _run_evaluator_or_skip(evaluator_name: str, case_dir: Path, expected: dict) -> str:
    """Run the evaluator, skipping the test when the backend is unusable at runtime.

    ``llm_available()`` only checks that a backend looks configured (auth file /
    api key). A stale backend (e.g. deepseek with zero balance, an expired auth
    token, a timeout) surfaces only when the call is made; treat *any* backend
    error as a skip, not a fail, so the recall number is re-measured when the
    backend recovers. The first failure latches so sibling cases skip without
    re-spawning the backend. (The driver wraps backend faults as RuntimeError;
    ``except Exception`` is the safety net for anything that leaks through.)
    """
    global _BACKEND_BROKEN_REASON
    if _BACKEND_BROKEN_REASON is not None:
        pytest.skip(
            f"LLM backend ({ev.backend_label()}) unusable, earlier call failed: "
            f"{_BACKEND_BROKEN_REASON}"
        )
    try:
        return ev.run_evaluator(evaluator_name, case_dir, expected)
    except Exception as e:  # noqa: BLE001 -- any backend fault is a skip, not a fail
        _BACKEND_BROKEN_REASON = str(e)
        pytest.skip(f"LLM backend ({ev.backend_label()}) unusable at runtime: {e}")


@pytest.mark.skipif(not ev.llm_available(), reason="no LLM backend (anthropic key / opencode+deepseek)")
class TestEvaluatorRecall:
    """Drive each evaluator over the fixture suite and assert the recall AC.

    Granularity note (SPEC §16.6 / §16.7): the fixture suite seeds one case per
    distinct check category (7 block-map categories, 5 search-space categories).
    At that granularity the ``>= 0.90`` bar for block-map requires every seeded
    case to be caught (N=7, so 6/7 = 0.857 < 0.90). That is intentional: with
    one case per check category, missing any single one is a real coverage
    regression, so the strict bar is the right quality gate. If the suite later
    grows multiple cases per category, the same 0.90 bar gains statistical
    headroom without changing the threshold. The search-space ``== 1.0`` bar is
    strict by design (schema violations are crisp, deterministic checks).
    """

    @pytest.mark.parametrize("case_id", _BLOCK_MAP_CASES)
    def test_block_map_flags_seeded_error(self, case_id):
        case_dir = CASES_ROOT / case_id
        expected = _load_expected(case_dir)
        verdict = _run_evaluator_or_skip("block-map-evaluator", case_dir, expected)
        assert _score(verdict, expected), (
            f"{case_id}: block-map-evaluator verdict did not match expected "
            f"{expected['expect']} {expected['must_mention_one_of']}; verdict=\n{verdict}"
        )

    @pytest.mark.parametrize("case_id", _SEARCH_SPACE_CASES)
    def test_search_space_flags_seeded_error(self, case_id):
        case_dir = CASES_ROOT / case_id
        expected = _load_expected(case_dir)
        verdict = _run_evaluator_or_skip("search-space-evaluator", case_dir, expected)
        assert _score(verdict, expected), (
            f"{case_id}: search-space-evaluator verdict did not match expected "
            f"{expected['expect']} {expected['must_mention_one_of']}; verdict=\n{verdict}"
        )

    def test_clean_baseline_lgtm_for_both(self):
        case_dir = CASES_ROOT / "08_clean_baseline"
        expected = _load_expected(case_dir)
        for evaluator in ("block-map-evaluator", "search-space-evaluator"):
            verdict = _run_evaluator_or_skip(evaluator, case_dir, expected)
            assert verdict.strip().upper().startswith("LGTM"), (
                f"clean baseline must LGTM for {evaluator}; verdict=\n{verdict}"
            )

    def test_block_map_recall_threshold(self):
        """SPEC §16.6: block-map-evaluator recall on seeded cases >= 0.90."""
        hits, total, misses = 0, 0, []
        for case_id in _BLOCK_MAP_CASES:
            case_dir = CASES_ROOT / case_id
            expected = _load_expected(case_dir)
            verdict = _run_evaluator_or_skip("block-map-evaluator", case_dir, expected)
            total += 1
            if _score(verdict, expected):
                hits += 1
            else:
                misses.append(case_id)
        recall = hits / total
        assert recall >= 0.90, (
            f"block-map recall {recall:.2f} < 0.90; misses={misses}"
        )

    def test_search_space_recall_full(self):
        """SPEC §16.7: search-space-evaluator schema-violation recall == 1.0."""
        hits, total, misses = 0, 0, []
        for case_id in _SEARCH_SPACE_CASES:
            case_dir = CASES_ROOT / case_id
            expected = _load_expected(case_dir)
            verdict = _run_evaluator_or_skip("search-space-evaluator", case_dir, expected)
            total += 1
            if _score(verdict, expected):
                hits += 1
            else:
                misses.append(case_id)
        recall = hits / total
        assert recall == 1.0, (
            f"search-space recall {recall:.2f} < 1.00; misses={misses}"
        )
