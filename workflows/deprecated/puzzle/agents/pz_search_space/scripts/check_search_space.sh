#!/usr/bin/env bash
# check_search_space.sh — deterministic gate wrapper for pz_search_space.
# Delegates to check_search_space.py which loads search_space.yaml via
# search_space_io.load_search_space_yaml (schema + kind legal + path unique +
# catalog registration + identity mandatory per kind) and additionally verifies
# the transformer_layer-specific must_extract fields.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"
AGENT_RESOURCES="${ORCA_AGENT_RESOURCES:-$SCRIPT_DIR/..}"
WF_ROOT="${ORCA_WORKFLOWS_ROOT:-}"

if [ -z "$WF_ROOT" ]; then
  echo "FAIL: ORCA_WORKFLOWS_ROOT must be set (host prompt injects it)" >&2
  exit 1
fi

CONTRACT_OUT="$(python3 "$SCRIPT_DIR/check_search_space.py" \
  --artifacts-dir "$ARTIFACTS_DIR" \
  --scripts-dir "$WF_ROOT/agents/_puzzle_scripts" \
  --agent-resources "$AGENT_RESOURCES" 2>&1)"
CONTRACT_RC=$?

echo "$CONTRACT_OUT" | sed 's/^/[check_search_space] /'

if [ "$CONTRACT_RC" -eq 0 ]; then
  echo "[check_search_space] result: PASS"
  exit 0
fi
echo "[check_search_space] result: FAIL"
exit 1
