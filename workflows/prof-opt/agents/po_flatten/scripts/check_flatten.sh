#!/usr/bin/env bash
# check_flatten.sh — deterministic gate for po_flatten artifacts (v7).
#
# Re-verifies everything downstream nodes depend on (fail loud, exit 1):
#   1. heartbeat refresh (validation-time mtime touch of .run_lock — v7 F5:
#      the heartbeat model is CONTINUATION (the watchdog and the baseline
#      finalizer refresh it mid-run), so this gate only touches the mtime,
#      never rewrites the lock's identity content)
#   2. shadow tree: exists, >=1 top-level name, >=1 .py, no __pycache__/*.pyc
#   3. BASELINE.lock: valid JSON, v7 schema (version / model_path /
#      py_files_sha256), checksums still match (recomputed over shadow *.py;
#      the pretrained-ckpt anchor is deleted in v7 — F2)
#   4. stdlib collision: no shadow top-level name in sys.stdlib_module_names
#   5. deployed tooling: orca_inject pair + scripts/{assert_shadow,render_run,
#      emit_result,deploy_scripts,analyze,mfu_adapter} present
#   6. project_manifest.md: pinned sections + EVERY listed ranking metric's
#      direction marked (v7 F10: per-metric check, one spelling —
#      higher_better / lower_better)
#   7. readiness/readiness.json: all THREE v7 checks true (constructible /
#      exportable / definition_located — the vacuous pretrained_loadable is
#      deleted, F2)
#   8. runtime proof: assert_shadow.py passes under the injection header —
#      every shadow pkg resolves inside the shadow tree, executed in the exact
#      invocation form the run templates use
#
# Usage: check_flatten.sh <model_path>
# Environment: ORCA_ARTIFACTS_DIR (required), ORCA_RUN_ID (heartbeat owner),
#              ORCA_PYTHON (optional; env wins over readiness.json "python",
#              python3 last — v7 F9: code and comment agree on the priority)
set -uo pipefail

MODEL_PATH="${1:?usage: check_flatten.sh <model_path>}"
ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (check_flatten.sh)}"

FAIL=0
bad() { echo "FAIL: $*" >&2; FAIL=1; }
note() { echo "[check_flatten] $*" >&2; }

cd "$ART" || { echo "FATAL: artifacts dir unreachable: $ART" >&2; exit 2; }

# 1. heartbeat (mtime touch only — v7 F5: the lock's identity content belongs
# to the entry gate that wrote it; this gate just keeps the heartbeat fresh)
touch "$ART/.run_lock" 2>/dev/null \
  || note "WARN: cannot refresh .run_lock heartbeat"

# resolve the working interpreter (v7 F9): ORCA_PYTHON env wins — the caller
# pinned it deliberately — then readiness.json "python", python3 last
PY="python3"
if [ -z "${ORCA_PYTHON:-}" ] && [ -f "$ART/readiness/readiness.json" ]; then
  CAND="$(python3 -c '
import json, sys
from pathlib import Path
try:
    print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["python"])
except Exception:
    pass
' "$ART/readiness/readiness.json" 2>/dev/null || true)"
  [ -n "$CAND" ] && PY="$CAND"
fi
[ -n "${ORCA_PYTHON:-}" ] && PY="$ORCA_PYTHON"
command -v "$PY" >/dev/null 2>&1 || [ -x "$PY" ] \
  || { echo "FAIL: interpreter not found: $PY" >&2; exit 1; }
note "interpreter=$PY"

# 2. shadow tree shape
if [ ! -d "$ART/shadow" ]; then
  bad "shadow/ tree missing"
else
  TOP_COUNT="$(find "$ART/shadow" -mindepth 1 -maxdepth 1 | wc -l)"
  [ "$TOP_COUNT" -ge 1 ] || bad "shadow/ has no top-level entry"
  PY_COUNT="$(find "$ART/shadow" -type f -name '*.py' | wc -l)"
  [ "$PY_COUNT" -ge 1 ] || bad "shadow/ contains no .py file"
  if find "$ART/shadow" \( -name '__pycache__' -o -name '*.pyc' \) | grep -q .; then
    bad "shadow/ contains __pycache__/ or *.pyc (copy exclusions violated)"
  fi
fi

# 3. BASELINE.lock integrity (mechanical re-verification, fail loud)
LOCK_OK="$(python3 - "$ART/BASELINE.lock" "$MODEL_PATH" "$ART/shadow" <<'PY'
import hashlib, json, sys
from pathlib import Path

lock_path, model_path, shadow = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])

def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

problems = []
if not lock_path.is_file():
    print(json.dumps({"ok": False, "problems": ["BASELINE.lock missing"]}))
    sys.exit(0)
try:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
except Exception as exc:
    print(json.dumps({"ok": False, "problems": [f"BASELINE.lock not valid JSON: {exc}"]}))
    sys.exit(0)

for key in ("version", "model_path", "py_files_sha256"):
    if key not in lock:
        problems.append(f"BASELINE.lock missing key {key!r}")
if problems:
    extra = (["lock predates the v7 schema (no version field) — rebuild "
              "with fresh_start"] if "version" not in lock else [])
    print(json.dumps({"ok": False, "problems": problems + extra}))
    sys.exit(0)
if lock.get("version") != 2:
    problems.append(f"BASELINE.lock schema version {lock.get('version')!r} != 2 "
                    "(v7 lock: version/model_path/py_files_sha256) — rebuild "
                    "with fresh_start, never silently migrated")
if lock["model_path"] != model_path:
    problems.append(f"model_path drift: lock={lock['model_path']!r} now={model_path!r}")
actual_py = {str(p.relative_to(shadow)).replace("\\", "/"): sha(p)
             for p in sorted(shadow.rglob("*.py"))} if shadow.is_dir() else {}
