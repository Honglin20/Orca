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
import types

parser = argparse.ArgumentParser()
parser.add_argument("--latency-unit", default="ms")
args = parser.parse_args()

# exec supernet.py to extract SearchSpace. A registered ModuleType namespace (not a
# bare dict) is required: PSU SearchSpace is a postponed-annotation dataclass, and
# @dataclass resolves cls.__module__ through sys.modules — a bare dict exec crashes
# with AttributeError. Registering before exec also keeps __name__ != '__main__',
# which skips the module's __main__ smoke block.
try:
    src = open('supernet.py').read()
    probe = types.ModuleType('supernet_schema_probe')
    probe.__dict__['__file__'] = 'supernet.py'
    sys.modules['supernet_schema_probe'] = probe
    exec(compile(src, 'supernet.py', 'exec'), probe.__dict__)
except Exception as e:
    print(f'FATAL: cannot exec supernet.py for introspection: {e}', file=sys.stderr)
    traceback.print_exc()
    sys.exit(1)

SearchSpace = probe.__dict__.get('SearchSpace')
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
            # Nested container (e.g. per-stage candidate lists) — NOT a valid
            # choice container; recorded only so the gate below can name it.
            arch_fields[attr] = {'type': 'list_of_lists', 'values': [list(v) for v in val]}
        elif all(isinstance(v, (int, float, str)) for v in val):
            # Flat value list — the choice container, e.g.
            # branch_choices = ('original', 'vanilla', 'random_synthesizer', ...)
            arch_fields[attr] = {'type': 'list', 'values': list(val)}

if not arch_fields:
    print('FATAL: no searchable choice fields found in SearchSpace', file=sys.stderr)
    sys.exit(1)

# Double gate (schema side of the choice-only contract): reflection must find
# exactly one searchable dimension — the choice container 'branch_choices'.
# Any extra public container is fatal, including a flat single-value tuple on
# a pinned dimension, which the walk above would misreport as type=list.
if set(arch_fields) != {'branch_choices'}:
    print(
        f"FATAL: the searchable fields must be exactly ['branch_choices'], "
        f"found {sorted(arch_fields)} — pinned dimensions must be scalars or "
        f"_-prefixed, never list/tuple",
        file=sys.stderr,
    )
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
