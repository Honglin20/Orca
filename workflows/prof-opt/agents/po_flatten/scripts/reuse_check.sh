#!/usr/bin/env bash
# reuse_check.sh — po_flatten reuse gate (idempotent entry).
#
# Verifies, in order:
#   1. single-writer lock: no OTHER live run owns this workspace
#      (.run_lock = {run_id, pid, ts} heartbeat; "live" = pid alive OR
#      heartbeat younger than LOCK_STALE_S). Our own run_id always refreshes.
#   2. fresh_start=1 -> wipe the ENTIRE reusable workspace (everything under
#      $ORCA_ARTIFACTS_DIR except .run_lock) and report NO_REUSE so the node
#      rebuilds from scratch.
#   3. BASELINE.lock matches the current key inputs (structural anchor:
#      model_path + pretrained_ckpt[optional] + shadow *.py checksums + ckpt
#      checksum when a ckpt is anchored). Failure -> exit 3, two distinguishable
#      states: unreadable/corrupt lock = REAL error; readable-but-mismatched =
#      usually design behavior on a workspace with a promotion history (the
#      shadow moved forward, the lock still anchors the original baseline —
#      cross-run reuse holds only for zero-promotion workspaces) -> fresh_start.
#   4. shadow tree + project_manifest.md + readiness/readiness.json exist and
#      readiness is all-pass -> REUSE (skip the workflow steps).
#
# Also re-validates the PROFILING MODE on the reuse path (only): the mode is
# re-resolved once (read-only) and compared with the recorded
# profile_mode.json over the measurement-config fields {mode, chip,
# precision, core_num}. Any difference — or a missing profile_mode.json —
# exits 2: cycles measured under a different configuration cannot be
# compared across runs (fresh_start=true rebuilds the workspace).
#
# Exit codes: 0 = REUSE (skip steps)
#             1 = NO_REUSE (run the steps; also returned after fresh-start wipe)
#             2 = hard environment error (incl. profiling-mode drift on reuse)
#             3 = fail-loud conflict (live other run / baseline-lock
#                 mismatch) -> flatten_passed=false
#
# Usage: reuse_check.sh <model_path> <pretrained_ckpt> <fresh_start:0|1>
#   (<pretrained_ckpt> may be empty: reference-only, recorded in the lock only
#    when provided)
set -euo pipefail

MODEL_PATH="${1:?usage: reuse_check.sh <model_path> <pretrained_ckpt> <fresh_start:0|1>}"
CKPT="${2-}"
FRESH_START="${3:-0}"
[ $# -le 3 ] || { echo "FATAL: unexpected extra argument(s): $* (usage: reuse_check.sh <model_path> <pretrained_ckpt> <fresh_start:0|1>)" >&2; exit 2; }

ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (reuse_check.sh)}"
RUN_ID="${ORCA_RUN_ID:-unknown-run}"
LOCK_STALE_S=1800   # 30 min heartbeat grace

cd "$ART" || { echo "FATAL: artifacts dir unreachable: $ART" >&2; exit 2; }

# ── helpers ──────────────────────────────────────────────────────────────────
file_mtime() { # seconds since epoch, 0 when unreadable
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0
}

heartbeat() { # (re)write the lock with this execution's identity
  # a lock we cannot write means no single-writer guarantee — hard error (2),
  # never rc 1 (which this script's caller maps to NO_REUSE / rebuild)
  printf '{"run_id": "%s", "pid": %s, "ts": %s}\n' \
    "$RUN_ID" "$$" "$(date +%s)" > "$ART/.run_lock.tmp.$$" \
    && mv -f "$ART/.run_lock.tmp.$$" "$ART/.run_lock" \
    || { echo "FATAL: cannot refresh .run_lock heartbeat in $ART" >&2; return 2; }
}

# ── 1. single-writer lock ────────────────────────────────────────────────────
if [ -f "$ART/.run_lock" ]; then
  LOCK_JSON="$(python3 - "$ART/.run_lock" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    print(d.get("run_id", ""), int(d.get("pid", 0) or 0))
except Exception as exc:  # bad lock -> treat as absent (will be overwritten)
    print(f"BAD_LOCK:{exc}", 0)
PY
)"
  LOCK_RUN_ID="${LOCK_JSON%% *}"
  LOCK_PID="${LOCK_JSON##* }"
  if [[ "$LOCK_RUN_ID" == BAD_LOCK:* ]]; then
    echo "WARN: unreadable .run_lock, overwriting: $LOCK_RUN_ID" >&2
  elif [ "$LOCK_RUN_ID" != "$RUN_ID" ]; then
    ALIVE=0
    if [ "$LOCK_PID" -gt 0 ] && kill -0 "$LOCK_PID" 2>/dev/null; then
      ALIVE=1
    fi
    AGE=$(( $(date +%s) - $(file_mtime "$ART/.run_lock") ))
    if [ "$ALIVE" -eq 1 ] || [ "$AGE" -lt "$LOCK_STALE_S" ]; then
      echo "FATAL: workspace owned by another live run (run_id=$LOCK_RUN_ID pid=$LOCK_PID alive=$ALIVE heartbeat_age=${AGE}s < ${LOCK_STALE_S}s). Concurrent runs on one workspace are not supported — wait for it to finish or use a different project directory." >&2
      exit 3
    fi
    echo "stale lock from run $LOCK_RUN_ID (pid dead, heartbeat ${AGE}s old) — taking over" >&2
  fi
