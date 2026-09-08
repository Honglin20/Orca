#!/usr/bin/env bash
# reuse_check.sh — psu_flatten Step 0 reuse gate.
# REUSE iff a *_flat.py / *_llm-optimized.py file exists and execs cleanly AND
# project_manifest.md AND load_pretrained.py exist (this node's three authoritative artifacts).
set -euo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

FLAT_OK=false
for f in *_flat.py *_llm-optimized.py; do
  [ -s "$f" ] || continue
  if python3 -c "
import ast, sys, types
src = open(sys.argv[1]).read()
ast.parse(src)
probe = types.ModuleType('flat_reuse_probe')
probe.__dict__['__file__'] = sys.argv[1]
sys.modules['flat_reuse_probe'] = probe
exec(compile(src, sys.argv[1], 'exec'), probe.__dict__)
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

if [ ! -s "load_pretrained.py" ]; then
  echo "NO_REUSE: load_pretrained.py missing" >&2
  exit 1
fi

# Stale-loader guard: the loader must still run clean, and its pinned PRETRAINED_CKPT
# must match this run's input (optional $1; agent.md Step 0 passes the input path).
if ! python3 load_pretrained.py >/dev/null 2>&1; then
  echo "NO_REUSE: load_pretrained.py smoke failed (stale loader / moved ckpt)" >&2
  exit 1
fi
if [ -n "${1:-}" ] && ! python3 -c "
import sys
from pathlib import Path
import load_pretrained
sys.exit(0 if Path(load_pretrained.PRETRAINED_CKPT).resolve() == Path(sys.argv[1]).resolve() else 1)
" "$1" 2>/dev/null; then
  echo "NO_REUSE: load_pretrained.PRETRAINED_CKPT != this run's pretrained_ckpt input" >&2
  exit 1
fi

echo "REUSE: flat/optimized + manifest + load_pretrained exist and pass → skip Steps 1-4, go straight to emitting the output JSON"
