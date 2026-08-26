#!/usr/bin/env bash
# reuse_check.sh — pz_search_space Step 0 soft-skip gate.
# Prints REUSE_VALID when search_space.yaml exists, parses, has ≥1 slot, and
# the check_search_space.py contract gate passes. Otherwise prints nothing and
# the agent runs Step 1.
set +e
cd "$ORCA_ARTIFACTS_DIR" 2>/dev/null || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

[ -s "search_space.yaml" ] || exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_RESOURCES="${ORCA_AGENT_RESOURCES:-$SCRIPT_DIR/..}"
WF_ROOT="${ORCA_WORKFLOWS_ROOT:-}"
[ -z "$WF_ROOT" ] && exit 0

# Empty slots (unsupported branch) does NOT count as REUSE_VALID — re-judge fresh.
SLOT_COUNT="$(python3 - <<'PY' 2>/dev/null
import yaml
d = yaml.safe_load(open("search_space.yaml", encoding="utf-8"))
slots = d.get("slots") if isinstance(d, dict) else None
print(len(slots) if isinstance(slots, list) else 0)
PY
)"
[ -n "$SLOT_COUNT" ] || exit 0
[ "$SLOT_COUNT" -ge 1 ] || exit 0

# Contract gate must pass on the existing declaration.
python3 "$SCRIPT_DIR/check_search_space.py" \
  --artifacts-dir "$ORCA_ARTIFACTS_DIR" \
  --scripts-dir "$WF_ROOT/agents/_puzzle_scripts" \
  --agent-resources "$AGENT_RESOURCES" >/dev/null 2>&1 || exit 0

echo "REUSE_VALID"