if lock["py_files_sha256"] != actual_py:
    problems.append("shadow *.py checksum drift vs BASELINE.lock (shadow edited after lock?)")
print(json.dumps({"ok": not problems, "problems": problems}))
PY
)" || LOCK_OK='{"ok": false, "problems": ["lock verification crashed"]}'
[[ "$LOCK_OK" == *'"ok": true'* ]] || bad "BASELINE.lock verification: $LOCK_OK"

# 4. stdlib collision (top-level shadow names must not shadow the stdlib)
if [ -d "$ART/shadow" ]; then
  CLASH="$(python3 -c '
import sys
from pathlib import Path
shadow = Path(sys.argv[1])
names = sorted({p.name[:-3] if p.suffix == ".py" else p.name for p in shadow.iterdir()})
clash = [n for n in names if n in sys.stdlib_module_names]
print(",".join(clash))
' "$ART/shadow" 2>/dev/null || true)"
  [ -z "$CLASH" ] || bad "shadow top-level names collide with the Python standard library: $CLASH (injection would resolve them back to the originals — rename or restructure)"
fi

# 5. deployed shared tooling
for f in orca_inject/sitecustomize.py orca_inject/header.env \
         scripts/assert_shadow.py scripts/render_run.sh scripts/emit_result.py \
         scripts/deploy_scripts.sh scripts/analyze.py scripts/mfu_adapter.py; do
  [ -s "$ART/$f" ] || bad "deployed tooling missing: $ART/$f (run the deploy step)"
done

# 6. project manifest: pinned sections + per-metric direction markers (F10)
MANIFEST="$ART/project_manifest.md"
if [ ! -s "$MANIFEST" ]; then
  bad "project_manifest.md missing or empty"
else
  for section in "Project Overview" "Model" "Training And Evaluation" "Data And Environment" "Relevant Source Files"; do
    grep -q "## $section" "$MANIFEST" || bad "project_manifest.md missing section '## $section'"
  done
  python3 - "$MANIFEST" <<'PY' || bad "project_manifest.md metric-direction check failed (see stderr above)"
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
m = re.search(r"^##[ \t]*Training And Evaluation[ \t]*$\n"
              r"(.*?)(?=^##[ \t]|\Z)",
              text, re.MULTILINE | re.DOTALL)
section = m.group(1) if m else ""
# F10: EVERY list item in the section is judged — no keyword guessing. Each
# item must either carry a direction marker (higher_better / lower_better —
# the same tokens contracts.json uses) or be explicitly tagged (non-metric);
# a keyword-shaped item cannot slip past, and at least one metric must be
# listed at all.
items = [l for l in section.splitlines() if l.strip().startswith(("- ", "* "))]
problems = []
metric_items = []
for raw in items:
    l = raw.lower()
    if "higher_better" in l or "lower_better" in l:
        metric_items.append(raw)
        continue
    if "higher-better" in l or "lower-better" in l:
        problems.append(f"direction marker uses the old hyphen spelling: {raw.strip()!r}")
        metric_items.append(raw)
        continue
    if "(non-metric)" in l:
        continue
    problems.append(f"list item carries neither a direction marker nor the "
                    f"(non-metric) tag: {raw.strip()!r}")
if not metric_items:
    problems.append("no ranking-metric list item found in '## Training And "
                    "Evaluation' (expected e.g. '- <metric>: higher_better')")
if problems:
    for p in problems:
        print(f"manifest: FAIL {p}", file=sys.stderr)
    raise SystemExit(1)
PY
fi

# 7. readiness report: all three v7 checks true (F2)
if ! python3 - "$ART/readiness/readiness.json" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
if not p.is_file():
    print("readiness/readiness.json missing", file=sys.stderr)
    sys.exit(1)
d = json.loads(p.read_text(encoding="utf-8"))
missing = [k for k in ("constructible", "exportable", "definition_located")
           if d.get(k) is not True]
if missing:
    print(f"readiness checks not all-pass: {missing} in {d}", file=sys.stderr)
    sys.exit(1)
PY
then
  bad "readiness report incomplete or failed"
fi

# 8. runtime shadow-resolution proof (same invocation form as run templates)
if [ -d "$ART/shadow" ]; then
  PKGS="$(python3 -c '
import sys
from pathlib import Path
shadow = Path(sys.argv[1])
print(",".join(sorted(p.name[:-3] if p.suffix == ".py" else p.name
                     for p in shadow.iterdir())))
' "$ART/shadow" 2>/dev/null || true)"
  if [ -n "$PKGS" ]; then
    PROJ_ROOT="$(python3 -c '
import json, sys
from pathlib import Path
try:
    print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["project_root"])
except Exception:
    pass
' "$ART/readiness/readiness.json" 2>/dev/null || true)"
    if [ -n "$PROJ_ROOT" ] && [ -d "$PROJ_ROOT" ]; then
      if ! (cd "$PROJ_ROOT" \
            && ORCA_SHADOW_DIR="$ART/shadow" ORCA_SHADOW_PKGS="$PKGS" \
               PYTHONPATH="$ART/orca_inject:$PROJ_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
               "$PY" "$ART/scripts/assert_shadow.py" >/dev/null 2>"$ART/check_flatten.assert.log"); then
        bad "runtime shadow assertion failed — see check_flatten.assert.log (injection must resolve every shadow pkg inside the shadow tree)"
      fi
    else
      bad "cannot run the shadow assertion: project root unknown (readiness.json lacks project_root)"
    fi
  fi
fi

if [ "$FAIL" -ne 0 ]; then
  echo "FAIL: check_flatten" >&2
  exit 1
fi
echo "PASS: check_flatten" >&2
