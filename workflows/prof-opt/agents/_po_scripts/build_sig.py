"""build_sig.py — CLI wrapper for the canonical change signature.

The proposer supplies a stable params string from its concrete change spec;
modules are sorted and comma-joined here because dedup is exact-string and a
hand-ordered list would silently dodge the permanent-dedup set.
"""
from __future__ import annotations

import argparse
import json
import sys


def build_change_sig(lever: str, params: str, modules: list[str]) -> str:
    modules_canonical = ",".join(sorted(module for module in modules if module))
    if not lever or not params or not modules_canonical:
        raise ValueError("build_change_sig needs non-empty lever, params and modules")
    return f"{lever}:{params}:{modules_canonical}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lever", required=True)
    ap.add_argument("--params", required=True,
                    help="stable params string from the concrete change spec")
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
