#!/usr/bin/env bash
# check_report.sh — deterministic gate for ns3_report output JSON.
# Checks: JSON is valid + status enum + required fields present.
set -euo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }

# Read the report JSON from stdin or from .report.json marker
REPORT_FILE="$ARTIFACTS_DIR/.report.json"
if [ ! -s "$REPORT_FILE" ]; then
  echo "SKIP: .report.json not found"
  exit 0
fi

python3 -c "
import json, sys

with open(sys.argv[1], 'r') as f:
    data = json.load(f)

required = ['status', 'stage', 'reason', 'selected_arch', 'selected_acc',
            'selected_latency', 'latency_unit', 'subnet_structure',
            'pareto_size', 'supernet_path', 'output_dir',
            'final_metrics', 'artifacts', 'charts_summary', 'error']
missing = [k for k in required if k not in data]
if missing:
    print(f'FAIL: missing fields: {missing}')
    sys.exit(1)

if data['status'] not in ('success', 'failed'):
    print(f'FAIL: status must be success/failed, got {data[\"status\"]}')
    sys.exit(1)

stages = {'flatten', 'expand', 'train_script', 'search_pipeline', 'run_train',
          'run_search', 'retrain', 'report'}
if data['stage'] not in stages:
    print(f'FAIL: stage must be one of {stages}, got {data[\"stage\"]}')
    sys.exit(1)

print('PASS: check_report')
" "$REPORT_FILE" || exit 1
