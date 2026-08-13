#!/usr/bin/env bash
# extract_user_pkg.sh — write the user project's top-level package names to .user_pkg.
# Arg 1: the model entry file path (project_root/model_path). Never fails (|| true).
set -uo pipefail

MODEL_ENTRY="${1:?usage: extract_user_pkg.sh <model_entry_path>}"
ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"

# Extract non-stdlib/third-party imports from the model entry file (not the flat file —
# the flat file has already inlined all local code).
grep -rhE '^\s*(from|import)\s+\w+' "$MODEL_ENTRY" 2>/dev/null \
  | sed -E 's/^\s*(from|import)\s+(\w+).*/\2/' \
  | sort -u | while read pkg; do
    python3 -c "import $pkg" 2>/dev/null || echo "$pkg"   # not importable as third-party = user package
  done > "$ARTIFACTS_DIR/.user_pkg" || true
