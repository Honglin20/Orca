#!/usr/bin/env bash
# extract_user_pkg.sh — write the user project's top-level import names to .user_pkg.
#
# For this workflow the marker answers one question: which top-level import
# names found in the model entry are USER-OWNED project code (not stdlib, not
# third-party)? That set is the pool the shadow closure is drawn from, and it
# keeps the closure walk from mistaking an installed package for local code.
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

if [[ "$MODEL_PATH" = /* ]]; then
  MODEL_ENTRY="$MODEL_PATH"
else
  MODEL_ENTRY="$PROJECT_ROOT/$MODEL_PATH"
fi
[ -f "$MODEL_ENTRY" ] || {
  echo "FATAL: model entry not found: $MODEL_ENTRY (project_root=$PROJECT_ROOT model_path=$MODEL_PATH)" >&2
  exit 2
}

# Top-level import names from the model entry that do NOT import as
# stdlib/third-party = user project names.
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
    | sort -u | while read -r pkg; do
      [ -n "$pkg" ] || continue
      python3 -c "import $pkg" 2>/dev/null || echo "$pkg"
    done >> "$ARTIFACTS_DIR/.user_pkg"
fi

echo "[extract_user_pkg] wrote $ARTIFACTS_DIR/.user_pkg:" >&2
cat "$ARTIFACTS_DIR/.user_pkg" >&2