fi
heartbeat

# ── 2. fresh start wipe ──────────────────────────────────────────────────────
# Normalize before judging: the caller renders 1/0, but tolerate true/false/yes
# in any case — a literal-rendered boolean must never silently mean "0".
case "$(printf '%s' "$FRESH_START" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes) FRESH_START=1 ;;
  *) FRESH_START=0 ;;
esac
if [ "$FRESH_START" = "1" ]; then
  # Wipe the ENTIRE reusable workspace, not a pinned path list: anything left
  # behind by an older run — including files from a paradigm this version no
  # longer knows — would silently false-gate the rebuild checks below. The
  # only survivor is .run_lock: the single-writer lock belongs to THIS run
  # (heartbeat refreshed at the top; the validation gate refreshes it again),
  # so it is never wiped and never needs rebuilding by anyone else.
  # A failed wipe MUST exit 2 (hard error): under set -e a bare failure would
  # exit 1, which the caller maps to NO_REUSE — a HALF-wiped workspace must
  # never continue as if it were a clean rebuild.
  if ! find "$ART" -mindepth 1 -maxdepth 1 ! -name '.run_lock' -exec rm -rf {} +; then
    echo "FATAL: fresh-start wipe failed in $ART (permissions / read-only mount?) — workspace is possibly half-wiped, NOT continuing" >&2
    exit 2
  fi
  echo "NO_REUSE: fresh_start=1 wiped the whole reusable workspace (only .run_lock kept) — rebuild everything" >&2
  exit 1
fi

# ── 3. BASELINE.lock match ───────────────────────────────────────────────────
if [ ! -f "$ART/BASELINE.lock" ]; then
  echo "NO_REUSE: no BASELINE.lock (first run)" >&2
  exit 1
fi

LOCK_VERDICT="$(python3 - "$ART/BASELINE.lock" "$MODEL_PATH" "$CKPT" <<'PY'
import hashlib, json, sys
from pathlib import Path

lock_path, model_path, ckpt_raw = sys.argv[1], sys.argv[2], sys.argv[3]
ckpt = Path(ckpt_raw) if ckpt_raw else None  # Path("") would normalize to "."

def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

try:
    lock = json.loads(Path(lock_path).read_text(encoding="utf-8"))
    # a parsable but structurally corrupt lock (top level not an object /
    # py_files_sha256 not a mapping) would crash the comparisons below or
    # misclassify as a mismatch — it is the same real-error state.
    # key-missing / non-str values deliberately fall through to mismatch:
    # fresh_start rebuild is the right recovery there too
    if not isinstance(lock, dict) or not isinstance(lock.get("py_files_sha256", {}), dict):
        print(json.dumps({"match": False, "unreadable": True,
                          "why": "BASELINE.lock corrupt: not a valid anchor object"}))
        sys.exit(0)
except Exception as exc:
    # discriminator: the lock EXISTS but cannot be read/parsed (real error),
    # as opposed to a readable lock whose content no longer matches
    print(json.dumps({"match": False, "unreadable": True,
                      "why": f"BASELINE.lock unreadable: {exc}"}))
    sys.exit(0)

why = []
if lock.get("model_path") != model_path:
    why.append(f"model_path changed: lock={lock.get('model_path')!r} now={model_path!r}")
if ckpt is None:
    # no reference checkpoint provided now: the lock must carry the empty anchor
    if lock.get("pretrained_ckpt") or lock.get("ckpt_sha256"):
        why.append("pretrained_ckpt changed: lock anchors a ckpt but none is provided now")
else:
    if not ckpt.is_file():
        print(json.dumps({"match": False, "why": [f"pretrained_ckpt missing: {ckpt}"]}))
        sys.exit(0)
    ckpt_resolved = str(ckpt.resolve())
    if lock.get("pretrained_ckpt") != ckpt_resolved:
        why.append(f"pretrained_ckpt changed: lock={lock.get('pretrained_ckpt')!r} now={ckpt_resolved!r}")
    elif lock.get("ckpt_sha256") != sha(ckpt):
        why.append("pretrained_ckpt file content changed (sha256 mismatch)")

shadow = Path(lock_path).parent / "shadow"
actual_py = {}
if shadow.is_dir():
    for p in sorted(shadow.rglob("*.py")):
        rel = str(p.relative_to(shadow)).replace("\\", "/")
        actual_py[rel] = sha(p)
if lock.get("py_files_sha256") != actual_py:
    changed = sorted(set(lock.get("py_files_sha256", {})) ^ set(actual_py))
    why.append(f"shadow *.py checksums differ (added/removed paths: {changed}); "
               f"content drift possible — rerun from scratch")

