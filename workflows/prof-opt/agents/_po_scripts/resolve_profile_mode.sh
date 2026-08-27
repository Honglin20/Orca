#!/usr/bin/env bash
# resolve_profile_mode.sh — resolve the profiling mode, once, at entry.
#
# Priority (first match wins):
#   1. env ORCA_PO_NPU_CHIP non-empty  -> mfu mode. chip must be 6613|1951,
#      ORCA_PO_NPU_PRECISION (default INT8, enum INT8|INT16|AMP) and
#      ORCA_PO_NPU_CORES (default 1, enum 1|2|4) are validated the same way;
#      an illegal value exits 2 (fail loud, never a silent fallback).
#      resolved_by=env.
#   2. npu-smi on PATH                  -> mfu mode. The chip model is parsed
#      from the MODEL COLUMN of `npu-smi info` (column-aware table parse —
#      a bare substring scan of the whole output would false-positive on
#      values like "1951 MB" in memory columns). Model 6613 -> 6613,
#      1951 -> 1951; anything unparseable or ambiguous exits 2 with guidance
#      to set ORCA_PO_NPU_CHIP explicitly. precision/cores keep defaults.
#      resolved_by=npu-smi.
#   3. otherwise                        -> placeholder mode (the built-in
#      estimator; no NPU environment needed). chip="" precision=null
#      core_num=null. resolved_by=fallback.
#
# Output: single-line JSON {"mode", "chip", "precision", "core_num",
# "resolved_by"} on stdout, written verbatim to
# $ORCA_ARTIFACTS_DIR/profile_mode.json (the single source every profiling
# consumer reads). --stdout-only resolves and prints WITHOUT touching the
# file (read-only re-resolution for reuse-time comparison).
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STDOUT_ONLY=0
if [ "${1:-}" = "--stdout-only" ]; then STDOUT_ONLY=1; shift; fi
[ $# -eq 0 ] || { echo "FATAL: unknown argument(s): $* (only --stdout-only is accepted)" >&2; exit 2; }
if [ "$STDOUT_ONLY" -eq 0 ]; then
  ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (resolve_profile_mode.sh)}"
fi

MODE=""; CHIP=""; PRECISION=""; CORES=""; RESOLVED_BY=""

if [ -n "${ORCA_PO_NPU_CHIP:-}" ]; then
  case "$ORCA_PO_NPU_CHIP" in
    6613|1951) CHIP="$ORCA_PO_NPU_CHIP" ;;
    *) echo "FATAL: ORCA_PO_NPU_CHIP must be 6613 or 1951 (mfu mode), got: '$ORCA_PO_NPU_CHIP'" >&2; exit 2 ;;
  esac
  PRECISION="${ORCA_PO_NPU_PRECISION:-INT8}"
  case "$PRECISION" in
    INT8|INT16|AMP) ;;
    *) echo "FATAL: ORCA_PO_NPU_PRECISION must be INT8/INT16/AMP, got: '$PRECISION'" >&2; exit 2 ;;
  esac
  CORES="${ORCA_PO_NPU_CORES:-1}"
  case "$CORES" in
    1|2|4) ;;
    *) echo "FATAL: ORCA_PO_NPU_CORES must be 1/2/4, got: '$CORES'" >&2; exit 2 ;;
  esac
  MODE="mfu"; RESOLVED_BY="env"
elif command -v npu-smi >/dev/null 2>&1; then
  if ! NPU_SMI_OUT="$(npu-smi info 2>/dev/null)"; then
    echo "FATAL: npu-smi is present but 'npu-smi info' failed — set ORCA_PO_NPU_CHIP explicitly to declare the chip" >&2
    exit 2
  fi
  CHIP="$(python3 - "$NPU_SMI_OUT" <<'PY'
import sys

enum = {"6613", "1951"}
rows = []
for line in sys.argv[1].splitlines():
    if "|" not in line:
        continue
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if not cells:
        continue
    if any(c and set(c) <= {"=", "-", " "} for c in cells):
        continue  # separator rows
    rows.append(cells)

name_idx = None
for cells in rows:
    if any("npu-smi" in c.lower() for c in cells):
        continue  # version banner row
    for i, c in enumerate(cells):
        if "Name" in c or "型号" in c:
            name_idx = i
            break
    if name_idx is not None:
        break
found = set()
if name_idx is not None:
    for cells in rows:
        if len(cells) > name_idx and cells[name_idx]:
            token = cells[name_idx].split()[-1]  # model token (merged NPU+Name cells)
            if token in enum:
                found.add(token)
if len(found) == 1:
    print(found.pop())
PY
)"
  if [ -z "$CHIP" ]; then
    echo "FATAL: npu-smi is present but its output carries no unambiguous chip model (6613/1951) in the model column — set ORCA_PO_NPU_CHIP explicitly to declare the chip" >&2
    exit 2
  fi
  PRECISION="INT8"; CORES="1"
  MODE="mfu"; RESOLVED_BY="npu-smi"
else
  MODE="placeholder"; RESOLVED_BY="fallback"
fi

# assemble the payload (placeholder mode: chip empty, precision/core_num null;
# chip/precision stay JSON strings even when they look numeric — the schema
# pins chip as a string so consumers never str/num-branch)
if [ "$MODE" = "mfu" ]; then
  P_JSON="\"$PRECISION\""; C_JSON="$CORES"
else
  P_JSON="null"; C_JSON="null"
fi
PAYLOAD="$(python3 "$SELF_DIR/emit_result.py" \
  --field "mode=$MODE" --field "chip=\"$CHIP\"" --field "precision=$P_JSON" \
  --field "core_num=$C_JSON" --field "resolved_by=$RESOLVED_BY")"

if [ "$STDOUT_ONLY" -eq 1 ]; then
  printf '%s\n' "$PAYLOAD"
else
  printf '%s\n' "$PAYLOAD" > "$ART/profile_mode.json"
  printf '%s\n' "$PAYLOAD"
fi
