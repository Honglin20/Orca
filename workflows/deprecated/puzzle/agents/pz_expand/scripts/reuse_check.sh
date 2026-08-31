#!/bin/bash
# pz_expand Step 0 软跳过：5 产物齐 + flat 可 parse + search_space 与 block_map 的 slot 数一致且 ≥1。
# 输出 REUSE_VALID（达标 → 跳过 Step 1-3）或空（不达标 → 照常执行 Step 1-3）。
set +e
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }
MISSING=""
for f in manifest.yaml search_space.yaml block_map.json baseline_metrics.json puzzle_adapters.py; do
  [ -s "$f" ] || MISSING="$MISSING $f"
done
FLAT="$(ls *_flat.py 2>/dev/null | head -1)"
[ -n "$FLAT" ] || MISSING="$MISSING <base>_flat.py"
[ -n "$MISSING" ] && exit 0
python3 -c "
import ast, json, sys, yaml
ast.parse(open(sys.argv[1]).read())
slots = yaml.safe_load(open('search_space.yaml'))['slots']
assert isinstance(slots, list), 'slots not list'
bm = json.load(open('block_map.json'))['slots']
assert len(slots) == len(bm) and len(slots) >= 1, 'slots mismatch/empty'
print('REUSE_VALID')
" "$FLAT" 2>/dev/null
