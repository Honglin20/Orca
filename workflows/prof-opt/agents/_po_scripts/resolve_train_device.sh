#!/usr/bin/env bash
# resolve_train_device.sh — resolve the TRAINING device backend, once, at entry
# (v6 §3.1). profile_mode.json is a SEPARATE contract: profiling is
# machine-independent (static/remote) and never feeds training-device
# allocation — the two resolvers do not couple.
#
# Priority (first match wins, fail loud):
#   1. env ORCA_PO_DEVICE_BACKEND non-empty -> explicit declaration. Must be
#      npu|cuda; anything else exits 2 (fail loud, never a silent fallback).
#      resolved_by=env. device_count still comes from the backend's own
#      counter below — a declared backend whose count cannot be determined
#      is a hard error, not a guess.
#   2. npu-smi on PATH -> npu. resolved_by=npu-smi.
#   3. nvidia-smi on PATH OR torch.cuda.is_available() -> cuda.
#      resolved_by=nvidia-smi | torch.cuda.
#   4. none of the above -> exit 2 (a missing trainable device is a hard
#      error — placeholder is valid for PROFILING only; training has no
#      placeholder backend).
#
# device_count: npu -> `npu-smi -l` (the "Total Count" line, falling back to
# counting NPU<n> rows); cuda -> `nvidia-smi -L` (GPU lines) or, when only
# torch is available, torch.cuda.device_count(). A counter that fails or
# yields 0 exits 2.
#
# Output: single-line JSON {"backend", "device_count", "resolved_by"} on
# stdout. Plain mode writes it verbatim to
# $ORCA_ARTIFACTS_DIR/train_device.json WRITE-IF-ABSENT (an existing file is
# the single source and is never rewritten; the fresh payload is still
# printed). --stdout-only resolves and prints WITHOUT touching the file;
# when train_device.json already exists it re-resolves and compares
# {backend, device_count} — a mismatch exits 2 pointing at fresh_start (the
# workspace was resolved for different hardware).
#
# The payload is assembled with printf (not emit_result.py): every field is
# a fixed enum / validated integer, and keeping python3 out of the emit path
# lets the torch-probe level run under a hermetic PATH in tests. The
# --stdout-only REUSE COMPARISON does need a real python3 (it parses the
# frozen JSON) — under a stubbed-python3 hermetic PATH that comparison is
# simply not reachable (no train_device.json to compare against).
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STDOUT_ONLY=0
if [ "${1:-}" = "--stdout-only" ]; then STDOUT_ONLY=1; shift; fi
[ $# -eq 0 ] || { echo "FATAL: unknown argument(s): $* (only --stdout-only is accepted)" >&2; exit 2; }
if [ "$STDOUT_ONLY" -eq 0 ]; then
  ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (resolve_train_device.sh)}"
fi

torch_cuda_available() {
  python3 -c 'import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)' \
    >/dev/null 2>&1
}

torch_cuda_count() {
  python3 -c 'import torch; print(torch.cuda.device_count())' 2>/dev/null
}

npu_smi_count() { # prints the NPU count or fails
  local out
  if ! out="$(npu-smi -l 2>/dev/null)"; then
    return 1
  fi
  local n
  n="$(printf '%s\n' "$out" | sed -n 's/.*Total Count[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -n 1)"
  if [ -z "$n" ]; then
    n="$(printf '%s\n' "$out" | grep -cE '^[[:space:]]*NPU[0-9]+')"
  fi
  [ -n "$n" ] && [ "$n" -gt 0 ] || return 1
  printf '%s' "$n"
}

nvidia_smi_count() { # prints the GPU count or fails
  local out
  if ! out="$(nvidia-smi -L 2>/dev/null)"; then
    return 1
  fi
  local n
  n="$(printf '%s\n' "$out" | grep -c '^GPU ')"
  [ "$n" -gt 0 ] || return 1
  printf '%s' "$n"
}

BACKEND=""; RESOLVED_BY=""; COUNT=""

