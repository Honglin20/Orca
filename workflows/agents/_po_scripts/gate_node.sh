#!/usr/bin/env bash
# gate_node.sh — deterministic script-node wrapper around gate_decide.py.
set -euo pipefail

ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (gate_node.sh)}"
TARGET=""; MAXR="5"
while [ $# -gt 0 ]; do
  case "$1" in
    --latency-reduction-min) TARGET="${2:?}"; shift 2 ;;
    --max-rounds) MAXR="${2:?}"; shift 2 ;;
    *) echo "FATAL: unknown argument $1" >&2; exit 2 ;;
  esac
done

rc=0
OUT="$(python3 "$ART/scripts/gate_decide.py" --artifacts "$ART" \
  --latency-reduction-min "$TARGET" --max-rounds "$MAXR)" || rc=$?
if [ "$rc" -eq 0 ]; then
  python3 "$ART/scripts/emit_result.py" --json "$OUT" --field 'error='
else
  python3 "$ART/scripts/emit_result.py" \
    --field decision=finish-failed --field round=0 --field stall=0 \
    --field best=null --field reason="gate decision script failed" \
    --field "error=gate_decide exited rc=$rc (see stderr in the run log)"
fi
