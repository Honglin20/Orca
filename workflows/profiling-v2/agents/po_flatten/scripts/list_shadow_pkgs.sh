#!/usr/bin/env bash
# list_shadow_pkgs.sh — mechanically enumerate the shadow's top-level packages:
# a directory yields its name, a *.py file yields the module name (suffix
# stripped), anything else yields nothing. Output: one name per line, sorted.
# The flatten emit consumes this directly — shadow_pkgs must never be
# hand-maintained.
set -euo pipefail

SHADOW="${1:?FATAL: usage: list_shadow_pkgs.sh <shadow_dir>}"
[ -d "$SHADOW" ] || { echo "FATAL: shadow dir not found: $SHADOW" >&2; exit 2; }

cd "$SHADOW"
for e in *; do
  if [ -d "$e" ]; then echo "$e"
  else case "$e" in *.py) echo "${e%.py}";; esac; fi
done | sort
