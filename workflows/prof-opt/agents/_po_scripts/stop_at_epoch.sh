#!/usr/bin/env bash
# stop_at_epoch.sh — idempotent single check: stop a detached variant training
# at epoch k (process-group kill), or report its natural termination.
#
# Called repeatedly by the po_probe bounded-poll loop (interval <= 30s); each
# invocation is ONE check — it never waits for the epoch itself. The variant
# trains under the FULL rendered epoch count; this script watches the training
# log and kills the worker's process group the moment epoch >= k first appears.
#
# Single-source epoch parsing (E3-06): the epoch regex comes ONLY from
# contracts.json via the deployed metric_curve module (_contract_pattern +
# _extract) — this script accepts --contract, never --pattern, and never
# reimplements the parsing, so the pattern cannot drift between extraction
# and stopping.
#
# Lifecycle (stop_status.json next to the pid file is the terminal record):
#   - max epoch < k, worker group alive      -> {"status": "waiting"} (caller
#     keeps polling); a log that does not exist yet is also waiting (epoch 0)
#   - max epoch >= k first seen              -> /proc cmdline attribution
#     check -> kill -TERM -<pid> (process group) -> 10s grace -> KILL if the
#     group survives -> re-parse the now-frozen log -> stop_status.json
#     {"status": "killed", "stopped_at_epoch": <max complete epoch at terminal
#     time, >= k — NOT always k: lines written between the kill decision and
#     the group's death count>, "rc": null}
#   - worker ended naturally (pid dead, rc file present) -> stop_status.json
#     {"status": "natural_done", "stopped_at_epoch": <parsed>, "rc": <rc>,
#      "monitor_failed": true iff parsed epochs > k — the kill missed, the
#      run went past the probe depth}
#   - stop_status.json already present        -> printed verbatim, exit 0
#     (idempotent: a second call after a kill never kills again)
#
# rc priority: stop_status wins over the rc file — a killed run records
# rc: null even if the wrapper managed to write an rc in the race window.
#
# stdout: ALWAYS exactly one JSON line (the current or terminal status).
# Hard errors (bad args, unreadable contract, pid attribution mismatch,
# worker died without rc or stop_status) -> exit 2 with a FATAL line.
#
# Usage:
#   stop_at_epoch.sh --log LOG --contract contracts.json --stop-epoch K \
#                    --pid-file PIDFILE [--expect SUBSTR]
# --expect: substring the group leader's /proc cmdline must contain before
#   any kill (default "train.rendered.sh"); a pid reused by an unrelated
#   process fails the attribution check instead of being killed.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LOG=""; CONTRACT=""; STOP_EPOCH=""; PID_FILE=""; EXPECT="train.rendered.sh"
while [ $# -gt 0 ]; do
  case "$1" in
    --log)        LOG="${2:?}"; shift 2 ;;
    --contract)   CONTRACT="${2:?}"; shift 2 ;;
    --stop-epoch) STOP_EPOCH="${2:?}"; shift 2 ;;
    --pid-file)   PID_FILE="${2:?}"; shift 2 ;;
    --expect)     EXPECT="${2:?}"; shift 2 ;;
    # --pattern is deliberately ABSENT: the regex comes only from the contract
    # (single source with metric_curve extract). Reject it with a pointed
    # message so nobody "fixes" a mismatch by re-specifying the pattern here.
    --pattern) echo "FATAL: --pattern is not accepted — the epoch regex comes from --contract (single source with metric_curve extract)" >&2; exit 2 ;;
    *) echo "FATAL: unknown argument $1" >&2; exit 2 ;;
  esac
done
[ -n "$LOG" ] && [ -n "$CONTRACT" ] && [ -n "$STOP_EPOCH" ] && [ -n "$PID_FILE" ] \
  || { echo "FATAL: --log, --contract, --stop-epoch and --pid-file are all required" >&2; exit 2; }
case "$STOP_EPOCH" in (*[!0-9]*|'') echo "FATAL: --stop-epoch must be a positive integer, got '$STOP_EPOCH'" >&2; exit 2 ;; esac
[ "$STOP_EPOCH" -ge 1 ] || { echo "FATAL: --stop-epoch must be >= 1" >&2; exit 2; }
[ -f "$CONTRACT" ] || { echo "FATAL: contract not found: $CONTRACT" >&2; exit 2; }

STATUS_FILE="$(dirname "$PID_FILE")/stop_status.json"

# idempotent: a terminal record already on disk is replayed verbatim
if [ -f "$STATUS_FILE" ]; then
  python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), sort_keys=True))' \
    "$STATUS_FILE" && exit 0
fi

# max complete epoch in the log, via the SAME contract-reading + parsing
# implementation metric_curve extract uses (E3-06). Prints 0 for BOTH early
# states (log not created yet, or created but with no matching line yet —
# the redirect creates the file before the first epoch line lands). Any other
# failure exits 2 with metric_curve's OWN error surface ("metric_curve: FAIL
# ..."), identical to what `metric_curve extract` would print for the same
# contract/log — one error surface, not two.
max_epoch() { # max_epoch <log>
  python3 - "$SCRIPT_DIR" "$CONTRACT" "$1" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import metric_curve

log = Path(sys.argv[3])
if not log.is_file():
    print(0)
    raise SystemExit(0)
try:
    pattern = metric_curve._contract_pattern(Path(sys.argv[2]))
    points = metric_curve._extract(log, pattern)
except metric_curve.MetricCurveError as exc:
    if "no epoch metric matched" in str(exc):
        print(0)  # empty early-state log — a waiting state, not an error
        raise SystemExit(0)
    print(f"metric_curve: FAIL {exc}", file=sys.stderr)
    raise SystemExit(2) from exc
print(max(int(p["epoch"]) for p in points))
PY
}

