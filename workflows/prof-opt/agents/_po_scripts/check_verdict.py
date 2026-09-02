#!/usr/bin/env python3
"""check_verdict.py — the ONE latency-line predicate (v7 §6.2).

`makespan_cycles <= target_cycles` (boundary INCLUSIVE) was hand-copied in
three places in v6 (the recheck gate, the probe emit gate, the probe
protocol's precondition heredoc); v7 collapses them into this script. Every
caller — run_latency_recheck.sh, check_probe_emit.py, and the probe
protocol's precondition — invokes THIS script; none of them re-implements
the comparison.

    --vid <VID>   (required) the variant whose verdict.json is judged
    --artifacts   optional override; $ORCA_ARTIFACTS_DIR is the default
    --makespan N  judge N against the frozen line directly instead of
                  reading verdict.json (the recheck's pre-verdict gate —
                  same single comparison, no third hand-copy)

Exit codes:
    0  verdict holds — stdout carries {"vid", "makespan_cycles",
       "target_cycles", "ok": true}
    1  verdict does NOT hold — above the frozen line, or torn workspace
       (verdict/anchor missing, unparseable, non-integer makespan).
       The stderr message names which; the caller decides what a torn
       verdict means in its context (the recheck records latency_fail;
       the probe fails loud — never re-measures).
    2  hard usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _load_json(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{path} missing — torn workspace") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} unparseable ({exc}) — torn workspace") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"{path} is not a JSON object — torn workspace")
    return doc


def check_verdict(artifacts: Path, vid: str,
                  makespan: int | None = None) -> dict:
    """The single predicate. With --makespan the given value is judged
    against the anchor directly (the recheck's pre-verdict gate); without
    it the value comes from variants/<vid>/verdict.json (the probe emit
    gate + the protocol precondition). Either way the SAME inclusive
    comparison decides."""
    anchor = _load_json(artifacts / "base" / "origin_anchor.json")
    target = anchor.get("target_cycles")
    if not isinstance(target, int) or isinstance(target, bool):
        raise ValueError(f"origin anchor carries no integer target_cycles: "
                         f"{anchor!r}")
    if makespan is None:
        verdict = _load_json(artifacts / "variants" / vid / "verdict.json")
        ms = verdict.get("makespan_cycles")
    else:
        ms = makespan
    if not isinstance(ms, int) or isinstance(ms, bool):
        raise ValueError(
            f"{vid} carries no makespan_cycles (structural mismatch "
            f"verdicts carry null) — torn workspace")
    if ms > target:  # inclusive boundary: == target HOLDS
        raise ValueError(
            f"{vid} makespan {ms} > frozen target {target} — the verdict is "
            f"above the frozen line")
    return {"vid": vid, "makespan_cycles": ms, "target_cycles": target,
            "ok": True}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vid", required=True)
    ap.add_argument("--artifacts", default=None,
                    help="workspace root (default: $ORCA_ARTIFACTS_DIR)")
    ap.add_argument("--makespan", type=int, default=None,
                    help="judge this value against the frozen line instead "
                         "of reading verdict.json (the recheck's pre-verdict "
                         "gate)")
    ns = ap.parse_args()

    art_raw = ns.artifacts or os.environ.get("ORCA_ARTIFACTS_DIR")
    if not art_raw:
        print("check_verdict: FAIL --artifacts or ORCA_ARTIFACTS_DIR is "
              "required", file=sys.stderr)
        return 2
    try:
        result = check_verdict(Path(art_raw), ns.vid, ns.makespan)
    except ValueError as exc:
        print(f"check_verdict: FAIL {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
