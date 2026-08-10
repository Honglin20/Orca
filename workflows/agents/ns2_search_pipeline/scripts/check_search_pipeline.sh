#!/usr/bin/env bash
# check_search_pipeline.sh — deterministic gate for ns2_search_pipeline artifacts.
# Checks: 5 files exist + each py_compile + select_architecture.py --help rc=0 + shared schema present.
set -euo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"
FAIL=0

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }
echo "[check_search_pipeline] artifacts_dir=$ARTIFACTS_DIR"

# ── 1. Required files exist ─────────────────────────────────────────────
for f in latency_estimator.py evaluator.py arch_codec.py search_config.yaml run_search_supernet.sh select_architecture.py; do
  if [ ! -s "$f" ]; then
    echo "FAIL: $f missing or empty"
    FAIL=1
  fi
done
[ "$FAIL" -eq 0 ] && echo "[check_search_pipeline] all 6 files exist OK"

# ── 2. py_compile each .py ──────────────────────────────────────────────
for f in latency_estimator.py evaluator.py arch_codec.py select_architecture.py; do
  [ -s "$f" ] || continue
  python3 -m py_compile "$f" || {
    echo "FAIL: $f py_compile failed"
    FAIL=1
  }
done
[ "$FAIL" -eq 0 ] && echo "[check_search_pipeline] py_compile all OK"

# ── 3. select_architecture.py --help rc=0 ───────────────────────────────
if [ -s "select_architecture.py" ]; then
  python3 select_architecture.py --help >/dev/null 2>&1 || {
    echo "FAIL: select_architecture.py --help returned non-zero"
    FAIL=1
  }
  [ "$FAIL" -eq 0 ] && echo "[check_search_pipeline] select_architecture.py --help OK"
fi

# ── 4. Shared schema present ─────────────────────────────────────────────
if [ ! -s "search_record_schema.json" ]; then
  echo "WARN: search_record_schema.json missing (shared schema for sub-agents B/C)"
fi

# ── 5. run_search_supernet.sh: no torchrun (single-device default) ───────
if [ -s "run_search_supernet.sh" ]; then
  if grep -q 'torchrun' run_search_supernet.sh; then
    echo "WARN: run_search_supernet.sh contains torchrun (should default to plain python3)"
  fi
fi

# ── Result ──────────────────────────────────────────────────────────────
if [ "$FAIL" -ne 0 ]; then
  echo "FAIL: check_search_pipeline failed"
  exit 1
fi
echo "PASS: check_search_pipeline"
