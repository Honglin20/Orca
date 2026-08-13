#!/usr/bin/env python3
"""generate_schema.py — write search_record_schema.json by introspecting supernet.py's SearchSpace.

Run from $ORCA_ARTIFACTS_DIR (cwd). The file is written only after all computation succeeds —
do NOT use `> search_record_schema.json` redirection: bash truncates the file before python
starts, so any crash during parsing leaves a 0-byte file which then fools the Step 0.5 resume
gate (`-s` non-empty check) and crashes downstream discover_latency_unit on an empty json.
"""
import argparse
import json
import sys
import traceback

parser = argparse.ArgumentParser()
parser.add_argument("--latency-unit", default="ms")
args = parser.parse_args()

# exec supernet.py to extract SearchSpace. __name__='not_main' skips its __main__ smoke block.
try:
    src = open('supernet.py').read()
    ns = {'__name__': 'not_main'}
    exec(compile(src, 'supernet.py', 'exec'), ns)
except Exception as e:
    print(f'FATAL: cannot exec supernet.py for introspection: {e}', file=sys.stderr)
    traceback.print_exc()
    sys.exit(1)

SearchSpace = ns.get('SearchSpace')
if SearchSpace is None:
    print('FATAL: SearchSpace not found in supernet.py', file=sys.stderr)
    sys.exit(1)

try:
    ss = SearchSpace()
except Exception as e:
    print(f'FATAL: SearchSpace() instantiation failed: {e}', file=sys.stderr)
    sys.exit(1)

arch_fields = {}
for attr in dir(ss):
    if attr.startswith('_'):
        continue
    val = getattr(ss, attr)
    if isinstance(val, (list, tuple)) and len(val) > 0:
        if all(isinstance(v, (list, tuple)) for v in val):
            # Nested: stage_depth_candidates = [[1,2,3], [2,3,4]]
            arch_fields[attr] = {'type': 'list_of_lists', 'values': [list(v) for v in val]}
        elif all(isinstance(v, (int, float, str)) for v in val):
            arch_fields[attr] = {'type': 'list', 'values': list(val)}

if not arch_fields:
    print('FATAL: no elastic dimensions found in SearchSpace', file=sys.stderr)
    sys.exit(1)

# metric info (best-effort): downstream select_architecture.py derives metric name/direction from
# search_config.yaml objs; the schema's metric_name/metric_direction are kept as backup — manifest is
# free text with no reliable metric-name anchor, so metric_name is left empty.
metric_direction = ''
try:
    for line in open('project_manifest.md').read().split('\n'):
        low = line.lower()
        if 'higher-better' in low:
            metric_direction = 'higher-better'
        elif 'lower-better' in low:
            metric_direction = 'lower-better'
except FileNotFoundError:
    pass

schema = {
    'arch_fields': arch_fields,
    'metric_name': '',
    'metric_direction': metric_direction,
    'latency_ms_field': 'latency',
    'latency_unit': args.latency_unit,
    'extra_fields': ['acc', 'params'],
}
with open('search_record_schema.json', 'w') as f:
    json.dump(schema, f, indent=2)
print(f'WROTE search_record_schema.json ({len(arch_fields)} arch_fields)')
