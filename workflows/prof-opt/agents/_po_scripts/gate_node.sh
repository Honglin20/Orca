#!/usr/bin/env bash
# gate_node.sh — deterministic promotion + decision script-node wrapper.
# Promotion may advance the current base and writes incumbent_promotion.json.
# gate_decide.py then reads the workspace only (history terminal rows,
# round_state current, every round's proposals.json for the idle probe, the
# frozen origin anchor); the knobs are the round cap and the idle-round cap
# (workflow inputs). Before either step, the deployed script set's version stamp is
# verified — a tampered or half-deployed workspace emits the finish-failed
# disclosure payload, which matches NO explicit route and lands in the
# catch-all (to: po_report) so the failure is disclosed, never guessed around.
set -euo pipefail

ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (gate_node.sh)}"
MAXR="100"
IDLE_CAP="5"
while [ $# -gt 0 ]; do
  case "$1" in
    --max-rounds) MAXR="${2:?}"; shift 2 ;;
    --idle-round-cap) IDLE_CAP="${2:?}"; shift 2 ;;
    *) echo "FATAL: unknown argument $1" >&2; exit 2 ;;
  esac
done

if ! bash "$ART/scripts/deploy_scripts.sh" --verify; then
  python3 "$ART/scripts/emit_result.py" \
    --field decision=finish-failed --field round=0 --field target_cycles=0 \
    --field 'success_vids=[]' --field 'in_flight=[]' \
    --field "reason=deployed script set failed its .VERSION stamp check (tampered or half-deployed) — do NOT fresh_start mid-run; redeploy the shared scripts or escalate for manual intervention" \
    --field "error=deploy --verify failed (see stderr in the run log)"
  exit 0
fi

# Accuracy-safe latency improvements that have completed training become the
# next round's base before the pure decision script evaluates loop/report.
promote_out=""
promote_err="$ART/.incumbent_promotion.stderr"
rm -f "$ART/incumbent_promotion.json" "$promote_err"
if ! promote_out="$(python3 "$ART/scripts/promote_incumbent.py" --artifacts "$ART" 2>"$promote_err")"; then
  promote_note="$(cat "$promote_err")"
  rm -f "$promote_err"
  python3 "$ART/scripts/emit_result.py" \
    --field decision=finish-failed --field round=0 --field target_cycles=0 \
    --field 'success_vids=[]' --field 'in_flight=[]' \
    --field reason="incumbent promotion failed" \
    --field "error=$promote_note"
  exit 0
fi
rm -f "$promote_err"
printf '%s\n' "$promote_out" > "$ART/incumbent_promotion.json"
promotion_arg=()
if [ "$(python3 -c 'import json,sys; print(str(bool(json.loads(sys.argv[1]).get("promoted"))).lower())' "$promote_out")" = "true" ]; then
  promotion_arg=(--incumbent-promoted)
fi

rc=0
OUT="$(python3 "$ART/scripts/gate_decide.py" --artifacts "$ART" \
  --max-rounds "$MAXR" --idle-round-cap "$IDLE_CAP" \
  "${promotion_arg[@]}")" || rc=$?
if [ "$rc" -eq 0 ]; then
  python3 "$ART/scripts/emit_result.py" --json "$OUT" \
    --field "incumbent_promotion=$promote_out" \
    --field 'incumbent_promotion_path=incumbent_promotion.json' \
    --field 'error='
else
  python3 "$ART/scripts/emit_result.py" \
    --field decision=finish-failed --field round=0 --field target_cycles=0 \
    --field 'success_vids=[]' --field 'in_flight=[]' \
    --field "incumbent_promotion=$promote_out" \
    --field 'incumbent_promotion_path=incumbent_promotion.json' \
    --field reason="gate decision script failed" \
    --field "error=gate_decide exited rc=$rc (see stderr in the run log)"
fi
