#!/usr/bin/env bash
# reuse_check.sh — pz_baseline Step 0 soft-skip gate.
# Prints REUSE_VALID when block_map.json + baseline_metrics.json both exist and
# carry the fields downstream (bld/score/gate) consume. Otherwise prints nothing
# and the agent runs Step 1 (measure_baseline.py).
set +e
cd "$ORCA_ARTIFACTS_DIR" 2>/dev/null || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

MISSING=""
for f in block_map.json baseline_metrics.json; do
  [ -s "$f" ] || MISSING="$MISSING $f"
done
[ -n "$MISSING" ] && exit 0

python3 - <<'PY' 2>/dev/null
import json
bm = json.load(open("block_map.json", encoding="utf-8"))
slots = bm.get("slots") if isinstance(bm, dict) else None
assert isinstance(slots, list) and len(slots) >= 1, "block_map.json slots missing/empty"

m = json.load(open("baseline_metrics.json", encoding="utf-8"))
for k in ("baseline_acc", "baseline_latency", "latency_floor", "max_achievable_reduction", "smokes_passed"):
    assert k in m, f"baseline_metrics.json missing field {k}"
assert isinstance(m["smokes_passed"], list) and len(m["smokes_passed"]) >= 1, \
    "baseline_metrics.json smokes_passed must be a non-empty list"
print("REUSE_VALID")
PY
