#!/usr/bin/env bash
# reuse_check.sh — ns3_flatten Step 0 reuse gate.
# REUSE iff a *_flat.py / *_llm-optimized.py file exists and execs cleanly AND project_manifest.md exists.
set -euo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

FLAT_OK=false
for f in *_flat.py *_llm-optimized.py; do
  [ -s "$f" ] || continue
  if python3 -c "
import ast, sys
src = open(sys.argv[1]).read()
ast.parse(src)
mod = compile(src, sys.argv[1], 'exec')
ns = {}
exec(mod, ns)
print('FLAT_VALID')
" "$f" 2>/dev/null | grep -q FLAT_VALID; then
    FLAT_OK=true
    break
  fi
done

if [ "$FLAT_OK" != true ]; then
  echo "NO_REUSE: no valid *_flat.py / *_llm-optimized.py" >&2
  exit 1
fi

if [ ! -s "project_manifest.md" ]; then
  echo "NO_REUSE: project_manifest.md missing" >&2
  exit 1
fi

echo "REUSE: flat/optimized + manifest exist and pass → skip Steps 1-3, go straight to emitting the output JSON"
