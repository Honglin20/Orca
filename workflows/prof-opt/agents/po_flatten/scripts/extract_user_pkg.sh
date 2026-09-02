#!/usr/bin/env bash
# extract_user_pkg.sh — write the user project's top-level import names to .user_pkg.
#
# For this workflow the marker answers one question: which top-level import
# names found in the model entry are USER-OWNED project code (not stdlib, not
# third-party)? That set is the pool the shadow closure is drawn from, and it
# keeps the closure walk from mistaking an installed package for local code.
#
# v7 (F4): the classification runs under the WORKING interpreter
# ($ORCA_PYTHON — it must import torch/onnx just like every downstream
# step) with PYTHONPATH=$PROJECT_ROOT, and decides by the imported module's
# FILE LOCATION (under the project root = user-owned), not by bare import
# success/failure — a user package that happens to be pip-installed too is
# still user-owned when the entry imports the project copy. Names whose
# classification is UNCERTAIN (import fails even with the project root on
# the path, or no resolvable file) are listed explicitly on stderr for the
# node agent to review — never silently dropped into either bucket.
#
# Arg 1: the user project root.
# Arg 2: the model entry path (relative to the project root or absolute —
#        resolved HERE, never string-concatenated by the caller). Must be
#        the ORIGINAL user file, before any shadow copy.
#
# Fail loud: a bad usage, an unresolvable/missing model entry, or an
# unwritable marker exits non-zero — a silently empty .user_pkg would empty
# the closure pool downstream. A model entry with zero import lines is the
# one legitimate empty-marker case (disclosed via a WARN line).
set -euo pipefail

PROJECT_ROOT="${1:?usage: extract_user_pkg.sh <project_root> <model_entry_path>}"
MODEL_PATH="${2:?usage: extract_user_pkg.sh <project_root> <model_entry_path>}"
ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"
PY="${ORCA_PYTHON:-python3}"

if [[ "$MODEL_PATH" = /* ]]; then
  MODEL_ENTRY="$MODEL_PATH"
else
  MODEL_ENTRY="$PROJECT_ROOT/$MODEL_PATH"
fi
[ -f "$MODEL_ENTRY" ] || {
  echo "FATAL: model entry not found: $MODEL_ENTRY (project_root=$PROJECT_ROOT model_path=$MODEL_PATH)" >&2
  exit 2
}
command -v "$PY" >/dev/null 2>&1 || [ -x "$PY" ] || {
  echo "FATAL: working interpreter not found: $PY (set ORCA_PYTHON first — the classifier must run under it)" >&2
  exit 2
}

# Top-level import names from the model entry.
grep_rc=0
raw_imports="$(grep -hE '^[[:space:]]*(from|import)[[:space:]]+[[:alnum:]_]+' "$MODEL_ENTRY")" || grep_rc=$?
if [ "$grep_rc" -gt 1 ]; then
  echo "FATAL: import scan failed on $MODEL_ENTRY (grep rc=$grep_rc)" >&2
  exit 2
fi

: > "$ARTIFACTS_DIR/.user_pkg"
if [ "$grep_rc" -eq 1 ]; then
  echo "WARN: no import lines found in $MODEL_ENTRY — .user_pkg is empty" >&2
else
  printf '%s\n' "$raw_imports" \
    | sed -E 's/^[[:space:]]*(from|import)[[:space:]]+([[:alnum:]_]+).*/\2/' \
    | sort -u > "$ARTIFACTS_DIR/.user_pkg.raw"
  # classify under the working interpreter with the project root importable;
  # every diagnostic line lands on stderr (the log), never swallowed
  PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}" "$PY" - \
    "$PROJECT_ROOT" "$ARTIFACTS_DIR/.user_pkg.raw" "$ARTIFACTS_DIR/.user_pkg" <<'PYEOF'
import importlib
import sys
from pathlib import Path

project_root = Path(sys.argv[1]).resolve()
raw_path, out_path = Path(sys.argv[2]), Path(sys.argv[3])
names = [n.strip() for n in raw_path.read_text(encoding="utf-8").splitlines()
         if n.strip()]

user_owned, uncertain = [], []
for name in names:
    try:
        mod = importlib.import_module(name)
    except Exception as exc:                     # noqa: BLE001 — classified, not raised
        uncertain.append((name, f"import failed: {type(exc).__name__}"))
        continue
    origin = getattr(mod, "__file__", None) or \
        getattr(getattr(mod, "__spec__", None), "origin", None)
    if not origin:
        uncertain.append((name, "no resolvable file (namespace/builtin)"))
        continue
    try:
        under = project_root in Path(origin).resolve().parents
    except OSError:
        uncertain.append((name, f"unresolvable origin: {origin}"))
        continue
    if under:
        user_owned.append(name)
    else:
        print(f"[extract_user_pkg] not user-owned: {name} -> {origin}", file=sys.stderr)

out_path.write_text("".join(n + "\n" for n in sorted(set(user_owned))),
                    encoding="utf-8")
for name, why in uncertain:
    print(f"[extract_user_pkg] UNCERTAIN: {name} ({why}) — review this name "
          f"yourself: is it user project code (add to .user_pkg) or an "
          f"uninstalled third-party dependency?", file=sys.stderr)
PYEOF
fi

echo "[extract_user_pkg] wrote $ARTIFACTS_DIR/.user_pkg:" >&2
cat "$ARTIFACTS_DIR/.user_pkg" >&2
