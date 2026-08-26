#!/usr/bin/env python3
"""check_search_space.py — deterministic search-space contract gate for ns3_expand_supernet.

Verifies the generated supernet search space is a proper "sandwich" around the
prepared model's baseline. For **every** searchable dimension — global/per-stage
**depth** and every searchable **internal-width** field — the candidate set must
satisfy all three checks:

    1. baseline present   — the prepared model's actual value is a candidate
    2. some candidate > baseline
    3. some candidate < baseline

so sandwich training can sample subnets both smaller and larger than the original.

Inputs:
  - ``supernet.py`` (exec'd to instantiate ``SearchSpace``)  -> candidates
  - ``.baseline.json`` (written by ns3_expand_supernet at generation time) -> baselines

Baseline marker schema (internal-width field names are model-type/block dependent;
the script is name-agnostic and checks whatever the expand node recorded):

  isotropic::
      {"depth": 4, "internal_dims": {"ffn_dim": 1024, "num_heads": 8}}

  staged (cnn / hierarchical transformer)::
      {"stage_depths": {"stage1": 2, "stage2": 3},
       "stage_internal_dims": {"stage1": {"ffn_dim": 256}, "stage2": {"ffn_dim": 512}}}

Fail loud: any violated check -> non-zero exit + one line per failure on stderr.
Deterministic: no LLM, no network, no clock, no random.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load_search_space(artifacts_dir: Path):
    """exec ``supernet.py`` and instantiate ``SearchSpace``.

    Mirrors check_expand.sh / search_space_table.py: compiles + execs the module,
    grabs ``SearchSpace`` (or ``build_supernet``). Returns ``(None, reason)`` on any
    failure so the caller fail-louds with a clear message instead of crashing.
    """
    supernet_path = artifacts_dir / "supernet.py"
    if not supernet_path.is_file():
        return None, "supernet.py missing"

    ns: dict[str, Any] = {}
    try:
        src = supernet_path.read_text(encoding="utf-8", errors="replace")
        exec(compile(src, str(supernet_path), "exec"), ns)  # noqa: S102 -- same as check_expand.sh gate
    except Exception as exc:  # noqa: BLE001 -- fail-loud: report and exit non-zero
        return None, f"exec supernet.py failed: {exc}"

    ss_cls = ns.get("SearchSpace")
    try:
        if ss_cls is not None:
            return ss_cls(), ""
        builder = ns.get("build_supernet")
        if builder is None:
            return None, "supernet.py exposes neither SearchSpace nor build_supernet"
        return builder(), ""
    except Exception as exc:  # noqa: BLE001 -- fail-loud
        return None, f"SearchSpace/build_supernet instantiation failed: {exc}"


def _load_baseline(artifacts_dir: Path):
    """Load the ``.baseline.json`` marker. Returns ``(None, reason)`` on failure."""
    path = artifacts_dir / ".baseline.json"
    if not path.is_file():
        return None, (
            ".baseline.json missing — the expand node must record the prepared "
            "model's baseline (depth + internal widths) before this gate runs"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        return None, f".baseline.json is not valid JSON: {exc}"
    if not isinstance(data, dict):
        return None, ".baseline.json must be a JSON object"
    return data, ""


def _to_numbers(candidates: Any) -> list[int | float]:
    """Coerce a candidate tuple/list into numeric values, dropping non-numerics."""
    out: list[int | float] = []
    if isinstance(candidates, (tuple, list)):
        for v in candidates:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(v)
    elif isinstance(candidates, (int, float)) and not isinstance(candidates, bool):
        out.append(candidates)
    return out


def _coerce_baseline(v: Any) -> int | float | None:
    """Coerce a marker baseline value to a number; ``None`` if not numeric."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v) if "." in v else int(v)
        except ValueError:
            return None
    return None


def _sandwich_errors(candidates: list[int | float], baseline: int | float, label: str) -> list[str]:
    """The three contract checks for one searchable dimension."""
    errs: list[str] = []
    cands = sorted(candidates)
    if baseline not in cands:
        errs.append(f"{label}: baseline {baseline} missing from candidates {tuple(cands)}")
    if not any(c > baseline for c in cands):
        errs.append(f"{label}: no candidate > baseline {baseline} (candidates={tuple(cands)})")
    if not any(c < baseline for c in cands):
        errs.append(f"{label}: no candidate < baseline {baseline} (candidates={tuple(cands)})")
    return errs


def _gather_field(block_choice_dict: Any, field: str) -> list[int | float]:
    """Union of candidate values for ``field`` across all block choices in one position.

    ``block_choice_dict`` is one ``{choice_name: {field: candidate_tuple}}`` dict
    (isotropic ``layer_configs``, or one stage's entry of ``stage_layer_configs``).
    """
    vals: list[int | float] = []
    if not isinstance(block_choice_dict, dict):
        return vals
    for cfg in block_choice_dict.values():
        if isinstance(cfg, dict) and field in cfg:
            vals.extend(_to_numbers(cfg[field]))
    return sorted(set(vals))


