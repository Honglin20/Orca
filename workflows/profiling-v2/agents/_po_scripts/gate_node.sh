#!/usr/bin/env bash
# gate_node.sh — frontier snapshot + pure-read decision script-node wrapper
# (v8: the base never moves — there is no promotion).
# frontier_snapshot.py refreshes base/frontier.json (the mechanical
# Pareto/avoid/in-flight view the next propose round reads). gate_decide.py
# then reads the workspace only (history terminal rows, round_state current,
# every round's proposals.json for the idle probe, the frozen origin anchor);
# the knobs are the round cap and the idle-round cap (workflow inputs).
# Before either step, the deployed script set's version stamp is verified — a
# tampered or half-deployed workspace emits the finish-failed disclosure
# payload, which matches NO explicit route and lands in the catch-all
# (to: po_report) so the failure is disclosed, never guessed around.
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

# Per-round shadow source archive (C1): sidecar — an unexpected crash must not
# block the gate (stderr stays visible; per-vid failures land in
# rounds/<RRR>/shadow_archive_error.json inside the script). A mid-run resume
# against an old deployed set without this script only echoes this stderr line.
python3 "$ART/scripts/archive_round_shadow.py" --artifacts "$ART" || echo "archive_round_shadow failed (non-zero; see stderr)" >&2

# Refresh the mechanical frontier/avoid/in-flight view BEFORE the decision:
# the next propose round and the report read it as their number layer. A
# torn workspace (missing anchor, unparseable rows) fails loud here — the
# decision input is never silently stale. The root cause travels in the
# payload (finish-failed must be self-describing, never log-only).
frontier_err="$ART/.frontier_snapshot.stderr"
rm -f "$frontier_err"
if ! python3 "$ART/scripts/frontier_snapshot.py" --artifacts "$ART" \
    >/dev/null 2>"$frontier_err"; then
  frontier_note="$(cat "$frontier_err")"
  rm -f "$frontier_err"
  python3 "$ART/scripts/emit_result.py" \
    --field decision=finish-failed --field round=0 --field target_cycles=0 \
    --field 'success_vids=[]' --field 'in_flight=[]' \
    --field reason="frontier snapshot failed" \
    --field "error=$frontier_note"
  exit 0
fi
rm -f "$frontier_err"

# v2 §4: refresh the derived human layer (base/history.md) before the
# decision — the next propose round reads it as its brief input. stdout is
# REDIRECTED (/dev/null + an err file): po_gate is parse_json:true and this
# node's stdout must stay a single JSON object (render_history prints the
# whole markdown on stdout by contract). A non-zero render maps into the
# same finish-failed disclosure payload as the frontier snapshot failure.
history_err="$ART/.render_history.stderr"
rm -f "$history_err"
if ! python3 "$ART/scripts/render_history.py" --artifacts "$ART" \
    >/dev/null 2>"$history_err"; then
  history_note="$(cat "$history_err")"
  rm -f "$history_err"
  python3 "$ART/scripts/emit_result.py" \
    --field decision=finish-failed --field round=0 --field target_cycles=0 \
    --field 'success_vids=[]' --field 'in_flight=[]' \
    --field reason="history render failed" \
    --field "error=$history_note"
  exit 0
fi
rm -f "$history_err"

rc=0
OUT="$(python3 "$ART/scripts/gate_decide.py" --artifacts "$ART" \
  --max-rounds "$MAXR" --idle-round-cap "$IDLE_CAP")" || rc=$?
if [ "$rc" -eq 0 ]; then
  python3 "$ART/scripts/emit_result.py" --json "$OUT" \
    --field 'error='
else
  python3 "$ART/scripts/emit_result.py" \
    --field decision=finish-failed --field round=0 --field target_cycles=0 \
    --field 'success_vids=[]' --field 'in_flight=[]' \
    --field reason="gate decision script failed" \
    --field "error=gate_decide exited rc=$rc (see stderr in the run log)"
fi
