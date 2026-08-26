#!/usr/bin/env bash
# supernet_bar.sh — does <file> expose SearchSpace or build_supernet (exec-level bar)?
# Exit 0 iff the file parses, execs, and exposes SearchSpace/build_supernet.
set -euo pipefail

FILE="${1:-supernet.py}"

python3 -c "
import ast, sys, types
src = open(sys.argv[1]).read()
ast.parse(src)
probe = types.ModuleType('supernet_bar_probe')
probe.__dict__['__file__'] = sys.argv[1]
sys.modules['supernet_bar_probe'] = probe
exec(compile(src, sys.argv[1], 'exec'), probe.__dict__)
ns = probe.__dict__
assert 'SearchSpace' in ns or 'build_supernet' in ns, 'no SearchSpace/build_supernet'
print('SUPERNET_VALID')
" "$FILE" 2>/dev/null | grep -q SUPERNET_VALID
