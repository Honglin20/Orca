#!/usr/bin/env bash
# deploy_scripts.sh — idempotently deploy the shared deterministic scripts into
# the artifacts workspace. Run by po_flatten once per run entry (safe to re-run).
#   *.py / *.sh      -> $ORCA_ARTIFACTS_DIR/scripts/
#   orca_inject/*    -> $ORCA_ARTIFACTS_DIR/orca_inject/   (artifacts ROOT, not
#                       scripts/ — run templates point PYTHONPATH at the root)
# Retired scripts: any deployed *.py/*.sh whose name is NOT in this source
#   tree is deleted (a stale copy would keep executing after an upgrade).
# stdout: single-line JSON (pure-stdout contract); logs go to stderr.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (deploy_scripts.sh)}"

mkdir -p "$ART/scripts" "$ART/orca_inject"

copied_py=0; copied_sh=0; copied_inject=0
for f in "$SRC"/*.py; do [ -f "$f" ] || continue; cp -f "$f" "$ART/scripts/"; copied_py=$((copied_py+1)); done
for f in "$SRC"/*.sh;  do [ -f "$f" ] || continue; cp -f "$f" "$ART/scripts/"; chmod +x "$ART/scripts/$(basename "$f")"; copied_sh=$((copied_sh+1)); done
for f in "$SRC"/orca_inject/*; do [ -f "$f" ] || continue; cp -f "$f" "$ART/orca_inject/"; copied_inject=$((copied_inject+1)); done

# defensive retirement: the deployed set must equal the shipped set — an
# orphan left by an older deploy would silently keep executing (nothing else
# re-deploys between runs; fresh_start wipes it, reuse does not)
declare -A shipped=()
for f in "$SRC"/*.py "$SRC"/*.sh; do [ -f "$f" ] || continue; shipped["$(basename "$f")"]=1; done
orphans_removed=0
for f in "$ART"/scripts/*.py "$ART"/scripts/*.sh; do
  [ -f "$f" ] || continue
  name="$(basename "$f")"
  if [ -z "${shipped[$name]:-}" ]; then
    echo "retired orphan script removed from workspace: $name" >&2
    rm -f "$f"
    orphans_removed=$((orphans_removed+1))
  fi
done

# Fail loud if the injection pair is missing after deploy — every run template
# depends on both files being present.
for required in "$ART/orca_inject/sitecustomize.py" "$ART/orca_inject/header.env" \
                "$ART/scripts/assert_shadow.py"; do
  [ -f "$required" ] || { echo "FATAL: deploy incomplete, missing $required" >&2; exit 1; }
done

echo "deployed py=$copied_py sh=$copied_sh orca_inject=$copied_inject orphans_removed=$orphans_removed -> $ART" >&2
PY_BIN="${ORCA_PYTHON:-python3}"
"$PY_BIN" "$ART/scripts/emit_result.py" --field "scripts_dir=$ART/scripts" \
  --field "orca_inject_dir=$ART/orca_inject" \
  --field "py=$copied_py" --field "sh=$copied_sh" \
  --field "orca_inject=$copied_inject" \
  --field "orphans_removed=$orphans_removed"
