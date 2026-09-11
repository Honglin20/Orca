"""append_impl_row.py — CLI entry for the proposal node's mechanical history
writes.

Thin wrapper over history_lib.append_implemented (and, for a broken
implementation, the append_outcome row that follows it) so the node prompt
stays a single invocation with no import-path knowledge. The field set and
its validation stay in history_lib — the only write path for history.jsonl.

Lineage (v8): the base tree NEVER moves — there is no parent. Provenance is
composition: --absorbs names the frontier vids whose proven mechanisms the
design fuses. Every absorbs vid must exist in history and must not be the
vid itself; the origin anchor must be present (expected_base fails loud
otherwise). A stale base/incumbent.json fails loud here too — a pre-v8
workspace is never silently adopted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from history_lib import append_implemented, append_outcome, expected_base
from history_lib import (FACET_UNCHANGED, HistoryError, JOINT_RETRY_OUTCOMES,
                         read_rows)


def main() -> int:
    art = os.environ.get("ORCA_ARTIFACTS_DIR", "")
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", default=os.path.join(art, "history.jsonl")
                    if art else None,
                    help="history.jsonl path (default $ORCA_ARTIFACTS_DIR/history.jsonl)")
    ap.add_argument("--vid", required=True)
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--seq", type=int, required=True)
    ap.add_argument("--change-sig", required=True)
    ap.add_argument("--probe-epochs", type=int, required=True,
                    help="contracts.json proxy_budget.epochs (epoch-only)")
    ap.add_argument("--target-modules", required=True,
                    help="JSON list from declaration.target_modules")
    ap.add_argument("--absorbs", default="[]",
                    help="JSON list of frontier vids whose mechanisms this "
                         "design fuses (composition lineage; default [])")
    ap.add_argument("--predicted-delta-cycles", type=int, default=None)
    ap.add_argument("--structure-change", default=FACET_UNCHANGED,
                    help="structure facet phrase (≤80 code points; "
                         "declaration.json verbatim; default 未改)")
    ap.add_argument("--feature-change", default=FACET_UNCHANGED,
                    help="feature facet phrase (same rules as "
                         "--structure-change)")
    ap.add_argument("--loss-change", default=FACET_UNCHANGED,
                    help="loss facet phrase (same rules as "
                         "--structure-change)")
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
    if not art:
        print("FATAL: ORCA_ARTIFACTS_DIR not set — the lineage gate cannot "
              "resolve the origin anchor", file=sys.stderr)
        return 2

    # Lineage gate BEFORE any write: the origin anchor must be frozen, and
    # every absorbed vid must exist (composition provenance, never invented).
    try:
        expected_base(art)
    except ValueError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    try:
        modules = json.loads(ns.target_modules)
        absorbs = json.loads(ns.absorbs)
    except json.JSONDecodeError as exc:
        print(f"FATAL: {exc} (JSON flags must be valid JSON)", file=sys.stderr)
        return 2
    if not isinstance(absorbs, list) or not all(isinstance(v, str) and v
                                                for v in absorbs):
        print("FATAL: --absorbs must be a JSON array of vid strings",
              file=sys.stderr)
        return 2
    if ns.vid in absorbs:
        print(f"FATAL: --absorbs names the vid itself ({ns.vid!r}) — a "
              "design cannot absorb itself", file=sys.stderr)
        return 2
    known_vids: set = set()
    try:
        known_vids = {row.get("vid") for row in read_rows(ns.history)}
    except HistoryError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    unknown = [v for v in absorbs if v not in known_vids]
    if unknown:
        print(f"FATAL: --absorbs names vid(s) absent from history: {unknown} "
              "— composition provenance is mechanical, never invented",
              file=sys.stderr)
        return 2

    try:
        append_implemented(
            ns.history, ns.vid,
            round=ns.round, seq=ns.seq,
            change_sig=ns.change_sig,
            probe_epochs=ns.probe_epochs,
            target_modules=modules,
            absorbs=absorbs,
            predicted_delta_cycles=ns.predicted_delta_cycles,
            implemented=not ns.not_implemented,
            structure_change=ns.structure_change,
            feature_change=ns.feature_change,
            loss_change=ns.loss_change)
        if ns.outcome:
            append_outcome(ns.history, ns.vid, ns.outcome)
    except (json.JSONDecodeError, ValueError, HistoryError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
