"""build_evaluator_cases.py -- deterministic generator for the evaluator fixture suite.

Materializes every case directory under ``evaluator_cases/``:

    <case_id>/
        search_space.yaml   # the artifact under audit (with the seeded error)
        manifest.yaml       # project facts fed to the evaluator
        expected.yaml       # the verdict the correct evaluator must return

and a shared ``flats/`` tree of self-contained flat models.

The generator is a dev tool: it captures the *intent* of each seeded error in
one place. The committed materialized files are the real fixtures; the recall
test reads them directly (never re-runs this generator). Run after editing to
refresh the materialized tree:

    python3 tests/e2e_puzzle/fixtures/build_evaluator_cases.py

Each case targets exactly one evaluator (``primary_evaluator``) with one
expected verdict. The recall test scores only that evaluator against that
verdict, so the fixture->evaluator mapping stays unambiguous.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent / "evaluator_cases"

# ── canonical clean slot declarations (a 2-slot block: attention + ffn) ───────

CLEAN_ATTN_SLOT: dict[str, Any] = {
    "id": "L0_attn",
    "path": "blocks.0.attn",
    "kind": "attention",
    "layer_idx": 0,
    "source_class": "TinyAttention",
    "forward_arity": "single",
    "return_arity": "single",
    "mask_load_bearing": False,
    "num_heads": 4,
    "head_dim": 8,
    "in_dim": -1,
    "out_dim": -1,
    "kind_evidence": "forward: matmul(Q, K^T) * scale + softmax over scores",
}

CLEAN_FFN_SLOT: dict[str, Any] = {
    "id": "L0_ffn",
    "path": "blocks.0.ffn",
    "kind": "ffn",
    "layer_idx": 0,
    "source_class": "FeedForward",
    "forward_arity": "single",
    "return_arity": "single",
    "original_intermediate": 64,
    "activation": "gelu",
    "ffn_struct": "standard",
    "in_dim": -1,
    "out_dim": -1,
    "kind_evidence": "forward: Linear(fc1) -> GELU -> Linear(fc2)",
}

CLEAN_CANDIDATES: dict[str, list[str]] = {
    "attention": ["identity", "vanilla", "fnet"],
    "ffn": ["identity", "ffn_50"],
}

CLEAN_MANIFEST: dict[str, Any] = {
    "project_overview": {
        "task_type": "image classification",
        "purpose": "toy classification baseline",
    },
    "model": {
        "location": "model.py",
        "build_entry": "build_model",
        "forward_signature": "forward(self, x)",
        "inputs": "[2,8,32]",
        "outputs": "[2,10]",
    },
    "training_and_evaluation": {
        "paradigm": "cross-entropy classification",
        "loss": "CrossEntropyLoss",
        "metric": {"name": "accuracy", "direction": "higher-better"},
        "eval_kind": "classification",
        "evaluation_entry": "train.py::eval_model",
    },
    "data_and_environment": {"dataset": "toy"},
    "relevant_source_files": [],
}


def _clone(d: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in d.items():
        out[k] = v.copy() if isinstance(v, (dict, list)) else v
    return out


def _write_yaml(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _write_case(
    case_id: str,
    *,
    primary_evaluator: str,
    flat_relpath: str,
    expect: str,
    expected_severity: str | None,
    must_mention_one_of: list[str],
    description: str,
    slots: list[dict[str, Any]] | None = None,
    candidates: dict[str, list[str]] | None = None,
    manifest: dict[str, Any] | None = None,
) -> None:
    case_dir = ROOT / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    if slots is not None and candidates is not None:
        _write_yaml(case_dir / "search_space.yaml", {"slots": slots, "candidates": candidates})
    _write_yaml(
        case_dir / "manifest.yaml",
        manifest if manifest is not None else _clone(CLEAN_MANIFEST),
    )
    _write_yaml(
        case_dir / "expected.yaml",
        {
            "case_id": case_id,
            "primary_evaluator": primary_evaluator,
            "flat_relpath": flat_relpath,
            "expect": expect,
            "expected_severity": expected_severity,
            "must_mention_one_of": must_mention_one_of,
            "description": description,
        },
    )


# ── block-map-evaluator cases (semantic / structural slot audit) ──────────────


def case_01_conv_as_attention() -> None:
    """attention kind label but the slot module is a Conv1d mixer (no QK^T)."""
    slots = [_clone(CLEAN_ATTN_SLOT), _clone(CLEAN_FFN_SLOT)]
    slots[0]["source_class"] = "ConvMixer"
    _write_case(
        "01_conv_as_attention",
        primary_evaluator="block-map-evaluator",
        flat_relpath="flats/conv_attn_flat.py",
        expect="FLAG",
        expected_severity="MAJOR",
        must_mention_one_of=["conv", "attention", "qk", "kind", "softmax", "scaling"],
        description="slot L0_attn declared attention but the module source is a Conv1d mixer",
        slots=slots,
        candidates=_clone(CLEAN_CANDIDATES),
    )


def case_02_wrong_path() -> None:
    """slot path does not resolve to any submodule of the flat model."""
    slots = [_clone(CLEAN_ATTN_SLOT), _clone(CLEAN_FFN_SLOT)]
    slots[0]["path"] = "blocks.0.nonexistent"
    _write_case(
        "02_wrong_path",
        primary_evaluator="block-map-evaluator",
        flat_relpath="flats/clean_flat.py",
        expect="FLAG",
        expected_severity="BLOCKER",
        must_mention_one_of=["path", "nonexistent", "resolve", "submodule", "not found"],
        description="slot path blocks.0.nonexistent does not resolve in the flat model",
        slots=slots,
        candidates=_clone(CLEAN_CANDIDATES),
    )


def case_03_shape_mismatch() -> None:
    """declared in_dim/out_dim contradict the real module (dim=32)."""
    slots = [_clone(CLEAN_ATTN_SLOT), _clone(CLEAN_FFN_SLOT)]
    slots[0]["in_dim"] = 64
    slots[0]["out_dim"] = 64
    _write_case(
        "03_shape_mismatch",
        primary_evaluator="block-map-evaluator",
        flat_relpath="flats/clean_flat.py",
        expect="FLAG",
        expected_severity="BLOCKER",
        must_mention_one_of=["dim", "shape", "mismatch", "32", "64"],
        description="slot L0_attn declares in_dim/out_dim=64 but the real module is dim=32",
        slots=slots,
        candidates=_clone(CLEAN_CANDIDATES),
    )


def case_04_identity_missing() -> None:
    """attention candidate list omits identity."""
    candidates = _clone(CLEAN_CANDIDATES)
    candidates["attention"] = ["vanilla", "fnet"]
    _write_case(
        "04_identity_missing",
        primary_evaluator="block-map-evaluator",
        flat_relpath="flats/clean_flat.py",
        expect="FLAG",
        expected_severity="BLOCKER",
        must_mention_one_of=["identity", "candidate"],
        description="candidates.attention omits identity",
        slots=[_clone(CLEAN_ATTN_SLOT), _clone(CLEAN_FFN_SLOT)],
        candidates=candidates,
    )


def case_06_mask_blind_on_mask_slot() -> None:
    """mask_load_bearing slot offered a mask-blind candidate (fnet)."""
    slots = [_clone(CLEAN_ATTN_SLOT), _clone(CLEAN_FFN_SLOT)]
    slots[0]["mask_load_bearing"] = True
    _write_case(
        "06_mask_blind_on_mask_slot",
        primary_evaluator="block-map-evaluator",
        flat_relpath="flats/clean_flat.py",
        expect="FLAG",
        expected_severity="MAJOR",
        must_mention_one_of=["mask", "fnet", "blind", "mask_aware"],
        description="mask_load_bearing=true slot offers fnet (mask_aware=false)",
        slots=slots,
        candidates=_clone(CLEAN_CANDIDATES),
    )


def case_07_return_arity_violation() -> None:
    """return_arity=multi slot offered single-output candidates."""
    slots = [_clone(CLEAN_ATTN_SLOT), _clone(CLEAN_FFN_SLOT)]
    slots[0]["return_arity"] = "multi"
    slots[0]["source_class"] = "MultiReturnAttention"
    _write_case(
        "07_return_arity_violation",
        primary_evaluator="block-map-evaluator",
        flat_relpath="flats/multi_return_flat.py",
        expect="FLAG",
        expected_severity="BLOCKER",
        must_mention_one_of=["return_arity", "multi", "single", "arity", "output"],
        description="return_arity=multi slot offered single-output candidates",
        slots=slots,
        candidates=_clone(CLEAN_CANDIDATES),
    )


def case_12_candidate_wrong_kind() -> None:
    """an ffn-only candidate (ffn_50) listed under the attention kind key --
    structurally legal yaml, but the candidate does not apply to the slot kind."""
    candidates = _clone(CLEAN_CANDIDATES)
    # ffn_50's catalog kind list is [ffn]; offering it at attention is a
    # candidate-applicability mismatch the block-map-evaluator must flag.
    candidates["attention"] = ["identity", "vanilla", "ffn_50"]
    _write_case(
        "12_candidate_wrong_kind",
        primary_evaluator="block-map-evaluator",
        flat_relpath="flats/clean_flat.py",
        expect="FLAG",
        expected_severity="MAJOR",
        must_mention_one_of=["ffn_50", "candidate", "applic", "kind", "attention"],
        description="candidates.attention lists ffn_50 (an ffn-only candidate)",
        slots=[_clone(CLEAN_ATTN_SLOT), _clone(CLEAN_FFN_SLOT)],
        candidates=candidates,
    )


# ── search-space-evaluator cases (schema / contract compliance) ───────────────


def case_05_eval_kind_mislabel() -> None:
    """manifest self-contradicts: eval_kind=classification but every other
    paradigm signal (loss, metric, output shape) describes an embedding model."""
    manifest = _clone(CLEAN_MANIFEST)
    te = manifest["training_and_evaluation"]
    # leave eval_kind at the wrong value "classification"; the surrounding
    # signals describe metric learning / embedding -- a deterministic
    # contradiction the search-space-evaluator must catch from the manifest.
    te["paradigm"] = "InfoNCE metric learning"
    te["loss"] = "InfoNCELoss(temperature=0.07)"
    te["metric"] = {"name": "k-NN accuracy", "direction": "higher-better"}
    manifest["model"]["outputs"] = "[2,16]"
    manifest["model"]["forward_signature"] = "forward(self, x) -> hidden vector"
    _write_case(
        "05_eval_kind_mislabel",
        primary_evaluator="search-space-evaluator",
        flat_relpath="flats/embedding_flat.py",
        expect="FLAG",
        expected_severity="MAJOR",
        must_mention_one_of=["eval_kind", "classification", "embedding", "logits", "hidden", "metric", "k-nn", "paradigm"],
        description="manifest eval_kind=classification but loss=InfoNCE, metric=k-NN accuracy, output=[2,16] hidden -- embedding paradigm",
        slots=[_clone(CLEAN_ATTN_SLOT), _clone(CLEAN_FFN_SLOT)],
        candidates=_clone(CLEAN_CANDIDATES),
        manifest=manifest,
    )


def case_09_duplicate_id() -> None:
    """two slots share the same id."""
    a = _clone(CLEAN_ATTN_SLOT)
    b = _clone(CLEAN_FFN_SLOT)
    b["id"] = "L0_attn"  # duplicate of a
    _write_case(
        "09_duplicate_id",
        primary_evaluator="search-space-evaluator",
        flat_relpath="flats/clean_flat.py",
        expect="FLAG",
        expected_severity="BLOCKER",
        must_mention_one_of=["duplicate", "id", "unique"],
        description="two slots share id L0_attn",
        slots=[a, b],
        candidates=_clone(CLEAN_CANDIDATES),
    )


def case_10_unknown_candidate() -> None:
    """candidate name not registered in the catalog."""
    candidates = _clone(CLEAN_CANDIDATES)
    candidates["attention"] = ["identity", "bogus_block"]
    _write_case(
        "10_unknown_candidate",
        primary_evaluator="search-space-evaluator",
        flat_relpath="flats/clean_flat.py",
        expect="FLAG",
        expected_severity="BLOCKER",
        must_mention_one_of=["bogus", "candidate", "register", "catalog", "unknown"],
        description="candidates.attention references bogus_block which is not in the catalog",
        slots=[_clone(CLEAN_ATTN_SLOT), _clone(CLEAN_FFN_SLOT)],
        candidates=candidates,
    )


def case_11_axes_residual() -> None:
    """deprecated axes field still present on a slot."""
    a = _clone(CLEAN_ATTN_SLOT)
    a["axes"] = ["depth"]
    _write_case(
        "11_axes_residual",
        primary_evaluator="search-space-evaluator",
        flat_relpath="flats/clean_flat.py",
        expect="FLAG",
        expected_severity="MINOR",
        must_mention_one_of=["axes", "deprecated", "removed"],
        description="slot carries the removed axes field",
        slots=[a, _clone(CLEAN_FFN_SLOT)],
        candidates=_clone(CLEAN_CANDIDATES),
    )


# ── clean baseline (both evaluators LGTM) ─────────────────────────────────────


def case_08_clean_baseline() -> None:
    _write_case(
        "08_clean_baseline",
        primary_evaluator="both",
        flat_relpath="flats/clean_flat.py",
        expect="LGTM",
        expected_severity=None,
        must_mention_one_of=[],
        description="canonical clean search_space + manifest; both evaluators must LGTM",
        slots=[_clone(CLEAN_ATTN_SLOT), _clone(CLEAN_FFN_SLOT)],
        candidates=_clone(CLEAN_CANDIDATES),
    )


CASES = [
    case_01_conv_as_attention,
    case_02_wrong_path,
    case_03_shape_mismatch,
    case_04_identity_missing,
    case_05_eval_kind_mislabel,
    case_06_mask_blind_on_mask_slot,
    case_07_return_arity_violation,
    case_12_candidate_wrong_kind,
    case_08_clean_baseline,
    case_09_duplicate_id,
    case_10_unknown_candidate,
    case_11_axes_residual,
]


def main() -> None:
    for fn in CASES:
        fn()
    print(f"materialized {len(CASES)} cases under {ROOT}")


if __name__ == "__main__":
    main()
