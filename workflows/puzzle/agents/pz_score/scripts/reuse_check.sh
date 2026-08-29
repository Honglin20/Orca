#!/bin/bash
# pz_score Step 0 软跳过：scores.jsonl + latency_table.jsonl 齐且 (layer,kind,variant) 对齐。
# 输出 SCORE_REUSE_VALID（达标 → 跳过 Step 1-3）或空（不达标 → 照常执行）。
set +e
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }
[ -s scores.jsonl ] && [ -s latency_table.jsonl ] || exit 0
python3 -c "
import json
s = [json.loads(l) for l in open('scores.jsonl') if l.strip()]
t = [json.loads(l) for l in open('latency_table.jsonl') if l.strip()]
sk = {(r['layer'], r['kind'], r['variant']) for r in s}
tk = {(r['layer'], r['kind'], r['variant']) for r in t}
assert sk == tk, f'misaligned: {sk ^ tk}'
print('SCORE_REUSE_VALID')
" 2>/dev/null
