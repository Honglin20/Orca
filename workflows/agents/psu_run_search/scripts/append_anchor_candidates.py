#!/usr/bin/env python3
"""append_anchor_candidates.py -- Idempotently append anchor candidates after the search.

Runs after ``.search_rc==0`` and BEFORE select (also on the REUSE path -- select's
precondition). Guarantees the all-original baseline is in the candidate set regardless
of what the random initial population sampled, and gives the Pareto chart a baseline
anchor.

Anchor set (L+1 candidates, L = number of transformer layer slots):
  1. all-original -- every slot on its frozen ``original`` branch (the pretrained-model
     equivalent inside the supernet; the guaranteed floor candidate);
  2. per-slot single swaps -- slot i swapped to the first non-``original`` branch of its
     branch set (D5 enumeration order), for each i in 0..L-1.

Evaluation uses the SAME ``evaluator.py`` + ``latency_estimator.py`` the search used
(same supernet ckpt, same val data, same latency measurement path -- no proxies).
Records are appended to ``search_results.jsonl`` in the search's own record shape
(``{"objs": {...}, "pareto": ..., "arch": {...}}``) with an extra ``"anchor": true``
observability marker, keyed idempotently by the canonical arch dict -- anchors already
present are skipped, never duplicated, so repeated calls are no-ops.

Deterministic: fixed candidate order (all-original first, then slot 0..L-1), no clock,
no randomness.

Fail loud: any structural mismatch (no records / no arch field / no SearchSpace / no
evaluator symbols / ArchConfig construction failure) prints a clear stderr message and
exits 1 -- the caller records the miss in its assessment instead of silently losing the
anchor guarantee.

Arch layout discovery is record-driven (the appended records must match what
select_architecture.py / the chart scripts already parse):
  - the per-slot choice field is the unique arch field whose value is a sequence of
    per-slot entries, each either a branch-name string or a ``{"choice": <name>, ...}``
    dict;
  - the per-slot branch sets (for finding ``original`` + the first non-original swap)
    come from ``supernet.py``'s SearchSpace public choice containers; the D5
    enumeration makes the first non-original branch ``vanilla``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

ORIGINAL_BRANCH = "original"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Idempotently append all-original + per-slot-swap anchor candidates "
        "to search_results.jsonl using the search's own evaluator + latency estimator."
    )
    ap.add_argument(
        "--artifacts-dir",
        default=None,
        help="$ORCA_ARTIFACTS_DIR (default: $ORCA_ARTIFACTS_DIR env)",
    )
    args = ap.parse_args()
    ad = Path(args.artifacts_dir or os.environ["ORCA_ARTIFACTS_DIR"])

    try:
        appended, skipped, total = _run(ad)
    except _AnchorError as exc:
        sys.stderr.write(f"FAIL [append_anchor_candidates] {exc}\n")
        return 1

    print(
        f"[append_anchor_candidates] appended={appended} skipped_existing={skipped} "
        f"records_total={total} (all-original + per-slot first-non-original swaps, "
        f"evaluated via the search's evaluator.py + latency_estimator.py)"
    )
    return 0


def _run(ad: Path) -> tuple[int, int, int]:
    """Append the anchor candidates; return (appended, skipped_existing, total_records)."""
    # The generated modules import their siblings plainly (``from supernet import ...``,
    # ``from data_utils import ...``) — put the artifacts dir on sys.path so those
    # resolve exactly as they did inside the search runner.
    ad_str = str(ad.resolve())
    if ad_str not in sys.path:
        sys.path.insert(0, ad_str)
    # Anchors must resolve relative ckpt/data paths exactly as the search runner did:
    # pin cwd to the artifacts dir (agent bash calls run from arbitrary cwds).
    os.chdir(ad)

    results_path = ad / "search_results.jsonl"
    records = _read_jsonl(results_path)
    if not records:
        raise _AnchorError(f"no records in {results_path} -- cannot derive the arch layout")

    field_name, slot_entries = _find_choice_field(records[0].get("arch"))
    num_slots = len(slot_entries)

    # Per-slot branch sets from the SearchSpace (authoritative); fall back to the D5
    # constant order when discovery is impossible.
    search_space, branch_lists = _load_branch_lists(ad, num_slots)

    anchors = _build_anchor_archs(records[0]["arch"], field_name, slot_entries, branch_lists)
    if not anchors:
        raise _AnchorError("no anchor candidates could be constructed (no slot has a non-original branch?)")

    existing_keys = {_arch_key(rec.get("arch")) for rec in records}
    pending = [arch for arch in anchors if _arch_key(arch) not in existing_keys]
    skipped = len(anchors) - len(pending)

    if pending:
        quality_and_latency = _evaluate(ad, search_space, pending)
        # Canonical six-key record shape (identical to the search logger): a generated
        # select_architecture.py may read rec["gene"] / rec["generation"] -- any
        # extra or missing key is a contract break. Anchor provenance goes to the
        # .anchor_appended.json sidecar, never into the record itself.
        base_gen = max(
            (r.get("generation", 0) for r in records if isinstance(r.get("generation"), int)),
            default=0,
        )
        appended_keys: list[str] = []
        with results_path.open("a", encoding="utf-8") as fh:
            for offset, (arch, objs) in enumerate(quality_and_latency):
                row = {
                    "generation": base_gen + 1 + offset,
                    "gene": _anchor_gene(arch, field_name, branch_lists),
                    "objs": objs,
                    "cached": False,
                    "pareto": False,
                    "arch": arch,
                }
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                appended_keys.append(_arch_key(arch))
        (ad / ".anchor_appended.json").write_text(
            json.dumps({"appended": appended_keys}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    return len(pending), skipped, len(records) + len(pending)


# ── arch layout discovery (record-driven) ──────────────────────────────────────


def _find_choice_field(arch: Any) -> tuple[str, list[Any]]:
    """Find the per-slot choice field inside one record's ``arch`` dict.

    Returns ``(field_name, slot_entries)`` where each entry is a branch-name string or
    a ``{"choice": <name>, ...}`` dict. Raises on missing / ambiguous / unsupported
    layouts -- the record shape is the schema the search itself produced, so any
    mismatch is a contract break that must fail loud.
    """
    if not isinstance(arch, dict) or not arch:
        raise _AnchorError(f"record has no arch dict (got {type(arch).__name__}) -- cannot build anchors")
    candidates: list[tuple[str, list[Any]]] = []
    for name, val in arch.items():
        if isinstance(val, (list, tuple)) and val:
            if all(isinstance(e, str) for e in val):
                candidates.append((name, list(val)))
            elif all(isinstance(e, dict) and "choice" in e for e in val):
                candidates.append((name, list(val)))
    if not candidates:
        raise _AnchorError(f"arch dict has no per-slot choice field (keys={sorted(arch)})")
    if len(candidates) > 1:
        raise _AnchorError(
            f"arch dict has multiple per-slot choice fields ({[c[0] for c in candidates]}) -- ambiguous layout"
        )
    return candidates[0]


def _replace_entry(entry: Any, branch: str) -> Any:
    """A new slot entry with the branch swapped (preserves any other per-slot keys)."""
    if isinstance(entry, str):
        return branch
    new_entry = dict(entry)
    new_entry["choice"] = branch
    return new_entry


def _build_anchor_archs(
    template_arch: dict,
    field_name: str,
    slot_entries: list[Any],
    branch_lists: list[list[str]] | None,
) -> list[dict]:
    """Build the L+1 anchor arch dicts mirroring the record arch layout.

    ``branch_lists`` may be None (SearchSpace discovery failed) -- then every swap uses
    the D5 fallback (``vanilla``, the first non-original in the enumeration order).
    """
    # All-original anchor (the base every single swap is applied to).
    if branch_lists is not None:
        for i, branches in enumerate(branch_lists):
            if ORIGINAL_BRANCH not in branches:
                raise _AnchorError(
                    f"slot {i} branch set {branches} lacks '{ORIGINAL_BRANCH}' (PSU contract: every slot must contain it)"
                )
    all_original = [_replace_entry(e, ORIGINAL_BRANCH) for e in slot_entries]
    anchors: list[dict] = [_with_choices(template_arch, field_name, all_original)]
    # Per-slot single swaps: all-original base with exactly slot i swapped to the first
    # non-original branch of its set (slot 0..L-1; skip slots with no non-original branch).
    for i in range(len(slot_entries)):
        swap = _swap_branch(branch_lists[i] if branch_lists else None)
        if swap is None:
            continue
        new_entries = list(all_original)
        new_entries[i] = _replace_entry(all_original[i], swap)
        anchors.append(_with_choices(template_arch, field_name, new_entries))
    return anchors


def _swap_branch(branches: list[str] | None) -> str | None:
    """The swap branch for one slot: the first non-original branch of its set.

    D5 enumeration order makes this ``vanilla`` for the uniform branch set; falls back
    to the literal ``vanilla`` when the slot's set is unknown. Returns None when the
    slot offers no non-original branch.
    """
    if branches is None:
        return "vanilla"  # D5: first non-original in the enumeration order
    for b in branches:
        if b != ORIGINAL_BRANCH:
            return b
    return None


def _with_choices(template_arch: dict, field_name: str, entries: list[Any]) -> dict:
    """A new arch dict = template with the choice field replaced (other fields kept)."""
    arch = dict(template_arch)
    arch[field_name] = entries
    return arch


def _arch_key(arch: Any) -> str:
    """Canonical idempotency key of an arch value (stable across list/tuple)."""
    def _norm(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: _norm(x) for k, x in sorted(v.items())}
        if isinstance(v, (list, tuple)):
            return [_norm(x) for x in v]
        return v

    return json.dumps(_norm(arch), sort_keys=True, ensure_ascii=False)


_D5_ORDER = ("original", "vanilla", "random_synthesizer", "relu_attention", "fnet", "softs_star")


def _anchor_gene(arch: dict, field_name: str, branch_lists: list[list[str]] | None) -> list[int]:
    """Per-slot branch indices of one anchor arch (the encoding the search codec used)."""
    gene: list[int] = []
    for i, entry in enumerate(arch[field_name]):
        name = entry if isinstance(entry, str) else entry["choice"]
        branches = branch_lists[i] if branch_lists else list(_D5_ORDER)
        if name not in branches:
            raise _AnchorError(f"branch {name!r} not in slot {i} branch set {branches}")
        gene.append(branches.index(name))
    return gene


# ── SearchSpace branch-set discovery ───────────────────────────────────────────


def _load_branch_lists(ad: Path, num_slots: int) -> tuple[Any, list[list[str]] | None]:
    """Load ``supernet.py`` and extract the per-slot branch name lists.

    Returns ``(search_space, branch_lists)``; ``branch_lists`` is None when discovery
    is impossible (the caller falls back to the D5 constant swap). Handles both PSU
    SearchSpace shapes: a per-slot container (sequence of branch-name sequences) and a
    flat shared branch set plus a scalar slot count.
    """
    supernet_path = ad / "supernet.py"
    if not supernet_path.is_file():
        raise _AnchorError(f"supernet.py not found at {supernet_path}")
    # Canonical module name: the generated evaluator / latency_estimator do a plain
    # ``from supernet import ...`` and must hit this exact registered instance.
    mod = _load_sibling(supernet_path, "supernet")
    SearchSpace = getattr(mod, "SearchSpace", None)
    if SearchSpace is None:
        raise _AnchorError("supernet.py missing the SearchSpace symbol")
    search_space = SearchSpace()

    branch_lists: list[list[str]] | None = None
    for name in sorted(vars(search_space)):
        if name.startswith("_"):
            continue
        val = getattr(search_space, name)
        if not isinstance(val, (list, tuple)) or not val:
            continue
        if all(
            isinstance(x, (list, tuple)) and x and all(isinstance(b, str) for b in x)
            for x in val
        ):
            branch_lists = [list(x) for x in val]  # per-slot containers
            break
        if all(isinstance(b, str) for b in val):
            branch_lists = [list(val) for _ in range(num_slots)]  # flat shared set
            break
    return search_space, branch_lists


# ── evaluation via the search's own evaluator + latency estimator ──────────────


def _evaluate(ad: Path, search_space: Any, arch_dicts: list[dict]) -> list[tuple[dict, dict[str, float]]]:
    """Evaluate each anchor arch with the sibling evaluator + latency estimator.

    Returns ``[(arch, objs)]`` where ``objs`` mirrors the search record objectives
    (quality metrics from the evaluator, smaller-is-better; latency from the same
    LatencyEstimator the search used).
    """
    evaluator_path = ad / "evaluator.py"
    estimator_path = ad / "latency_estimator.py"
    if not evaluator_path.is_file():
        raise _AnchorError(f"evaluator.py not found at {evaluator_path}")
    if not estimator_path.is_file():
        raise _AnchorError(f"latency_estimator.py not found at {estimator_path}")

    evaluator_mod = _load_sibling(evaluator_path, "evaluator")
    estimator_mod = _load_sibling(estimator_path, "latency_estimator")
    CandidateEvaluator = getattr(evaluator_mod, "CandidateEvaluator", None)
    LatencyEstimator = getattr(estimator_mod, "LatencyEstimator", None)
    ArchConfig = getattr(_sys_modules_get("supernet"), "ArchConfig", None)
    if CandidateEvaluator is None or LatencyEstimator is None or ArchConfig is None:
        raise _AnchorError(
            "evaluator.py / latency_estimator.py / supernet.py missing required symbols "
            "(CandidateEvaluator, LatencyEstimator, ArchConfig)"
        )

    cfg = _load_search_config(ad)
    evaluator_cfg = _wrap_cfg(cfg.get("evaluator_cfg") if cfg else None)
    latency_cfg = _wrap_cfg(cfg.get("latency_cfg") if cfg else None)

    try:
        evaluator = CandidateEvaluator(device=_resolve_device(), evaluator_cfg=evaluator_cfg)
        try:
            estimator = LatencyEstimator(search_space, latency_cfg, device=_resolve_device())
        except TypeError:
            # generated estimators without a device kwarg keep their own default
            estimator = LatencyEstimator(search_space, latency_cfg)
    except Exception as exc:  # noqa: BLE001 -- construction details are project-specific
        raise _AnchorError(f"evaluator/estimator construction failed: {exc}") from exc

    out: list[tuple[dict, dict[str, float]]] = []
    for arch in arch_dicts:
        try:
            arch_config = ArchConfig(**arch)
            quality = evaluator.evaluate(arch_config)
            latency = float(estimator.get_latency(arch_config))
        except Exception as exc:  # noqa: BLE001 -- per-candidate failure must fail loud
            raise _AnchorError(f"anchor evaluation failed for arch={_arch_key(arch)}: {exc}") from exc
        if not isinstance(quality, dict) or not quality:
            raise _AnchorError(
                f"evaluator returned no quality objectives for arch={_arch_key(arch)} (got {quality!r})"
            )
        objs = dict(quality)
        objs["latency"] = latency
        out.append((arch, objs))
    return out


def _resolve_device():
    """Same device policy as the generated scripts: auto-resolve, CPU fallback."""
    try:
        from nas_agent.train import resolve_device  # type: ignore[import-untyped]

        return resolve_device("auto")
    except Exception:  # noqa: BLE001 -- nas_agent absent -> plain torch fallback
        import torch  # noqa: PLC0415

        return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _load_search_config(ad: Path) -> dict | None:
    """Read ``search_config.yaml`` (PyYAML); None on missing/malformed."""
    cfg_path = ad / "search_config.yaml"
    if not cfg_path.is_file():
        return None
    try:
        import yaml  # type: ignore[import-untyped]

        with cfg_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (ImportError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _wrap_cfg(raw: Any) -> Any:
    """Attribute-access wrapper for an evaluator/latency cfg block (OmegaConf or namespace)."""
    if raw is None:
        raw = {}
    if isinstance(raw, dict):
        try:
            from omegaconf import OmegaConf  # type: ignore[import-untyped]

            return OmegaConf.create(raw)
        except ImportError:
            return _Ns(raw)
    return raw


class _Ns:
    """Minimal attribute-access wrapper when OmegaConf isn't installed."""

    def __init__(self, raw: dict) -> None:
        self._raw = raw

    def __getattr__(self, key: str) -> Any:
        try:
            return self._raw[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def get(self, key: str, default: Any = None) -> Any:
        return self._raw.get(key, default)


# ── shared helpers ─────────────────────────────────────────────────────────────


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.is_file():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
    return rows


def _load_sibling(path: Path, mod_name: str):
    """Import a sibling .py as a module, registered in ``sys.modules`` before exec."""
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    if spec is None or spec.loader is None:
        raise _AnchorError(f"cannot build module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    return module


def _sys_modules_get(mod_name: str) -> Any:
    mod = sys.modules.get(mod_name)
    if mod is None:
        raise _AnchorError(f"module {mod_name} was not loaded")
    return mod


class _AnchorError(Exception):
    """Structural failure with a user-facing message (caller prints stderr + exits 1)."""


if __name__ == "__main__":
    sys.exit(main())
