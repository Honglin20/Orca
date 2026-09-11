"""shadow_pkgs_csv.py — resolve the shadow package list to a comma-joined CSV.

Single resolution order: ``contracts.json`` ``shadow.shadow_pkgs`` when the
contracts are already assembled, else ``readiness/readiness.json``
``shadow_pkgs`` (the list the flatten stage pinned). Prints the CSV on stdout
for run-template ``--set shadow_pkgs=...``; neither source present → exit 1
fail loud.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", required=True, help="$ORCA_ARTIFACTS_DIR")
    art = Path(ap.parse_args().artifacts)

    for src, path in (("contracts.json", ("shadow", "shadow_pkgs")),
                      ("readiness/readiness.json", ("shadow_pkgs",))):
        p = art / src
        if p.is_file():
            d = json.loads(p.read_text(encoding="utf-8"))
            for key in path:
                d = d[key]
            print(",".join(d))
            return 0
    print("FATAL: shadow_pkgs not found in contracts.json or readiness.json",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
