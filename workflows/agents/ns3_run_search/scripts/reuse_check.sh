#!/usr/bin/env bash
# reuse_check.sh — ns3_run_search Step 0 reuse gate.
# REUSE iff search_results.jsonl is non-empty + search process dead + .search_rc exists + every line is valid JSON.
# On REUSE, clears stale markers + pushes the 3 search charts (fail-soft).
set -uo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"
LATENCY_UNIT="${1:-ms}"

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

RESULTS="$ARTIFACTS_DIR/search_results.jsonl"
# reuse requires all three: jsonl non-empty + search process dead + rc file exists (search really finished, not an incremental mid-flight write).
SPID="$(cat runs/search/.search_pid 2>/dev/null || echo '')"

if [ -s "$RESULTS" ] && { [ -z "$SPID" ] || ! kill -0 "$SPID" 2>/dev/null; } && [ -f runs/search/.search_rc ]; then
  # verify it meets the bar: every line is valid JSON (python json.loads verifies ≥1 valid line)
  if python3 -c "
import json, sys
n = 0
with open(sys.argv[1], 'r', encoding='utf-8', errors='replace') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        json.loads(line)   # raise on invalid
        n += 1
assert n >= 1, 'no valid records'
print('RESULTS_VALID')
" "$RESULTS" 2>/dev/null | grep -q RESULTS_VALID; then
    # clear stale markers (rm-only; emit_result.py defaults read_text to "false" / read_lines to [] for missing files).
    rm -f .ns_run_search_healed.txt .ns_run_search_fidelity.flag
    printf 'reused existing search_results.jsonl: %s' "$RESULTS" > .ns_run_search_assessment.txt
    # reuse also pushes the search 3 charts, otherwise the frontend never sees the Pareto / search table / latency distribution.
    python3 "$SCRIPTS_DIR/pareto.py" --artifacts-dir "$ARTIFACTS_DIR" --latency-unit "$LATENCY_UNIT" > /dev/null || true
    python3 "$SCRIPTS_DIR/search_table.py" --artifacts-dir "$ARTIFACTS_DIR" --latency-unit "$LATENCY_UNIT" > /dev/null || true
    python3 "$SCRIPTS_DIR/latency_dist.py" --artifacts-dir "$ARTIFACTS_DIR" --latency-unit "$LATENCY_UNIT" > /dev/null || true
    echo "REUSE: search_results.jsonl exists and meets the bar → skip search redo, pushed 3 charts → proceed to Step 2.8 select → Step 3"
    exit 0
  fi
fi

echo "NO_REUSE: search_results.jsonl absent / search alive / below the bar" >&2
exit 1
