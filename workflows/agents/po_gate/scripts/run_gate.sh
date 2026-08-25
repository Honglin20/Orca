#!/usr/bin/env bash
# run_gate.sh — read-only loop/exit decision for the current workspace.
#
# Wraps gate_decide.py (pure read + compute from history.jsonl, best.json and
# the current round's proposals.json; writes NOTHING) and adds the node
# output error field. On a gate_decide failure the wrapper emits a terminal
# finish-failed decision with the error filled — the run then ends at the
# report node, which re-derives the terminal state from disk independently.
#
# stdout: single-line JSON. Logs to stderr. rc 2 only on wrapper-level
# breakage (missing scripts / missing argument); the decision path always
# prints valid JSON and exits 0.
set -euo pipefail

ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (run_gate.sh)}"
TARGET=""
MAXR="5"
STALL="2"
while [ $# -gt 0 ]; do
  case "$1" in
    --latency-reduction-min) TARGET="${2:?--latency-reduction-min needs a value}"; shift 2 ;;
    --max-rounds)      MAXR="${2:?--max-rounds needs a value}"; shift 2 ;;
    --stall-rounds)    STALL="${2:?--stall-rounds needs a value}"; shift 2 ;;
    *) echo "FATAL: unknown arg $1" >&2; exit 2 ;;
  esac
done
[ -n "$TARGET" ] || { echo "FATAL: --latency-reduction-min is required" >&2; exit 2; }

for f in gate_decide.py emit_result.py; do
  [ -f "$ART/scripts/$f" ] || { echo "FATAL: $ART/scripts/$f not deployed — entry stage incomplete" >&2; exit 2; }
done

rc=0
OUT="$(python3 "$ART/scripts/gate_decide.py" \
        --artifacts "$ART" \
        --latency-reduction-min "$TARGET" \
        --max-rounds "$MAXR" \
        --stall-rounds "$STALL")" || rc=$?

if [ "$rc" -eq 0 ]; then
  python3 "$ART/scripts/emit_result.py" --json "$OUT" --field 'error='
else
  echo "run_gate: gate_decide exited rc=$rc — root cause on stderr above; emitting terminal decision" >&2
  python3 "$ART/scripts/emit_result.py" \
    --field decision=finish-failed \
    --field round=0 \
    --field stall=0 \
    --field best=null \
    --field reason="gate decision script failed" \
    --field "error=gate_decide exited rc=$rc (see stderr in the run log)"
fi
