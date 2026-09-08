#!/usr/bin/env bash
# resume_guard.sh — psu_run_search Step R resume guard.
# Detects whether the search is running (RESUME_SEARCH) or dead/not started (RESUME_HEAL).
# Each branch computes N independently (never a one-size-fits-all max+1).
set -uo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

SPID="$(cat runs/search/.search_pid 2>/dev/null || echo '')"
if [ -n "$SPID" ] && kill -0 "$SPID" 2>/dev/null; then
  # ── Branch A: RESUME_SEARCH (search running) ── N = latest-mtime log number (Step 2b uses it immediately)
  N=$(ls -t runs/search/search.attempt*.stdout.log 2>/dev/null | head -1 \
    | sed -n 's/.*attempt\([0-9]*\)\.stdout\.log/\1/p')
  N=${N:-1}
  echo "RESUME_SEARCH pid=$SPID attempt=$N search is running, go straight to Step 2b polling (no detach, no marker cleanup, no reuse-check)"
else
  # ── Branch B: RESUME_HEAL (search dead/not started) ── N = max(existing number)+1 (Step 2a re-detach will not overwrite existing logs)
  LAST_N=$(ls runs/search/search.attempt*.stdout.log 2>/dev/null \
    | sed -n 's/.*attempt\([0-9]*\)\.stdout\.log/\1/p' | sort -n | tail -1)
  N=$(( ${LAST_N:-0} + 1 ))
  echo "RESUME_HEAL new_attempt=$N search not running, normal Step 0 → Step 1 → Step 2a re-detach"
fi
