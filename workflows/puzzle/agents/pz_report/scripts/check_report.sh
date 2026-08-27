#!/usr/bin/env bash
# check_report.sh — deterministic gate for pz_report output JSON.
# Checks: JSON is valid + status enum + required fields present.
set -euo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }

# Read the report JSON from the .report.json marker (written by emit_report.py)
REPORT_FILE="$ARTIFACTS_DIR/.report.json"
if [ ! -s "$REPORT_FILE" ]; then
  echo "SKIP: .report.json not found"
  exit 0
fi

python3 -c "
import json, sys

with open(sys.argv[1], 'r') as f:
    data = json.load(f)

required = ['status', 'stage', 'reason', 'gate_status', 'final_metric',
            'final_latency', 'baseline_metric', 'baseline_latency',
            'metric_delta', 'latency_ratio', 'latency_unit', 'gate_reason',
            'report_path', 'selected_arch', 'optimized_flat_path',
            'output_dir', 'block_map', 'error', 'artifacts']
missing = [k for k in required if k not in data]
if missing:
    print(f'FAIL: missing fields: {missing}')
    sys.exit(1)

if data['status'] not in ('success', 'failed'):
    print(f'FAIL: status must be success/failed, got {data[\"status\"]}')
    sys.exit(1)

stages = {'ingest', 'search_space', 'baseline', 'build_library', 'score',
          'select', 'materialize', 'retrain', 'report'}
if data['stage'] not in stages:
    print(f'FAIL: stage must be one of {stages}, got {data[\"stage\"]}')
    sys.exit(1)

print('PASS: check_report')
" "$REPORT_FILE" || exit 1