def _check_contract(ss: Any, baseline: dict) -> list[str]:
    """Run the depth + internal-width sandwich checks against the baseline marker.

    Pure over a SearchSpace-like object + baseline dict (testable without torch).
    Returns a list of failure strings (empty = pass).
    """
    errs: list[str] = []

    if hasattr(ss, "depth_candidates"):
        # ── isotropic ──────────────────────────────────────────────────────
        depth_cands = _to_numbers(getattr(ss, "depth_candidates"))
        b_depth = _coerce_baseline(baseline.get("depth"))
        if b_depth is None:
            errs.append("depth: baseline 'depth' missing or non-numeric in .baseline.json")
        else:
            errs += _sandwich_errors(depth_cands, b_depth, "depth")

        internal = baseline.get("internal_dims", {})
        if not isinstance(internal, dict):
            errs.append("internal_dims: must be a JSON object in .baseline.json")
            return errs
        layer_configs = getattr(ss, "layer_configs", None)
        for field, b_val in internal.items():
            b = _coerce_baseline(b_val)
            if b is None:
                errs.append(f"{field}: baseline non-numeric in .baseline.json internal_dims")
                continue
            cands = _gather_field(layer_configs, field)
            if not cands:
                errs.append(f"{field}: no candidates found in layer_configs (marker field absent from SearchSpace)")
            else:
                errs += _sandwich_errors(cands, b, field)

    elif hasattr(ss, "stage_depth_candidates"):
        # ── staged (cnn / hierarchical transformer) ────────────────────────
        depth_cands_by_stage = getattr(ss, "stage_depth_candidates", ())
        n = len(depth_cands_by_stage) if isinstance(depth_cands_by_stage, (tuple, list)) else 0
        stage_names = tuple(getattr(ss, "stage_names", ()))
        if len(stage_names) != n:
            stage_names = tuple(f"stage{i + 1}" for i in range(n))

        stage_depths_bl = baseline.get("stage_depths", {})
        if not isinstance(stage_depths_bl, dict):
            stage_depths_bl = {}
        stage_internal_bl = baseline.get("stage_internal_dims", {})
        if not isinstance(stage_internal_bl, dict):
            stage_internal_bl = {}
        layer_configs_by_stage = getattr(ss, "stage_layer_configs", ())

        for i, name in enumerate(stage_names):
            depth_cands = _to_numbers(depth_cands_by_stage[i]) if i < n else []
            b_depth = _coerce_baseline(stage_depths_bl.get(name))
            if b_depth is None:
                errs.append(f"depth[{name}]: baseline missing or non-numeric in .baseline.json stage_depths")
            else:
                errs += _sandwich_errors(depth_cands, b_depth, f"depth[{name}]")

            stage_cfg = layer_configs_by_stage[i] if i < len(layer_configs_by_stage) else {}
            dims = stage_internal_bl.get(name) or {}
            if not isinstance(dims, dict):
                errs.append(f"{name}.stage_internal_dims: must be a JSON object")
                continue
            for field, b_val in dims.items():
                b = _coerce_baseline(b_val)
                if b is None:
                    errs.append(f"{name}.{field}: baseline non-numeric in .baseline.json")
                    continue
                cands = _gather_field(stage_cfg, field)
                if not cands:
                    errs.append(f"{name}.{field}: no candidates found in stage_layer_configs")
                else:
                    errs += _sandwich_errors(cands, b, f"{name}.{field}")

    else:
        errs.append("SearchSpace exposes neither depth_candidates nor stage_depth_candidates — cannot check")

    return errs


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Search-space contract gate (depth + internal-width sandwich vs baseline)."
    )
    ap.add_argument("--artifacts-dir", required=True, help="$ORCA_ARTIFACTS_DIR")
    args = ap.parse_args()
    ad = Path(args.artifacts_dir)

    ss, err = _load_search_space(ad)
    if ss is None:
        print(f"FAIL: {err}", file=sys.stderr)
        return 1

    baseline, err2 = _load_baseline(ad)
    if baseline is None:
        print(f"FAIL: {err2}", file=sys.stderr)
        return 1

    errs = _check_contract(ss, baseline)
    if errs:
        for e in errs:
            print(f"FAIL: {e}", file=sys.stderr)
        print(f"FAIL: search-space contract violated ({len(errs)} issue(s))", file=sys.stderr)
        return 1

    print("PASS: search-space contract (depth + internal-width sandwich vs baseline)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
