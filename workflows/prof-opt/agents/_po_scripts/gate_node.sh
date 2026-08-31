#!/usr/bin/env bash
# gate_node.sh — deterministic script-node wrapper around gate_decide.py.
# The decision reads the workspace only (history terminal rows, round_state
# current, the frozen origin anchor); the round cap is the single knob.
# Before deciding, the deployed script set's version stamp is verified — a
# tampered or half-deployed workspace emits the finish-failed disclosure
# payload, which matches NO explicit route and lands in the catch-all
# (to: po_report) so the failure is disclosed, never guessed around.
set -euo pipefail

ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (gate_node.sh)}"
MAXR="100"
while [ $# -gt 0 ]; do
  case "$1" in
    --max-rounds) MAXR="${2:?}"; shift 2 ;;
    *) echo "FATAL: unknown argument $1" >&2; exit 2 ;;
  esac
done

if ! bash "$ART/scripts/deploy_scripts.sh" --verify; then
  python3 "$ART/scripts/emit_result.py" \
    --field decision=finish-failed --field round=0 --field target_cycles=0 \
    --field 'success_vids=[]' --field 'in_flight=[]' \
    --field "reason=deployed script set failed its .VERSION stamp check (tampered or half-deployed) — rebuild the workspace with fresh_start" \
    --field "error=deploy --verify failed (see stderr in the run log)"
  exit 0
fi

rc=0
OUT="$(python3 "$ART/scripts/gate_decide.py" --artifacts "$ART" \
  --max-rounds "$MAXR")" || rc=$?
if [ "$rc" -eq 0 ]; then
  python3 "$ART/scripts/emit_result.py" --json "$OUT" --field 'error='
else
  python3 "$ART/scripts/emit_result.py" \
    --field decision=finish-failed --field round=0 --field target_cycles=0 \
    --field 'success_vids=[]' --field 'in_flight=[]' \
    --field reason="gate decision script failed" \
    --field "error=gate_decide exited rc=$rc (see stderr in the run log)"
fi
