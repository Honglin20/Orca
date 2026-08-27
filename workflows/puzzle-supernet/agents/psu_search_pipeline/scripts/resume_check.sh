#!/usr/bin/env bash
# resume_check.sh — psu_search_pipeline Step 0.5 resume gate.
# Sets SKIP_SCHEMA / SKIP_A / SKIP_B / SKIP_C from what is already on disk; prints the flags line.
set -uo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

SKIP_SCHEMA=false; SKIP_A=false; SKIP_B=false; SKIP_C=false

# schema: exists + valid JSON + has arch_fields → skip producing the schema
if [ -s search_record_schema.json ] && python3 -c "import json;d=json.load(open('search_record_schema.json'));assert d.get('arch_fields')" 2>/dev/null; then SKIP_SCHEMA=true; echo "RESUME: search_record_schema.json already present and valid → skip producing the schema"; fi
# subagent A (latency): latency_estimator.py exists + py_compile passes → skip A
if [ -s latency_estimator.py ] && python3 -m py_compile latency_estimator.py 2>/dev/null; then SKIP_A=true; echo "RESUME: latency_estimator.py already present and valid → skip subagent A"; fi
# subagent B (search-core): evaluator.py + arch_codec.py + search_config.yaml + run_search_supernet.sh all present + py_compile passes → skip B
if [ -s evaluator.py ] && [ -s arch_codec.py ] && [ -s search_config.yaml ] && [ -s run_search_supernet.sh ] \
   && python3 -m py_compile evaluator.py arch_codec.py 2>/dev/null; then SKIP_B=true; echo "RESUME: search-core 4 files already present and valid → skip subagent B"; fi
# subagent C (select): select_architecture.py present + select --help rc=0 → skip C
if [ -s select_architecture.py ] && python3 select_architecture.py --help >/dev/null 2>&1; then SKIP_C=true; echo "RESUME: select_architecture.py already present and valid → skip subagent C"; fi

echo "RESUME flags: SKIP_SCHEMA=$SKIP_SCHEMA SKIP_A=$SKIP_A SKIP_B=$SKIP_B SKIP_C=$SKIP_C"
