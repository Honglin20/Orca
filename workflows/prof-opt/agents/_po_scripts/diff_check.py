#!/usr/bin/env python3
"""diff_check.py — declaration-vs-reality utility.

The reference frame is ALWAYS the CURRENT base (base shadow / base onnx) —
never the original round-1 state, because rounds stack onto an advancing
base. A variant declaration is judged layer by layer:

    --layer file    variant shadow vs base shadow diff file set
                    (minus __pycache__//*.pyc) == declaration.edited_files
    --layer graph   legacy diagnostic only; the active prof-opt proposal path
                    uses file-layer source diffs and does not gate on it

Exit codes: 0 = match, 1 = mismatch (a legitimate deterministic verdict,
printed as JSON on stdout), >=2 = hard error (missing file / bad JSON).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _emit(payload: dict) -> int:
    print(json.dumps(payload))
    return 0 if payload["match"] else 1


def _shadow_diff_files(base_shadow: Path, variant_shadow: Path) -> set[str]:
    """Relative paths that differ in content or existence, __pycache__//*.pyc
    excluded — bytecode churn is not an edit."""

    def _scan(root: Path) -> dict[str, bytes]:
        if not root.is_dir():
            return {}
        files = {}
        for item in sorted(root.rglob("*")):
            rel = item.relative_to(root)
            if "__pycache__" in rel.parts or rel.suffix == ".pyc" or not item.is_file():
                continue
            files[str(rel).replace("\\", "/")] = item.read_bytes()
        return files

    base, variant = _scan(base_shadow), _scan(variant_shadow)
    diff = set()
    for rel in sorted(set(base) | set(variant)):
        if base.get(rel) != variant.get(rel):
            diff.add(rel)
    return diff


def check_file_layer(ns: argparse.Namespace) -> dict:
    edited = ns.edited_files
    if edited is None:
        raise ValueError("--layer file requires --edited-files")
    diff = _shadow_diff_files(Path(ns.base_shadow), Path(ns.variant_shadow))
    edited_set = {str(p).replace("\\", "/") for p in edited}
    return {
        "layer": "file", "match": diff == edited_set,
        "diff_files": sorted(diff), "edited_files": sorted(edited_set),
        "not_declared": sorted(diff - edited_set),
        "declared_but_absent": sorted(edited_set - diff),
    }


def check_graph_layer(ns: argparse.Namespace) -> dict:
    import onnx

    if not ns.op_delta:
        raise ValueError("--layer graph requires --op-delta")
    declared: dict[str, int] = json.loads(ns.op_delta) if not ns.op_delta.startswith("@") \
        else json.loads(Path(ns.op_delta[1:]).read_text(encoding="utf-8"))

    def _op_counts(path: Path) -> dict[str, int]:
        model = onnx.load(str(path))
        counts: dict[str, int] = {}
        for node in model.graph.node:
            counts[node.op_type] = counts.get(node.op_type, 0) + 1
        return counts

    base, variant = _op_counts(Path(ns.base_onnx)), _op_counts(Path(ns.variant_onnx))
    actual = {op: variant.get(op, 0) - base.get(op, 0)
              for op in set(base) | set(variant)
              if variant.get(op, 0) - base.get(op, 0) != 0}
    declared_nonzero = {op: n for op, n in declared.items() if n != 0}
    return {
        "layer": "graph", "match": actual == declared_nonzero,
        "actual_op_delta": dict(sorted(actual.items())),
        "declared_op_delta": dict(sorted(declared.items())),
        "mismatched_ops": sorted({op for op in set(actual) | set(declared_nonzero)
                                  if actual.get(op) != declared_nonzero.get(op)}),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--layer", required=True, choices=["file", "graph"])
    # file layer
    ap.add_argument("--base-shadow")
    ap.add_argument("--variant-shadow")
    ap.add_argument("--edited-files", help="JSON list or @file of edited paths")
    # graph layer
    ap.add_argument("--base-onnx")
    ap.add_argument("--variant-onnx")
    ap.add_argument("--op-delta", help='inline JSON or @file, e.g. \'{"Relu":4}\'')
    ns = ap.parse_args()

    try:
        if ns.layer == "file":
            if ns.edited_files:
                raw = ns.edited_files
                if raw.startswith("@"):
                    raw = Path(raw[1:]).read_text(encoding="utf-8")
                parsed = json.loads(raw)
                if not isinstance(parsed, list):
                    raise ValueError("--edited-files must be a JSON list of paths")
                ns.edited_files = parsed
            payload = check_file_layer(ns)
        else:
            payload = check_graph_layer(ns)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"diff_check: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return _emit(payload)


if __name__ == "__main__":
    sys.exit(main())
