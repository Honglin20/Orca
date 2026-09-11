#!/usr/bin/env bash
# check_baseline_docs.sh — po_baseline validation gate for the THREE baseline
# analysis documents (v7 §4.3; replaces check_business_logic.sh).
#
# The three sentinel literals live HERE and nowhere else (一处一真相): the
# node prompt and the emit gates reference this gate, they never re-type the
# sentinels.
#
# Checks, per document: file exists, non-empty, first line is the authoring
# subagent's sentinel (proves the document was verifiably written by the
# subagent, not fabricated elsewhere), and every required section heading is
# present with non-empty body content:
#   baseline/business_logic.md          business-logic-analyst, five sections
#   base/information_analysis.md        information-analyst v2, four sections
#   base/profile/mfu_bottleneck_report.md  mfu-analyzer v3, five sections
# Plus the origin anchor: base/origin_anchor.json exists and is schema-valid
# (the executed-gate belt for the chain's deterministic freeze).
#
# Findings -> stderr; exit 0 pass / 1 fail / 2 hard error.
# Environment: ORCA_ARTIFACTS_DIR (required).
set -uo pipefail

ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (check_baseline_docs.sh)}"

# ── the three sentinels (single source — this file) ──────────────────────────
BL_SENTINEL="[subagent:business-logic-analyst v1 BLA7K4]"
IX_SENTINEL="[subagent:information-analyst v2 IXA3N7]"
MFU_SENTINEL="[subagent:mfu-analyzer v3 MBA7K2]"

fail=0

check_doc() { # check_doc <label> <path> <sentinel> <section>...
  local label="$1" path="$2" sentinel="$3"; shift 3
  if [ ! -f "$path" ]; then
    echo "check_baseline_docs: FAIL $label not found at $path" >&2
    fail=1
    return
  fi
  if [ ! -s "$path" ]; then
    echo "check_baseline_docs: FAIL $label is empty ($path)" >&2
    fail=1
    return
  fi
  if [ "$(head -n 1 "$path")" != "$sentinel" ]; then
    echo "check_baseline_docs: FAIL $label first line is not the sentinel $sentinel (got: $(head -n 1 "$path"))" >&2
    fail=1
    return
  fi
  python3 - "$label" "$path" "$@" <<'PY' || fail=1
import re
import sys
from pathlib import Path

label, path = sys.argv[1], Path(sys.argv[2])
sections = sys.argv[3:]          # FULL headings incl. their ##/### prefix
text = path.read_text(encoding="utf-8")
problems = []
# heading bounds: (start, end) — the body runs from this heading's END to the
# NEXT heading's START (comparing against the next end would count the next
# heading's own text as body and silently void the emptiness check)
bounds: dict[str, tuple[int, int]] = {}
for section in sections:
    # [ \t]* (NOT \s*): \s would swallow the newline and shift every body
    # boundary into the next section
    m = re.search(rf"^{re.escape(section)}[ \t]*$", text, re.MULTILINE)
    if not m:
        problems.append(f"missing section heading '{section}'")
    else:
        bounds[section] = (m.start(), m.end())
for section, (_, body_start) in bounds.items():
    next_starts = [s for s, _ in bounds.values() if s > body_start]
    body = text[body_start:min(next_starts)] if next_starts else text[body_start:]
    if not body.strip():         # a bare heading with no body is not a section
        problems.append(f"section '{section}' is empty")
if problems:
    for p in problems:
        print(f"check_baseline_docs: FAIL {label}: {p}", file=sys.stderr)
    raise SystemExit(1)
PY
}

cd "$ART" || { echo "FATAL: artifacts dir unreachable: $ART" >&2; exit 2; }

check_doc "baseline/business_logic.md" \
  "$ART/baseline/business_logic.md" \
  "$BL_SENTINEL" \
  "## 任务语义" "## 输入输出" "## 架构动机" "## 逐模块职责与物理意义" "## 训练目标与指标方向"

check_doc "base/information_analysis.md" \
  "$ART/base/information_analysis.md" \
  "$IX_SENTINEL" \
  "## 信息成分拆解" "## 最小信息核心" "## 冗余与可近似项" "## 创新结构方向"

# the mfu report's sections sit under its `## MFU 时延瓶颈分析报告` title
# as ### headings (the analyzer md's own template) — same emptiness rules
check_doc "base/profile/mfu_bottleneck_report.md" \
  "$ART/base/profile/mfu_bottleneck_report.md" \
  "$MFU_SENTINEL" \
  "### 模型概况" "### MFU 损耗分解" "### 瓶颈根因" "### 算子级证据表（按显著性列行）" "### 评测异常与披露"

# ── origin anchor (executed-gate belt; the chain freezes it deterministically,
#    but `executed` must be IMPOSSIBLE without it — downstream readers
#    (frontier_snapshot / gate / verdict) all fail loud on a missing anchor,
#    a full node too late) ─────────────────────────────────────────────────────
python3 - "$ART/base/origin_anchor.json" <<'PY' || fail=1
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    doc = json.loads(path.read_text(encoding="utf-8"))
except FileNotFoundError:
    print("check_baseline_docs: FAIL base/origin_anchor.json missing — "
          "the chain's origin-anchor freeze did not run; every downstream "
          "gate/advance/verdict would fail loud a full node later", file=sys.stderr)
    raise SystemExit(1)
except json.JSONDecodeError as exc:
    print(f"check_baseline_docs: FAIL base/origin_anchor.json unparseable: {exc}",
          file=sys.stderr)
    raise SystemExit(1)
problems = []
if not isinstance(doc, dict):
    problems.append(f"not a JSON object (got {type(doc).__name__})")
else:
    for key in ("baseline_makespan_cycles", "target_cycles"):
        value = doc.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            problems.append(f"invalid {key!r}: {value!r} (must be a "
                            "non-negative integer)")
    budget = doc.get("accuracy_budget")
    if isinstance(budget, bool) or not isinstance(budget, (int, float)):
        problems.append(f"invalid 'accuracy_budget': {budget!r} (must be numeric)")
if problems:
    for p in problems:
        print(f"check_baseline_docs: FAIL base/origin_anchor.json: {p}",
              file=sys.stderr)
    raise SystemExit(1)
PY

if [ "$fail" -ne 0 ]; then
  echo "FAIL: check_baseline_docs" >&2
  exit 1
fi
echo "check_baseline_docs: PASS (three sentinels + all sections + origin anchor)" >&2
exit 0