pid_from_file() { cat "$PID_FILE" 2>/dev/null || echo 0; }

group_alive() { # group_alive <pid>
  [ "$1" -gt 0 ] 2>/dev/null && kill -0 "-$1" 2>/dev/null
}

cmdline_matches() { # cmdline_matches <pid> <expect-substr>
  local cmd
  cmd="$(tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null)" || return 1
  [ -n "$cmd" ] || return 1
  case "$cmd" in *"$2"*) return 0 ;; *) return 1 ;; esac
}

ME="$(max_epoch "$LOG")"; rc=$?
if [ $rc -ne 0 ]; then
  # metric_curve's own error text already went to stderr via the traceback;
  # surface it as this script's fail-loud exit (same error surface, E3-06)
  exit 2
fi

PID="$(pid_from_file)"
RC_FILE="$(dirname "$PID_FILE")/rc"

worker_ended_naturally() { # pid dead AND the wrapper wrote its rc
  ! group_alive "$PID" && [ -s "$RC_FILE" ]
}

# ── natural termination ───────────────────────────────────────────────────────
if worker_ended_naturally; then
  WORKER_RC="$(cat "$RC_FILE")"
  MONITOR_FAILED="false"
  [ "$ME" -gt "$STOP_EPOCH" ] && MONITOR_FAILED="true"
  python3 - "$STATUS_FILE" "$ME" "$WORKER_RC" "$MONITOR_FAILED" <<'PY'
import json, os, sys, tempfile
status = {"status": "natural_done", "stopped_at_epoch": int(sys.argv[2]),
          "rc": int(sys.argv[3]), "monitor_failed": sys.argv[4] == "true"}
path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(status, fh, sort_keys=True)
os.replace(tmp, path)
print(json.dumps(status, sort_keys=True))
PY
  exit 0
fi

# ── below the stop depth: keep waiting (only while the group is alive) ───────
if [ "$ME" -lt "$STOP_EPOCH" ]; then
  if group_alive "$PID"; then
    echo "{\"status\": \"waiting\", \"max_epoch\": $ME}"
    exit 0
  fi
  echo "FATAL: worker group is dead before epoch $STOP_EPOCH appeared (max epoch $ME) and no rc file exists at $RC_FILE — the run crashed without a terminal state" >&2
  exit 2
fi

# ── epoch >= k first seen: attribute, then kill the process group ────────────
if ! group_alive "$PID"; then
  # raced: the worker ended between the parse and this check — the rc branch
  # above should have caught it unless rc never landed
  echo "FATAL: worker group died around epoch $STOP_EPOCH without an rc file at $RC_FILE — no terminal state to report" >&2
  exit 2
fi
if ! cmdline_matches "$PID" "$EXPECT"; then
  echo "FATAL: refusing to kill pid $PID — /proc cmdline does not reference '$EXPECT' (pid reuse or wrong pid file: $PID_FILE)" >&2
  exit 2
fi

echo "[stop_at_epoch] epoch $ME >= $STOP_EPOCH: TERM process group $PID" >&2
kill -TERM "-$PID" 2>/dev/null || true

# 10s grace, then KILL the whole group
grace=0
while group_alive "$PID" && [ "$grace" -lt 10 ]; do
  sleep 1; grace=$((grace + 1))
done
if group_alive "$PID"; then
  echo "[stop_at_epoch] group survived ${grace}s grace — KILL process group $PID" >&2
  kill -KILL "-$PID" 2>/dev/null || true
  grace=0
  while group_alive "$PID" && [ "$grace" -lt 5 ]; do
    sleep 1; grace=$((grace + 1))
  done
fi

# the log is frozen now — re-parse for the TERMINAL depth (lines written
# between the kill decision and the group's death are real trained epochs;
# reporting k instead of the parsed depth would understate the comparison)
FROZEN_ME="$(max_epoch "$LOG")"; rc=$?
if [ $rc -ne 0 ]; then
  echo "FATAL: re-parsing the frozen log $LOG after killing group $PID failed" >&2
  exit 2
fi
if [ "$FROZEN_ME" -lt "$STOP_EPOCH" ]; then
  echo "FATAL: frozen log re-parse found max epoch $FROZEN_ME < stop epoch $STOP_EPOCH — the pre-kill parse saw $ME; inconsistent log state" >&2
  exit 2
fi

python3 - "$STATUS_FILE" "$FROZEN_ME" <<'PY'
import json, os, sys, tempfile
status = {"status": "killed", "stopped_at_epoch": int(sys.argv[2]), "rc": None}
path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(status, fh, sort_keys=True)
os.replace(tmp, path)
print(json.dumps(status, sort_keys=True))
PY
