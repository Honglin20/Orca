#!/usr/bin/env bash
# watch_variant.sh — detached per-variant training watchdog.
#
# STATUS: SIGNATURE STUB. The invocation contract below is already wired by
# the po_probe node (claim a card -> render -> detach training -> detach THIS
# watchdog -> emit without waiting). The supervision body — poll loop with
# per-epoch budget streak + warmup, early-stop kill with /proc attribution
# check, terminal eval chain, train_status/ledger-shard updates, the history
# terminal row, and the device-lock release — is delivered by the watchdog
# stage; until then this stub only pins the interface: it validates its
# arguments, writes its own pid (watchdog.pid) plus an ISO8601-stamped
# disclosure line (watchdog.log), and exits 0.
#
# Usage:
#   watch_variant.sh --vid <VID> --device <IDX>
#
# Environment: ORCA_ARTIFACTS_DIR (required — the run workspace root).
# Runs detached (setsid, </dev/null) from the probe node; its lifecycle
# files land at $ORCA_ARTIFACTS_DIR/variants/<VID>/{watchdog.pid,watchdog.log}.
set -uo pipefail

VID=""; DEVICE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --vid)    VID="${2:?--vid needs a value}"; shift 2 ;;
    --device) DEVICE="${2:?--device needs a value}"; shift 2 ;;
    --help)
      echo "usage: watch_variant.sh --vid <VID> --device <IDX>"
      echo "detached per-variant training watchdog (env: ORCA_ARTIFACTS_DIR)"
      exit 0 ;;
    *)
      echo "FATAL: unknown argument $1 (usage: watch_variant.sh --vid <VID> --device <IDX>)" >&2
      exit 2 ;;
  esac
done
[ -n "$VID" ] || { echo "FATAL: --vid is required" >&2; exit 2; }
[ -n "$DEVICE" ] || { echo "FATAL: --device is required" >&2; exit 2; }
case "$DEVICE" in
  ''|*[!0-9]*)
    echo "FATAL: --device must be a non-negative integer, got '$DEVICE'" >&2; exit 2 ;;
esac

ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (watch_variant.sh)}"
VDIR="$ART/variants/$VID"
mkdir -p "$VDIR"
echo $$ > "$VDIR/watchdog.pid"
echo "$(date -u +%FT%TZ) watchdog alive: vid=$VID device=$DEVICE pid=$$ — stub invocation only; the supervision body is not yet delivered and the training is unsupervised until it is" >> "$VDIR/watchdog.log"
exit 0
