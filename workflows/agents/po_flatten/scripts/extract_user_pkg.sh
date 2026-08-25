#!/usr/bin/env bash
# extract_user_pkg.sh — write the user project's top-level import names to .user_pkg.
#
# For this workflow the marker answers one question: which top-level import
# names found in the model entry are USER-OWNED project code (not stdlib, not
# third-party)? That set is the pool the shadow closure is drawn from, and it
# keeps the closure walk from mistaking an installed package for local code.
#
# Arg 1: the model entry file path (project_root/model_path — the ORIGINAL
#        user file, before any shadow copy). Never fails hard (|| true): a
#        missing marker only downgrades downstream checks to a warning.
set -uo pipefail

MODEL_ENTRY="${1:?usage: extract_user_pkg.sh <model_entry_path>}"
ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"

# Top-level import names from the model entry that do NOT import as
# stdlib/third-party = user project names.
grep -rhE '^\s*(from|import)\s+\w+' "$MODEL_ENTRY" 2>/dev/null \
  | sed -E 's/^\s*(from|import)\s+(\w+).*/\2/' \
  | sort -u | while read -r pkg; do
    [ -n "$pkg" ] || continue
    python3 -c "import $pkg" 2>/dev/null || echo "$pkg"
  done > "$ARTIFACTS_DIR/.user_pkg" || true

echo "[extract_user_pkg] wrote $ARTIFACTS_DIR/.user_pkg:" >&2
cat "$ARTIFACTS_DIR/.user_pkg" >&2 || true
