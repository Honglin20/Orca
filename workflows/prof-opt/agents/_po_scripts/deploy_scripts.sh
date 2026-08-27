#!/usr/bin/env bash
# deploy_scripts.sh — idempotently deploy the shared deterministic scripts into
# the artifacts workspace. Run by po_flatten on EVERY run entry, fresh or reuse
# (safe to re-run; cp -f full overwrite).
#   *.py / *.sh      -> $ORCA_ARTIFACTS_DIR/scripts/
#   orca_inject/*    -> $ORCA_ARTIFACTS_DIR/orca_inject/   (artifacts ROOT, not
#                       scripts/ — run templates point PYTHONPATH at the root)
# Version stamp: after each deploy the manifest over the deployed set is
#   recomputed and written to scripts/.VERSION (single-line JSON
#   {"manifest": "<sha256>"}). The manifest hashes the sorted
#   (name, sha256(content)) sequence of every deployed *.py/*.sh — .VERSION
#   itself is never part of the set.
# --verify: read-only mode — recompute the manifest over the CURRENT deployed
#   set and compare with .VERSION; missing file or mismatch exits 1 (tampered
#   or half-deployed workspace). Consumers run this before acting.
# Retired scripts: any deployed *.py/*.sh whose name is NOT in this source
#   tree is deleted (a stale copy would keep executing after an upgrade).
# stdout: single-line JSON (pure-stdout contract); logs go to stderr.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (deploy_scripts.sh)}"
VERIFY=0
[ "${1:-}" = "--verify" ] && VERIFY=1
[ $# -eq 0 ] || [ "$VERIFY" -eq 1 ] || {
  echo "FATAL: unknown argument(s): $* (only --verify is accepted)" >&2; exit 2; }

# manifest over a script directory: sha256 of the sorted (name, sha256) pairs
manifest_of() { # manifest_of <dir> -> prints the single sha256
  python3 - "$1" <<'PY'
import hashlib, sys
from pathlib import Path
d = Path(sys.argv[1])
files = sorted([p for p in d.iterdir() if p.is_file()
                and p.suffix in (".py", ".sh") and p.name != ".VERSION"],
               key=lambda p: p.name)
h = hashlib.sha256()
for p in files:
    h.update(p.name.encode("utf-8"))
    h.update(hashlib.sha256(p.read_bytes()).digest())
print(h.hexdigest())
PY
}

if [ "$VERIFY" -eq 1 ]; then
  if [ ! -f "$ART/scripts/.VERSION" ]; then
    echo "FATAL: $ART/scripts/.VERSION missing — deployed set has no version stamp (tampered or never verified deploy); re-run the entry deploy or rebuild with fresh_start" >&2
    exit 1
  fi
  NOW="$(manifest_of "$ART/scripts")"
  WANT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["manifest"])' "$ART/scripts/.VERSION")"
  if [ "$NOW" != "$WANT" ]; then
    echo "FATAL: deployed script set does not match its .VERSION stamp (now $NOW, stamp $WANT) — tampered or half-deployed workspace; 部署件版本戳不符，需 fresh_start 重建工作区" >&2
    exit 1
  fi
  echo "deploy verify: ok (manifest $NOW)" >&2
  exit 0
fi

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

# version stamp: recompute AFTER retirement so the manifest covers exactly
# the live deployed set (atomic write — a torn stamp fails --verify loudly)
MANIFEST="$(manifest_of "$ART/scripts")"
printf '{"manifest": "%s"}\n' "$MANIFEST" > "$ART/scripts/.VERSION.tmp.$$" \
  && mv -f "$ART/scripts/.VERSION.tmp.$$" "$ART/scripts/.VERSION"

echo "deployed py=$copied_py sh=$copied_sh orca_inject=$copied_inject orphans_removed=$orphans_removed manifest=$MANIFEST -> $ART" >&2
PY_BIN="${ORCA_PYTHON:-python3}"
"$PY_BIN" "$ART/scripts/emit_result.py" --field "scripts_dir=$ART/scripts" \
  --field "orca_inject_dir=$ART/orca_inject" \
  --field "py=$copied_py" --field "sh=$copied_sh" \
  --field "orca_inject=$copied_inject" \
  --field "orphans_removed=$orphans_removed" \
  --field "manifest=$MANIFEST"
