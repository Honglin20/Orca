#!/usr/bin/env bash
# precheck.sh — ns3_run_search Step 1 pre-checks.
# Clears stale markers + gates on run_search_supernet.sh presence.
set -uo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

# Clear stale markers from prior runs (idempotency).
rm -f .ns_run_search_healed.txt .ns_run_search_fidelity.flag .ns_run_search_assessment.txt

if [ ! -f run_search_supernet.sh ]; then
  printf "FATAL: run_search_supernet.sh absent — ns3_search_pipeline did not produce it." \
    > .ns_run_search_assessment.txt
  echo "GATE: run_search_supernet.sh absent -> cannot proceed"
  # Step 3 python will judge status=failed (script absent + no results).
else
  echo "GATE: run_search_supernet.sh exists -> proceed to search"
fi