print(json.dumps({"match": not why, "why": why}))
PY
)" || { echo "FATAL: lock verification crashed" >&2; exit 2; }

if [[ "$LOCK_VERDICT" == *'"unreadable": true'* ]]; then
  # state 1 — the lock EXISTS but cannot be read/parsed: a REAL error (corrupt
  # or unreadable lock), not a reuse decision. Never dress it up as a mismatch
  # (the fresh_start hint would point away from the actual fault).
  echo "FATAL: BASELINE.lock exists but is unreadable/corrupt — this is a real error, not a reuse mismatch. $LOCK_VERDICT" >&2
  exit 3
fi
if [[ "$LOCK_VERDICT" != *'"match": true'* ]]; then
  echo "FATAL: BASELINE.lock does not match current key inputs. $LOCK_VERDICT" >&2
  echo "HINT: most often this is design behavior on a workspace with a promotion history: after a round advanced, the shadow tree moved forward while BASELINE.lock still anchors the original baseline — cross-run reuse only holds for a workspace with zero promotions (or the model/ckpt/shadow anchor truly changed). Re-run with fresh_start=true to rebuild the workspace from scratch." >&2
  exit 3
fi

# ── 3b. profiling-mode consistency (reuse path only) ─────────────────────────
# Reached only with a matching lock (first-run NO_REUSE and fresh_start wipe
# both exited above). Re-resolve the mode READ-ONLY and compare the
# measurement-config four-field set with the recorded profile_mode.json —
# the existing file is never touched here. `resolved_by` is provenance only
# and deliberately NOT compared (an env->npu-smi source flip on the same
# hardware is a measurement-equivalent configuration, not drift).
PO_SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/_po_scripts"
[ -f "$PO_SCRIPTS/resolve_profile_mode.sh" ] || {
  echo "FATAL: shared tooling not found at $PO_SCRIPTS/resolve_profile_mode.sh (expected <agents root>/_po_scripts)" >&2
  exit 2; }
MODE_FILE="$ART/profile_mode.json"
if [ ! -s "$MODE_FILE" ]; then
  echo "FATAL: profile_mode.json missing under $ART — the workspace predates mode resolution or was half-built; cycles comparisons across runs are invalid without it. Re-run with fresh_start=true to rebuild the workspace." >&2
  exit 2
fi
RESOLVED_NOW="$(bash "$PO_SCRIPTS/resolve_profile_mode.sh" --stdout-only)" || {
  echo "FATAL: profiling-mode re-resolution failed while checking reuse consistency (see stderr above)" >&2
  exit 2; }
MODE_OK="$(python3 - "$MODE_FILE" "$RESOLVED_NOW" <<'PY'
import json, sys

COMPARE = ("mode", "chip", "precision", "core_num")
try:
    recorded = json.loads(open(sys.argv[1], encoding="utf-8").read())
    now = json.loads(sys.argv[2])
except Exception as exc:
    print(f"BAD:{exc}")
    raise SystemExit(0)
diff = {k: (recorded.get(k, "<absent>"), now.get(k, "<absent>"))
        for k in COMPARE if recorded.get(k, "<absent>") != now.get(k, "<absent>")}
print("OK" if not diff else f"DRIFT:{json.dumps(diff, ensure_ascii=False)}")
PY
)"
if [[ "$MODE_OK" == BAD:* || "$MODE_OK" == DRIFT:* ]]; then
  echo "FATAL: profiling-mode mismatch on workspace reuse ($MODE_OK) — cycles measured under a different configuration cannot be compared across runs; re-run with fresh_start=true to rebuild the workspace." >&2
  exit 2
fi

# ── 4. reuse products ────────────────────────────────────────────────────────
TOP_COUNT=0
if [ -d "$ART/shadow" ]; then
  TOP_COUNT="$(find "$ART/shadow" -mindepth 1 -maxdepth 1 | wc -l)"
fi
if [ ! -d "$ART/shadow" ] || [ "$TOP_COUNT" -eq 0 ]; then
  echo "NO_REUSE: shadow tree missing or empty" >&2
  exit 1
fi
if [ ! -s "$ART/project_manifest.md" ]; then
  echo "NO_REUSE: project_manifest.md missing" >&2
  exit 1
fi
if ! python3 - "$ART/readiness/readiness.json" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
if not p.is_file():
    print("NO_REUSE: readiness/readiness.json missing", file=sys.stderr)
    sys.exit(1)
d = json.loads(p.read_text(encoding="utf-8"))
ok = all(d.get(k) is True for k in
         ("constructible", "exportable", "pretrained_loadable", "definition_located"))
if not ok:
    print(f"NO_REUSE: readiness not all-pass: {d}", file=sys.stderr)
    sys.exit(1)
PY
then
  exit 1
fi

echo "REUSE: BASELINE.lock matches + shadow/manifest/readiness all present — skip the workflow steps, emit from existing artifacts"
