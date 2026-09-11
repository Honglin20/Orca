#!/usr/bin/env bash
# freeze_origin.sh — guarded, idempotent origin-anchor freeze.
#
# Run after every non-failed profiling-chain invocation. The guard skips the
# call while the profiling products are not on disk yet (intermediate chain
# states) and once the anchor already exists (the freeze is a no-op then).
# The canonical makespan is read directly from the single raw
# base/profile/<onnx_stem>/schedule_result.json. No derived profiling JSON is
# created. An existing anchor with different content fails loud.
#
# usage: freeze_origin.sh <latency_reduction_min> <accuracy_budget>
set -euo pipefail

ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (freeze_origin.sh)}"
LRM="${1:?FATAL: usage: freeze_origin.sh <latency_reduction_min> <accuracy_budget>}"
AB="${2:?FATAL: usage: freeze_origin.sh <latency_reduction_min> <accuracy_budget>}"

python3 - "$ART/base/profile" "$ART/base/origin_anchor.json" "$LRM" "$AB" <<'PYEOF'
import json, sys
from pathlib import Path

profile_dir, anchor_path = Path(sys.argv[1]), Path(sys.argv[2])
latency_reduction_min, accuracy_budget = float(sys.argv[3]), float(sys.argv[4])
hits = sorted(profile_dir.glob("*/schedule_result.json"))
if not hits:
    raise SystemExit(0)
if len(hits) != 1:
    raise SystemExit(f"freeze_origin: expected exactly one schedule_result.json under {profile_dir}, got {len(hits)}")
if not 0.0 < latency_reduction_min < 1.0:
    raise SystemExit(f"freeze_origin: latency_reduction_min must be in (0, 1), got {latency_reduction_min}")
if accuracy_budget < 0:
    raise SystemExit(f"freeze_origin: accuracy_budget must be >= 0, got {accuracy_budget}")
raw = json.loads(hits[0].read_text(encoding="utf-8"))
makespan = raw.get("parallel_cycles")
if isinstance(makespan, bool) or not isinstance(makespan, int) or makespan < 0:
    raise SystemExit(f"freeze_origin: {hits[0]} carries invalid parallel_cycles: {makespan!r}")
payload = {
    "baseline_makespan_cycles": makespan,
    "latency_reduction_min": latency_reduction_min,
    "accuracy_budget": accuracy_budget,
    "target_cycles": int(makespan * (1.0 - latency_reduction_min)) + 1,
    "frozen_at_round": 0,
}
if anchor_path.exists():
    existing = json.loads(anchor_path.read_text(encoding="utf-8"))
    if existing != payload:
        raise SystemExit("freeze_origin: existing origin anchor differs; rebuild with fresh_start=true")
else:
    anchor_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
PYEOF
