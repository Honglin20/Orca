#!/usr/bin/env python3
"""check_search_space.py — deterministic search-space contract gate for pz_search_space.

Layer-granularity version. Verifies the declared ``search_space.yaml`` is a
well-formed contract for downstream pz_baseline:

  1. YAML parses; ``slots`` is a list; every slot carries the required fields
     (``id`` / ``path`` / ``kind`` / ``layer_idx``); ``id`` and ``path`` are
     unique; ``kind`` is legal (delegated to ``search_space_io.load_search_space_yaml``).
  2. Candidate block well-formed: every referenced candidate name is either
     ``identity`` or registered in the candidate catalog with ``transformer_layer``
     in its ``kind`` list (delegated to ``search_space_io`` via ``parse_block_candidates``).
  3. ``identity`` mandatory per kind candidate list (delegated).
  4. Every ``transformer_layer`` slot carries the layer-specific fields named in
     ``transformer_layer_pattern.json#must_extract`` (``num_heads`` / ``head_dim``
     / ``original_intermediate`` / ``activation`` / ``norm_type`` / ``max_seq_len``
     / ``mask_load_bearing``). ``in_dim`` / ``out_dim`` placeholders must be ``-1``
     ( pz_baseline traces real values later); ``max_seq_len`` placeholder ``-1``
     required (no hardcoded fallback).

Empty ``slots: []`` is a **valid** declaration (``model_type_supported=false`` →
``terminate_unsupported``); the gate does not fail on it. The gate fails only on
structural / schema violations.

Inputs:
  - ``$ORCA_ARTIFACTS_DIR/search_space.yaml``
  - ``$ORCA_WORKFLOWS_ROOT/agents/_puzzle_scripts/`` (for puzzle_common / search_space_io / catalog)
  - ``$ORCA_AGENT_RESOURCES/references/transformer_layer_pattern.json`` (must_extract source)

Fail loud: any violated check -> non-zero exit + one line per failure on stderr.
Deterministic: no LLM, no network, no clock, no random.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _setup_path(scripts_dir: Path) -> None:
    """Prepend ``_puzzle_scripts`` to sys.path so search_space_io / puzzle_common import."""
    p = str(scripts_dir.resolve())
    if p not in sys.path:
        sys.path.insert(0, p)


def _load_pattern(agent_resources: Path) -> dict[str, Any]:
    """Load transformer_layer_pattern.json from the agent's references dir."""
    p = agent_resources / "references" / "transformer_layer_pattern.json"
    if not p.is_file():
        # Fallback: caller may pass the file directly via env
        raise FileNotFoundError(
            f"transformer_layer_pattern.json missing at {p} — the agent resource "
            "directory must contain references/transformer_layer_pattern.json"
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"transformer_layer_pattern.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or "must_extract" not in data:
        raise RuntimeError("transformer_layer_pattern.json missing 'must_extract' field")
    return data


def _check_layer_fields(
    slot: dict[str, Any],
    must_extract: list[str],
    idx: int,
) -> list[str]:
    """Verify a transformer_layer slot carries all must_extract fields with valid placeholders."""
    errs: list[str] = []
    slot_id = slot.get("id", f"<idx={idx}>")

    # kind must be transformer_layer for the layer-specific check to apply
    if str(slot.get("kind", "")) != "transformer_layer":
        return errs  # other kinds are not this gate's concern (legacy block kinds)

    for field in must_extract:
        if field not in slot:
            errs.append(
                f"slot {slot_id!r}: missing must_extract field {field!r} "
                f"(declared in transformer_layer_pattern.json)"
            )

    # max_seq_len placeholder rule: must be -1 (no hardcoded fallback)
    msq = slot.get("max_seq_len")
    if msq is not None and msq != -1 and not (isinstance(msq, int) and msq < 0):
        errs.append(
            f"slot {slot_id!r}: max_seq_len must be -1 (placeholder for pz_baseline runtime "
            f"trace); got {msq!r}. Hardcoded fallback over-parameterizes the mixing matrix "
            f"for short-sequence projects (e.g. seq=16 with max_seq_len=512 allocates a "
            f"2.6M-parameter mixer where the real one needs ~16 entries)."
        )

    # mask_load_bearing placeholder rule: declared false (pz_baseline runtime-traces the real value)
    mlb = slot.get("mask_load_bearing")
    if mlb is not None and mlb is not False:
        errs.append(
            f"slot {slot_id!r}: mask_load_bearing must be declared false (placeholder for "
            f"pz_baseline runtime trace of src_mask actual value); got {mlb!r}. Signature "
            f"presence alone is not load-bearing (design L13)."
        )

    # in_dim / out_dim placeholders: -1 (pz_baseline trace-backfills real values)
    for dim_field in ("in_dim", "out_dim"):
        v = slot.get(dim_field)
        if v is not None and v != -1 and not (isinstance(v, int) and v < 0):
            errs.append(
                f"slot {slot_id!r}: {dim_field} must be -1 (placeholder for pz_baseline "
                f"trace-backfill); got {v!r}."
            )

    # layer_evidence: present + non-empty + structural (not a class name)
    ev = slot.get("layer_evidence") or slot.get("kind_evidence")
    if ev is None:
        errs.append(
            f"slot {slot_id!r}: missing layer_evidence (structural forward facts from "
            f"transformer_layer_pattern.json#evidence_template)"
        )
    elif not isinstance(ev, str) or not ev.strip():
        errs.append(f"slot {slot_id!r}: layer_evidence is empty")
    # Note: full structural-fact vs class-name distinction is the
    # transformer-layer-evaluator's job (LLM judgment); this gate only checks
    # presence + non-empty.

    return errs


def _check_contract(
    slot_dicts: list[dict[str, Any]],
    candidates: dict[str, list[str]],
    must_extract: list[str],
) -> list[str]:
    """Run the layer-specific field checks across all slots. Empty slots → no errs."""
    errs: list[str] = []
    for i, slot in enumerate(slot_dicts):
        errs.extend(_check_layer_fields(slot, must_extract, i))

    # If slots non-empty: candidates must include transformer_layer (the only layer kind)
    if slot_dicts:
        if "transformer_layer" not in candidates:
            errs.append(
                "candidates block missing 'transformer_layer' key — required when "
                "transformer_layer slots are declared"
            )
    return errs


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Layer-granularity search-space contract gate for pz_search_space."
    )
    ap.add_argument("--artifacts-dir", required=True, help="$ORCA_ARTIFACTS_DIR")
    ap.add_argument(
        "--scripts-dir",
        required=True,
        help="$ORCA_WORKFLOWS_ROOT/agents/_puzzle_scripts (for puzzle_common / search_space_io)",
    )
    ap.add_argument(
        "--agent-resources",
        required=True,
        help="$ORCA_AGENT_RESOURCES (references/transformer_layer_pattern.json lives here)",
    )
    args = ap.parse_args()

    ad = Path(args.artifacts_dir)
    sd = Path(args.scripts_dir)
    ar = Path(args.agent_resources)

    ss_path = ad / "search_space.yaml"
    if not ss_path.is_file():
        print(f"FAIL: search_space.yaml missing at {ss_path}", file=sys.stderr)
        return 1

    _setup_path(sd)

    # load_search_space_yaml covers: required slot fields, legal kind, unique id/path,
    # candidates block shape, catalog registration (parse_block_candidates), identity per kind.
    try:
        from search_space_io import load_search_space_yaml
    except ImportError as exc:
        print(f"FAIL: cannot import search_space_io from {sd}: {exc}", file=sys.stderr)
        return 1

    try:
        slot_dicts, candidates = load_search_space_yaml(ss_path)
    except Exception as exc:  # noqa: BLE001 — fail-loud: report and exit non-zero
        print(f"FAIL: search_space.yaml invalid: {exc}", file=sys.stderr)
        return 1

    # Load pattern.json for must_extract (cwd-independent via $ORCA_AGENT_RESOURCES)
    try:
        pattern = _load_pattern(ar)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    must_extract = list(pattern.get("must_extract", []))
    if not must_extract:
        print("FAIL: transformer_layer_pattern.json#must_extract empty", file=sys.stderr)
        return 1

    errs = _check_contract(slot_dicts, candidates, must_extract)
    if errs:
        for e in errs:
            print(f"FAIL: {e}", file=sys.stderr)
        print(
            f"FAIL: search-space contract violated ({len(errs)} issue(s))",
            file=sys.stderr,
        )
        return 1

    slot_count = len(slot_dicts)
    print(
        f"PASS: search-space contract (slots={slot_count}, "
        f"transformer_layer candidates={len(candidates.get('transformer_layer', []))})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
