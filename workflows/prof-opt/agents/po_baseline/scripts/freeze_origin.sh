#!/usr/bin/env bash
# freeze_origin.sh — guarded, idempotent origin-anchor freeze.
#
# Run after every non-failed profiling-chain invocation. The guard skips the
# call while the profiling products are not on disk yet (intermediate chain
# states) and once the anchor already exists (the freeze is a no-op then).
# analyze.py fails loud on an illegal value range or an existing anchor with
# DIFFERENT content — its stderr names the fresh_start remedy; a non-zero
# exit here must be quoted verbatim into the node's `error`.
#
# usage: freeze_origin.sh <latency_reduction_min> <accuracy_budget>
set -euo pipefail

ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (freeze_origin.sh)}"
LRM="${1:?FATAL: usage: freeze_origin.sh <latency_reduction_min> <accuracy_budget>}"
AB="${2:?FATAL: usage: freeze_origin.sh <latency_reduction_min> <accuracy_budget>}"

if [ -s "$ART/base/profile/profile_summary.json" ] && \
   [ ! -f "$ART/base/origin_anchor.json" ]; then
  python3 "$ART/scripts/analyze.py" \
    --profile-dir "$ART/base/profile" --freeze-origin \
    --latency-reduction-min "$LRM" \
    --accuracy-budget "$AB" || exit 1
fi
