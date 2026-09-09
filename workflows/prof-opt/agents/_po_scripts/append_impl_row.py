"""append_impl_row.py — CLI entry for the proposal node's mechanical history
writes.

Thin wrapper over history_lib.append_implemented (and, for a broken
implementation, the append_outcome row that follows it) so the node prompt
stays a single invocation with no import-path knowledge. The field set and
its validation stay in history_lib — the only write path for history.jsonl.

Lineage gate: --parent-vid / --base-at-proposal must name the CURRENT base
(the incumbent — a variant that PASSED the accuracy gate AND improved
latency — or the origin baseline with parent null). A variant that never
passed both gates (e.g. latency_improved but accuracy_fail) is a lineage
dead-end: recording it as a parent fails loud here, at the only write path,
so a stacked-on-a-failed-base lineage can never enter history.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from history_lib import append_implemented, append_outcome, expected_base
from history_lib import JOINT_RETRY_OUTCOMES


def nullable_value(raw: str):
    """CLI-facing nullable value: ``null``/``none`` -> None, else int/float/
    raw string (the --parent-vid flag's parser)."""
    if raw.strip().lower() in ("null", "none"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def main() -> int:
    art = os.environ.get("ORCA_ARTIFACTS_DIR", "")
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", default=os.path.join(art, "history.jsonl")
                    if art else None,
                    help="history.jsonl path (default $ORCA_ARTIFACTS_DIR/history.jsonl)")
    ap.add_argument("--vid", required=True)
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--seq", type=int, required=True)
    ap.add_argument("--parent-vid", type=nullable_value, default=None,
                    help="lineage parent vid, or null for the origin baseline")
    ap.add_argument("--change-sig", required=True)
    ap.add_argument("--probe-epochs", type=int, required=True,
                    help="contracts.json proxy_budget.epochs (epoch-only, v7)")
    ap.add_argument("--target-modules", required=True,
                    help="JSON list from declaration.target_modules")
    ap.add_argument("--predicted-delta-cycles", type=int, default=None)
    ap.add_argument("--base-at-proposal", required=True,
                    help='JSON object, e.g. {"vid": null, "makespan_cycles": 15288}')
    ap.add_argument("--not-implemented", action="store_true",
                    help="write implemented=False (terminal-skip path)")
    ap.add_argument("--outcome", choices=sorted(JOINT_RETRY_OUTCOMES),
                    help="with --not-implemented: append the outcome row too")
    ns = ap.parse_args()
    if ns.outcome and not ns.not_implemented:
        # an implemented=True row never carries a terminal outcome — a silent
        # outcome row here would permanently burn the sig's joint retry budget
        ap.error("--outcome is only valid together with --not-implemented")
    if not ns.history:
        print("FATAL: --history missing and ORCA_ARTIFACTS_DIR not set",
              file=sys.stderr)
        return 2

    # Lineage gate BEFORE any write: the parent must be the current base.
    if not art:
        print("FATAL: ORCA_ARTIFACTS_DIR not set — the lineage gate cannot "
              "resolve the current base", file=sys.stderr)
        return 2
    try:
        expected_vid, expected_ms = expected_base(art)
    except ValueError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    if ns.parent_vid != expected_vid:
        print(f"FATAL: parent_vid {ns.parent_vid!r} is not the current base "
              f"(expected {expected_vid!r}). The only legal parent is the "
              "current incumbent — a variant that PASSED the accuracy gate "
              "AND improved latency — or null for the origin baseline. A "
              "variant that failed either gate (e.g. latency_improved but "
              "accuracy_fail) is a lineage dead-end: re-derive the idea on "
              "the incumbent shadow instead of stacking on its tree.",
              file=sys.stderr)
        return 2
    base = None
    try:
        modules = json.loads(ns.target_modules)
        base = json.loads(ns.base_at_proposal)
    except json.JSONDecodeError as exc:
        print(f"FATAL: {exc} (JSON flags must be valid JSON)", file=sys.stderr)
        return 2
    if not isinstance(base, dict) or {
            "vid": base.get("vid"),
            "makespan_cycles": base.get("makespan_cycles"),
            } != {"vid": expected_vid, "makespan_cycles": expected_ms}:
        print(f"FATAL: base_at_proposal does not match the current base "
              f"(expected {{'vid': {expected_vid!r}, "
              f"'makespan_cycles': {expected_ms!r}}}); a stale or invented "
              "base pointer is a lineage lie", file=sys.stderr)
        return 2

    try:
        append_implemented(
            ns.history, ns.vid,
            round=ns.round, seq=ns.seq, parent_vid=ns.parent_vid,
            change_sig=ns.change_sig,
            probe_epochs=ns.probe_epochs,
            target_modules=modules,
            predicted_delta_cycles=ns.predicted_delta_cycles,
            base_at_proposal=base,
            implemented=not ns.not_implemented)
        if ns.outcome:
            append_outcome(ns.history, ns.vid, ns.outcome)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"FATAL: {exc} (JSON flags must be valid JSON)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
