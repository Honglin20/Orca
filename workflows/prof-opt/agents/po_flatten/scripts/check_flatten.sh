#!/usr/bin/env bash
# check_flatten.sh — deterministic gate for po_flatten artifacts.
#
# Re-verifies everything downstream nodes depend on (fail loud, exit 1):
#   1. heartbeat refresh (validation-time touch of .run_lock)
#   2. shadow tree: exists, >=1 top-level name, >=1 .py, no __pycache__/*.pyc
#   3. BASELINE.lock: valid JSON, pinned key set, checksums still match
#      (recomputed over shadow *.py; the pretrained ckpt only when one is
#      anchored — it is reference-only and optional)
#   4. stdlib collision: no shadow top-level name in sys.stdlib_module_names
#   5. deployed tooling: orca_inject pair + scripts/{assert_shadow,render_run,
#      emit_result,deploy_scripts} present
#   6. project_manifest.md: pinned sections + metric direction marked
#   7. readiness/readiness.json: all four checks true
#   8. runtime proof: assert_shadow.py passes under the injection header —
#      every shadow pkg resolves inside the shadow tree, executed in the exact
#      invocation form the run templates use
#
# Usage: check_flatten.sh <model_path> <pretrained_ckpt>
#   (<pretrained_ckpt> may be empty: reference-only, recorded in the lock only
#    when provided)
# Environment: ORCA_ARTIFACTS_DIR (required), ORCA_RUN_ID (heartbeat owner),
#              ORCA_PYTHON (optional; readiness.json "python" wins, python3 last)
set -uo pipefail

MODEL_PATH="${1:?usage: check_flatten.sh <model_path> <pretrained_ckpt>}"
CKPT="${2-}"
ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (check_flatten.sh)}"
RUN_ID="${ORCA_RUN_ID:-unknown-run}"

FAIL=0
bad() { echo "FAIL: $*" >&2; FAIL=1; }
note() { echo "[check_flatten] $*" >&2; }

cd "$ART" || { echo "FATAL: artifacts dir unreachable: $ART" >&2; exit 2; }

# 1. heartbeat (validation-step head touch, keeps the single-writer lock fresh)
printf '{"run_id": "%s", "pid": %s, "ts": %s}\n' \
  "$RUN_ID" "$$" "$(date +%s)" > "$ART/.run_lock.tmp.$$" 2>/dev/null \
  && mv -f "$ART/.run_lock.tmp.$$" "$ART/.run_lock" \
  || note "WARN: cannot refresh .run_lock heartbeat"

# resolve the working interpreter: env > readiness.json > python3
PY="${ORCA_PYTHON:-python3}"
if [ -f "$ART/readiness/readiness.json" ] \
   && grep -q '"python"' "$ART/readiness/readiness.json" 2>/dev/null; then
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
LOCK_OK="$(python3 - "$ART/BASELINE.lock" "$MODEL_PATH" "$CKPT" "$ART/shadow" <<'PY'
import hashlib, json, sys
from pathlib import Path

lock_path, ckpt_raw, shadow = Path(sys.argv[1]), sys.argv[3], Path(sys.argv[4])
ckpt = Path(ckpt_raw) if ckpt_raw else None  # Path("") would normalize to "."
model_path = sys.argv[2]  # lock stores it as a JSON string — compare like-for-like

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

for key in ("model_path", "pretrained_ckpt", "ckpt_sha256", "py_files_sha256"):
    if key not in lock:
        problems.append(f"BASELINE.lock missing key {key!r}")
if problems:
    print(json.dumps({"ok": False, "problems": problems}))
    sys.exit(0)

if lock["model_path"] != model_path:
    problems.append(f"model_path drift: lock={lock['model_path']!r} now={model_path!r}")
if ckpt is None:
    # no reference checkpoint provided: the lock must carry the empty anchor
    if lock.get("pretrained_ckpt") or lock.get("ckpt_sha256"):
        problems.append("pretrained_ckpt drift: lock anchors a ckpt but none is provided now")
else:
    if not ckpt.is_file():
        problems.append(f"pretrained_ckpt missing: {ckpt}")
    else:
        if lock["pretrained_ckpt"] != str(ckpt.resolve()):
            problems.append("pretrained_ckpt path drift (same content, different anchor)")
        if lock["ckpt_sha256"] != sha(ckpt):
            problems.append("pretrained_ckpt content drift (sha256 mismatch)")
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
         scripts/deploy_scripts.sh scripts/placeholder_profiler.py scripts/analyze.py; do
  [ -s "$ART/$f" ] || bad "deployed tooling missing: $ART/$f (run the deploy step)"
done

# 6. project manifest: pinned sections + direction marker
MANIFEST="$ART/project_manifest.md"
if [ ! -s "$MANIFEST" ]; then
  bad "project_manifest.md missing or empty"
else
  for section in "Project Overview" "Model" "Training And Evaluation" "Data And Environment" "Relevant Source Files"; do
    grep -q "## $section" "$MANIFEST" || bad "project_manifest.md missing section '## $section'"
  done
  grep -qE 'higher-better|lower-better' "$MANIFEST" \
    || bad "project_manifest.md does not mark the metric direction (higher-better / lower-better)"
fi

# 7. readiness report: all four checks true
if ! python3 - "$ART/readiness/readiness.json" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
if not p.is_file():
    print("readiness/readiness.json missing", file=sys.stderr)
    sys.exit(1)
d = json.loads(p.read_text(encoding="utf-8"))
missing = [k for k in ("constructible", "exportable", "pretrained_loadable", "definition_located")
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
