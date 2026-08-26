#!/usr/bin/env bash
# check_business_logic.sh — po_baseline validation gate for the business-logic
# document (the business-logic-analyst subagent's product).
#
# Checks baseline/business_logic.md:
#   1. exists and is non-empty;
#   2. first line is the authoring subagent's sentinel
#      (`[subagent:business-logic-analyst v<N> <TOKEN>]` — proves the document
#      was verifiably written by the subagent, not fabricated elsewhere);
#   3. all five canonical section headings are present, each with non-empty
#      body content (任务语义 / 输入输出 / 架构动机 / 逐模块职责与物理意义 /
#      训练目标与指标方向).
#
# Findings -> stderr; exit 0 pass / 1 fail / 2 hard error.
# Environment: ORCA_ARTIFACTS_DIR (required).
set -uo pipefail

ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (check_business_logic.sh)}"
DOC="$ART/baseline/business_logic.md"

[ -f "$DOC" ] || { echo "check_business_logic: FAIL baseline/business_logic.md not found (business-logic-analyst has not completed)" >&2; exit 1; }
[ -s "$DOC" ] || { echo "check_business_logic: FAIL baseline/business_logic.md is empty" >&2; exit 1; }

fail=0
head -n 1 "$DOC" | grep -qE '^\[subagent:business-logic-analyst v[0-9]+ [A-Z0-9]+\]$' \
  || { echo "check_business_logic: FAIL first line is not the business-logic-analyst sentinel: $(head -n 1 "$DOC")" >&2; fail=1; }

# each canonical section: heading present AND followed by non-whitespace
# content before the next heading (a bare heading list is not a document)
python3 - "$DOC" <<'PY' || fail=1
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
SECTIONS = ["任务语义", "输入输出", "架构动机", "逐模块职责与物理意义", "训练目标与指标方向"]
problems = []
# heading bounds: (start, end) — the body runs from this heading's END to the
# NEXT heading's START (comparing against the next end would count the next
# heading's own text as body and silently void the emptiness check)
bounds: dict[str, tuple[int, int]] = {}
for section in SECTIONS:
    # [ \t]* (NOT \s*): \s would swallow the newline and shift every body
    # boundary into the next section
    m = re.search(rf"^##[ \t]*{re.escape(section)}[ \t]*$", text, re.MULTILINE)
    if not m:
        problems.append(f"missing section heading '## {section}'")
    else:
        bounds[section] = (m.start(), m.end())
for section, (_, body_start) in bounds.items():
    next_starts = [s for s, _ in bounds.values() if s > body_start]
    body = text[body_start:min(next_starts)] if next_starts else text[body_start:]
    if not body.strip():         # a bare heading with no body is not a section
        problems.append(f"section '{section}' is empty")
if problems:
    for p in problems:
        print(f"check_business_logic: FAIL {p}", file=sys.stderr)
    raise SystemExit(1)
PY

if [ "$fail" -ne 0 ]; then
  echo "FAIL: check_business_logic" >&2
  exit 1
fi
echo "check_business_logic: PASS (sentinel + five sections)" >&2
exit 0
