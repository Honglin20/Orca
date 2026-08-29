#!/usr/bin/env bash
# check_flatten.sh — deterministic gate for psu_flatten artifacts.
# Checks: flat/optimized __main__ runs + no local user-pkg imports + pathlib paths +
#         manifest sections + load_pretrained.py ckpt 冒烟（strict 载入 + probe forward）。
# Fail loud: any check fails → exit 1.
set -euo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"
FAIL=0

echo "[check_flatten] artifacts_dir=$ARTIFACTS_DIR"

# ── 1. Find prepared model (flat or optimized) ──────────────────────────
MODEL_FILE=""
for f in "$ARTIFACTS_DIR"/*_flat.py "$ARTIFACTS_DIR"/*_llm-optimized.py; do
  [ -s "$f" ] || continue
  MODEL_FILE="$f"
  break
done
if [ -z "$MODEL_FILE" ]; then
  echo "FAIL: no *_flat.py or *_llm-optimized.py found"
  exit 1
fi
echo "[check_flatten] model_file=$MODEL_FILE"

# ── 2. __main__ block runs successfully ─────────────────────────────────
python3 "$MODEL_FILE" >/dev/null 2>&1 || {
  echo "FAIL: $MODEL_FILE __main__ block failed to run"
  exit 1
}
echo "[check_flatten] __main__ runs OK"

# ── 3. ast.parse (syntax valid) ─────────────────────────────────────────
python3 -c "
import ast, sys
ast.parse(open(sys.argv[1]).read())
print('AST_OK')
" "$MODEL_FILE" 2>/dev/null | grep -q AST_OK || {
  echo "FAIL: $MODEL_FILE has syntax errors"
  exit 1
}

# ── 4. No forbidden string-concat paths (pathlib enforcement) ───────────
# Allow os.path.join and pathlib.Path; flag raw '+ ... + ...' path patterns.
if grep -nE "(\+\s*[\"']/|f[\"'].*\{.*\}.*/)" "$MODEL_FILE" 2>/dev/null | grep -v '#' | head -5; then
  echo "WARN: possible string-concat path in $MODEL_FILE (review manually)"
fi

# ── 5. No local user-pkg imports (.user_pkg marker) ─────────────────────
USER_PKG_FILE="$ARTIFACTS_DIR/.user_pkg"
if [ -s "$USER_PKG_FILE" ]; then
  while IFS= read -r pkg; do
    [ -n "$pkg" ] || continue
    if grep -nE "^\s*(from\s+${pkg}\b|import\s+${pkg}\b)" "$MODEL_FILE" 2>/dev/null; then
      echo "FAIL: $MODEL_FILE imports user package '$pkg' (forbidden in standalone model)"
      FAIL=1
    fi
  done < "$USER_PKG_FILE"
  [ "$FAIL" -eq 0 ] && echo "[check_flatten] no user-pkg imports OK"
else
  echo "[check_flatten] .user_pkg marker absent — skip user-pkg check (WARN)"
fi

# ── 6. project_manifest.md exists with required sections ────────────────
MANIFEST="$ARTIFACTS_DIR/project_manifest.md"
if [ ! -s "$MANIFEST" ]; then
  echo "FAIL: project_manifest.md missing or empty"
  exit 1
fi
for section in "Project Overview" "Model" "Training And Evaluation" "Data And Environment" "Relevant Source Files"; do
  grep -q "## $section" "$MANIFEST" || {
    echo "FAIL: project_manifest.md missing section '## $section'"
    FAIL=1
  }
done
[ "$FAIL" -eq 0 ] && echo "[check_flatten] manifest sections OK"

# ── 7. load_pretrained.py ckpt 冒烟（预训练权重可载入 + 键位对齐）──────────
# 运行其 __main__ 冒烟（build_pretrained_model + probe inputs forward）。
# key mismatch 由脚本内部 strict load_state_dict fail loud（列未匹配键清单）。
LP="$ARTIFACTS_DIR/load_pretrained.py"
if [ ! -s "$LP" ]; then
  echo "FAIL: load_pretrained.py missing or empty (Step 4 artifact)"
  FAIL=1
elif (cd "$ARTIFACTS_DIR" && python3 "$LP" >/dev/null 2>&1); then
  echo "[check_flatten] load_pretrained ckpt smoke OK"
else
  echo "FAIL: load_pretrained.py ckpt smoke failed (pretrained_ckpt cannot strictly load into the prepared model — unmatched keys are listed in its stderr)"
  FAIL=1
fi

# ── Result ──────────────────────────────────────────────────────────────
if [ "$FAIL" -ne 0 ]; then
  echo "FAIL: check_flatten failed"
  exit 1
fi
echo "PASS: check_flatten"
