#!/usr/bin/env bash
# reuse_check.sh — psu_search_pipeline Step 0 reuse gate.
# REUSE iff all four artifacts exist, the three .py files parse, and search_config.yaml is valid YAML.
set -euo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

MISSING=""
for f in select_architecture.py search_config.yaml evaluator.py arch_codec.py; do
  [ -s "$f" ] || MISSING="$MISSING $f"
done

if [ -n "$MISSING" ]; then
  echo "NO_REUSE: missing:$MISSING" >&2
  exit 1
fi

if python3 -c "
import ast, yaml, sys
for p in ('select_architecture.py', 'evaluator.py', 'arch_codec.py'):
    ast.parse(open(p).read())
yaml.safe_load(open('search_config.yaml'))
print('PIPELINE_VALID')
" 2>/dev/null | grep -q PIPELINE_VALID; then
  echo "REUSE: all four search pipeline artifacts exist and pass validation → skip Steps 1-3, proceed directly to emitting the output JSON"
  exit 0
fi

echo "NO_REUSE: artifacts below the bar" >&2
exit 1
