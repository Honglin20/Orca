#!/usr/bin/env bash
# check_expand.sh — deterministic gate for ns3_expand_supernet artifacts.
# Runs 5 checks, prints one tagged line per check, exits non-zero if any fail.
set -uo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"
SUPERNET="$ARTIFACTS_DIR/supernet.py"
FAILS=0

ok()   { echo "[check_expand] OK   $1"; }
fail() { echo "[check_expand] FAIL $1"; FAILS=$((FAILS + 1)); }

# ── 0. supernet.py present (fatal — nothing else can run without it) ─────
if [ ! -s "$SUPERNET" ]; then
  fail "supernet.py missing or empty"
  echo "[check_expand] result: FAIL"
  exit 1
fi
ok "supernet.py present"

# ── 1. ast.parse (syntax valid) ──────────────────────────────────────────
if python3 -c "
import ast, sys
ast.parse(open(sys.argv[1]).read())
" "$SUPERNET" 2>/dev/null; then
  ok "supernet.py syntax valid"
else
  fail "supernet.py has syntax errors"
fi

# ── 2. exec exposes SearchSpace or build_supernet ────────────────────────
if python3 -c "
import sys
src = open(sys.argv[1]).read()
ns = {}
exec(compile(src, sys.argv[1], 'exec'), ns)
assert 'SearchSpace' in ns or 'build_supernet' in ns, 'no SearchSpace/build_supernet'
" "$SUPERNET" 2>/dev/null; then
  ok "exposes SearchSpace/build_supernet"
else
  fail "does not expose SearchSpace/build_supernet"
fi

# ── 3. No local user-pkg imports (.user_pkg marker) ──────────────────────
USER_PKG_FILE="$ARTIFACTS_DIR/.user_pkg"
if [ -s "$USER_PKG_FILE" ]; then
  pkg_bad=0
  while IFS= read -r pkg; do
    [ -n "$pkg" ] || continue
    if grep -nE "^\s*(from\s+${pkg}\b|import\s+${pkg}\b)" "$SUPERNET" 2>/dev/null; then
      echo "[check_expand] FAIL imports user package '$pkg'"
      pkg_bad=1
    fi
  done < "$USER_PKG_FILE"
  [ "$pkg_bad" -eq 0 ] && ok "no user-pkg imports"
  FAILS=$((FAILS + pkg_bad))
else
  echo "[check_expand] WARN .user_pkg marker absent — skip user-pkg check"
fi

# ── 4. inspect_supernet.py runs ──────────────────────────────────────────
INSPECT="$ARTIFACTS_DIR/inspect_supernet.py"
if [ -s "$INSPECT" ]; then
  if (cd "$ARTIFACTS_DIR" && python3 "$INSPECT" >/dev/null 2>&1); then
    ok "inspect_supernet.py runs"
  else
    fail "inspect_supernet.py failed to run"
  fi
else
  fail "inspect_supernet.py missing"
fi

# ── 5. Search-space contract (depth + internal-width sandwich vs baseline) ──
#       Delegates to check_search_space.py; on failure, reprint its per-field
#       reasons (prefixed) so the agent sees exactly what to fix.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTRACT_OUT="$(python3 "$SCRIPT_DIR/check_search_space.py" --artifacts-dir "$ARTIFACTS_DIR" 2>&1)"
CONTRACT_RC=$?
if [ "$CONTRACT_RC" -eq 0 ]; then
  ok "search-space contract (depth + internal-width sandwich vs baseline)"
else
  printf '%s\n' "$CONTRACT_OUT" | sed 's/^/[check_expand]     /'
  fail "search-space contract violated"
fi

# ── Result ───────────────────────────────────────────────────────────────
if [ "$FAILS" -ne 0 ]; then
  echo "[check_expand] result: FAIL ($FAILS check(s) failed)"
  exit 1
fi
echo "[check_expand] result: PASS"
