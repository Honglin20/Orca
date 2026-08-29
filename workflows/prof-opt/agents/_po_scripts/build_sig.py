"""build_sig.py — CLI wrapper for the canonical change signature.

build_change_sig (predict_delta) is the only legitimate way to assemble a
change_sig: params come from the predictor, modules are sorted and
comma-joined inside the builder (dedup is exact-string — a hand-ordered list
would silently dodge the permanent-dedup set). This wrapper keeps subagent
prompts to a single call with no import-path knowledge.
"""
from __future__ import annotations

import argparse
import json
import sys

from predict_delta import build_change_sig


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lever", required=True)
    ap.add_argument("--params", required=True,
                    help="params string from the predictor")
    ap.add_argument("--modules", required=True,
                    help="JSON list of the affected modules (order-insensitive)")
    ns = ap.parse_args()
    try:
        print(build_change_sig(ns.lever, ns.params, json.loads(ns.modules)))
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
