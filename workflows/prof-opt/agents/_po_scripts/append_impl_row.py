"""append_impl_row.py — CLI entry for the proposal node's mechanical history
writes.

Thin wrapper over history_lib.append_implemented (and, for a broken
implementation, the append_outcome row that follows it) so the node prompt
stays a single invocation with no import-path knowledge. The field set and
its validation stay in history_lib — the only write path for history.jsonl.

Nullable flags accept ``null``/``none``: a pinned "mechanism absent" value,
never an unset one (same semantics as the dedup CLI in history_lib).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from history_lib import append_implemented, append_outcome
from history_lib import JOINT_RETRY_OUTCOMES, nullable_int, nullable_value


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
                    help="lineage parent vid, or null (the base never advances)")
    ap.add_argument("--change-sig", required=True)
    ap.add_argument("--probe-epochs", type=int, required=True,
                    help="contracts.json proxy_budget.epochs")
    ap.add_argument("--probe-max-steps", type=nullable_int, required=True,
                    help="proxy_budget.max_steps, or null")
    ap.add_argument("--probe-data-value", type=nullable_value, required=True,
                    help="proxy_budget.data_value, or null")
    ap.add_argument("--target-modules", required=True,
                    help="JSON list from declaration.target_modules")
    ap.add_argument("--predicted-delta-cycles", type=int, required=True)
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

    try:
        modules = json.loads(ns.target_modules)
        base = json.loads(ns.base_at_proposal)
        append_implemented(
            ns.history, ns.vid,
            round=ns.round, seq=ns.seq, parent_vid=ns.parent_vid,
            change_sig=ns.change_sig,
            probe_epochs=ns.probe_epochs, probe_max_steps=ns.probe_max_steps,
            probe_data_value=ns.probe_data_value,
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