if [ -n "${ORCA_PO_DEVICE_BACKEND:-}" ]; then
  case "$ORCA_PO_DEVICE_BACKEND" in
    npu|cuda) BACKEND="$ORCA_PO_DEVICE_BACKEND" ;;
    *) echo "FATAL: ORCA_PO_DEVICE_BACKEND must be npu or cuda, got: '$ORCA_PO_DEVICE_BACKEND'" >&2; exit 2 ;;
  esac
  RESOLVED_BY="env"
elif command -v npu-smi >/dev/null 2>&1; then
  BACKEND="npu"; RESOLVED_BY="npu-smi"
elif command -v nvidia-smi >/dev/null 2>&1; then
  BACKEND="cuda"; RESOLVED_BY="nvidia-smi"
elif torch_cuda_available; then
  BACKEND="cuda"; RESOLVED_BY="torch.cuda"
else
  echo "FATAL: no trainable device backend found (ORCA_PO_DEVICE_BACKEND unset, no npu-smi, no nvidia-smi, torch.cuda unavailable) — a missing trainable device is a hard error; placeholder is valid for profiling only" >&2
  exit 2
fi

# device_count from the backend's own counter (env-declared backends included)
case "$BACKEND" in
  npu)
    if ! COUNT="$(npu_smi_count)"; then
      echo "FATAL: backend npu but 'npu-smi -l' yielded no device count — cannot pin device_count (resolved_by=$RESOLVED_BY)" >&2
      exit 2
    fi ;;
  cuda)
    if command -v nvidia-smi >/dev/null 2>&1; then
      if ! COUNT="$(nvidia_smi_count)"; then
        echo "FATAL: backend cuda but 'nvidia-smi -L' yielded no devices — cannot pin device_count (resolved_by=$RESOLVED_BY)" >&2
        exit 2
      fi
    elif ! COUNT="$(torch_cuda_count)" || [ -z "$COUNT" ] || [ "$COUNT" -le 0 ] 2>/dev/null; then
      echo "FATAL: backend cuda but torch.cuda.device_count() yielded no usable count — cannot pin device_count (resolved_by=$RESOLVED_BY)" >&2
      exit 2
    fi ;;
esac

PAYLOAD="$(printf '{"backend": "%s", "device_count": %s, "resolved_by": "%s"}\n' \
  "$BACKEND" "$COUNT" "$RESOLVED_BY")"

if [ "$STDOUT_ONLY" -eq 1 ]; then
  if [ -n "${ORCA_ARTIFACTS_DIR:-}" ] && [ -f "$ORCA_ARTIFACTS_DIR/train_device.json" ]; then
    # reuse comparison: the frozen {backend, device_count} must still hold
    if ! python3 - "$ORCA_ARTIFACTS_DIR/train_device.json" "$BACKEND" "$COUNT" <<'PY'
import json, sys
try:
    frozen = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc:
    print(f"FATAL: train_device.json unparseable: {exc} — rebuild the workspace with fresh_start", file=sys.stderr)
    raise SystemExit(2)
if {"backend": sys.argv[2], "device_count": int(sys.argv[3])} != \
        {"backend": frozen.get("backend"), "device_count": frozen.get("device_count")}:
    print(f"FATAL: train_device.json drift: frozen {frozen.get('backend')!r}x{frozen.get('device_count')!r} "
          f"!= re-resolved {sys.argv[2]!r}x{sys.argv[3]} — the workspace was resolved for different hardware; "
          "rebuild it with fresh_start", file=sys.stderr)
    raise SystemExit(2)
PY
    then exit 2; fi
  fi
  printf '%s\n' "$PAYLOAD"
else
  if [ -f "$ART/train_device.json" ]; then
    echo "train_device.json already resolved — keeping the frozen file (single source)" >&2
  else
    mkdir -p "$ART"
    printf '%s\n' "$PAYLOAD" > "$ART/train_device.json"
  fi
  printf '%s\n' "$PAYLOAD"
fi
