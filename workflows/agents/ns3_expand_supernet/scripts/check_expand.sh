#!/usr/bin/env bash
# check_expand.sh — deterministic gate for ns3_expand_supernet artifacts.
# Checks: supernet.py exec exposes SearchSpace/build_supernet + no user-pkg imports.
set -euo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"
FAIL=0

echo "[check_expand] artifacts_dir=$ARTIFACTS_DIR"

SUPERNET="$ARTIFACTS_DIR/supernet.py"
if [ ! -s "$SUPERNET" ]; then
  echo "FAIL: supernet.py missing or empty"
  exit 1
fi

# ── 1. ast.parse (syntax valid) ─────────────────────────────────────────
python3 -c "
import ast, sys
ast.parse(open(sys.argv[1]).read())
print('AST_OK')
" "$SUPERNET" 2>/dev/null | grep -q AST_OK || {
  echo "FAIL: supernet.py has syntax errors"
  exit 1
}

# ── 2. exec exposes SearchSpace or build_supernet ───────────────────────
python3 -c "
import ast, sys
src = open(sys.argv[1]).read()
mod = compile(src, sys.argv[1], 'exec')
ns = {}
exec(mod, ns)
assert 'SearchSpace' in ns or 'build_supernet' in ns, 'no SearchSpace/build_supernet'
print('SUPERNET_VALID')
" "$SUPERNET" 2>/dev/null | grep -q SUPERNET_VALID || {
  echo "FAIL: supernet.py does not expose SearchSpace/build_supernet"
  exit 1
}
echo "[check_expand] supernet.py exposes SearchSpace/build_supernet OK"

# ── 3. No local user-pkg imports (.user_pkg marker) ─────────────────────
USER_PKG_FILE="$ARTIFACTS_DIR/.user_pkg"
if [ -s "$USER_PKG_FILE" ]; then
  while IFS= read -r pkg; do
    [ -n "$pkg" ] || continue
    if grep -nE "^\s*(from\s+${pkg}\b|import\s+${pkg}\b)" "$SUPERNET" 2>/dev/null; then
      echo "FAIL: supernet.py imports user package '$pkg'"
      FAIL=1
    fi
  done < "$USER_PKG_FILE"
  [ "$FAIL" -eq 0 ] && echo "[check_expand] no user-pkg imports OK"
else
  echo "[check_expand] .user_pkg marker absent — skip user-pkg check (WARN)"
fi

# ── 4. inspect_supernet.py runs (if exists) ─────────────────────────────
INSPECT="$ARTIFACTS_DIR/inspect_supernet.py"
if [ -s "$INSPECT" ]; then
  (cd "$ARTIFACTS_DIR" && python3 "$INSPECT" >/dev/null 2>&1) || {
    echo "FAIL: inspect_supernet.py failed to run"
    exit 1
  }
  echo "[check_expand] inspect_supernet.py runs OK"
else
  echo "FAIL: inspect_supernet.py missing"
  exit 1
fi

# ── Result ──────────────────────────────────────────────────────────────
if [ "$FAIL" -ne 0 ]; then
  echo "FAIL: check_expand failed"
  exit 1
fi
echo "PASS: check_expand"
