#!/usr/bin/env bash
# check_prerequisites.sh — po_propose entry check: the shared deterministic
# scripts the proposal loop depends on must already be deployed into the
# workspace by the entry node (flatten Step 1 deploys $ORCA_ARTIFACTS_DIR/scripts/).
#
# Exit codes: 0 = all present; non-zero = ORCA_ARTIFACTS_DIR unset /
#   workspace unreachable / a script missing (the entry stage did not
#   complete — the node must fail loud, never proceed against a
#   half-deployed workspace).
set -euo pipefail

ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (check_prerequisites.sh)}"
cd "$ART" || { echo "FATAL: workspace unreachable: $ART" >&2; exit 2; }

for f in analyze.py predict_delta.py history_lib.py experiment_ledger.py \
         emit_result.py check_bottleneck.py; do
  [ -f "$ART/scripts/$f" ] || {
    echo "FATAL: scripts/$f not deployed — entry stage incomplete" >&2; exit 2; }
done

echo "prerequisites: ok (shared scripts deployed at $ART/scripts)" >&2
