#!/usr/bin/env bash
# check_expand.sh — deterministic gate for psu_expand_supernet artifacts.
# Runs checks 0-5 (5 = choice contract + original-path equivalence gate),
# prints one tagged line per check, exits non-zero if any fail.
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
# 以真实 ModuleType 注册进 sys.modules 再 exec：py3.14 dataclass 解析 postponed
# annotation 时查 sys.modules[cls.__module__]，裸 dict 命空间会 AttributeError。
if python3 -c "
import sys, types
mod = types.ModuleType('check2_supernet_probe')
mod.__dict__['__file__'] = sys.argv[1]
sys.modules['check2_supernet_probe'] = mod
exec(compile(open(sys.argv[1]).read(), sys.argv[1], 'exec'), mod.__dict__)
ns = mod.__dict__
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

# ── 5. Choice contract + original-path equivalence gate ─────────────────
#       5a: choice-only 契约（反向维度 gate：任何维度 >1 候选即 FAIL；钉死
#           维度 == .baseline.json 实测值）。
#       5b: 等价 gate（全 original 路径 ≡ load_pretrained.py 构建的预训练原模型；
#           物化键契约 + 逐张量 forward 等价 + freeze 分组；无论 pass/fail 都落盘
#           .equivalence.json）。失败时重印两个脚本的逐条原因供 agent 定位。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if CHOICE_OUT="$(python3 "$SCRIPT_DIR/check_choice_contract.py" --artifacts-dir "$ARTIFACTS_DIR" 2>&1)"; then
  ok "choice contract (choice-only + pinned dims vs baseline)"
else
  printf '%s\n' "$CHOICE_OUT" | sed 's/^/[check_expand]     /'
  fail "choice contract violated"
fi
if EQUIV_OUT="$(python3 "$SCRIPT_DIR/check_equivalence.py" --artifacts-dir "$ARTIFACTS_DIR" 2>&1)"; then
  ok "original-path equivalence gate (gate E)"
else
  printf '%s\n' "$EQUIV_OUT" | sed 's/^/[check_expand]     /'
  fail "original-path equivalence gate failed"
fi

# ── Result ───────────────────────────────────────────────────────────────
if [ "$FAILS" -ne 0 ]; then
  echo "[check_expand] result: FAIL ($FAILS check(s) failed)"
  exit 1
fi
echo "[check_expand] result: PASS"
