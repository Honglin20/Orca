"""write_done_marker.py — sha-pinned DONE marker for a variant.

Writes ``variants/<vid>/DONE`` recording the vid, the sha256 of the variant's
``declaration.json``, and a UTC timestamp. The marker is the reuse gate: a
DONE whose sha no longer matches the declaration fails loud on re-entry
(never silently reused). Requires ORCA_ARTIFACTS_DIR in the environment.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vid", required=True, help="variant id, e.g. r1-01")
    ns = ap.parse_args()

    art = os.environ.get("ORCA_ARTIFACTS_DIR", "")
    if not art:
        print("FATAL: ORCA_ARTIFACTS_DIR not set (write_done_marker.py)",
              file=sys.stderr)
        return 2
    d = Path(art) / "variants" / ns.vid
    decl = d / "declaration.json"
    if not decl.is_file():
        print(f"FATAL: declaration not found: {decl}", file=sys.stderr)
        return 2

    marker = {
        "vid": ns.vid,
        "declaration_sha256": hashlib.sha256(
            decl.read_text(encoding="utf-8").encode()).hexdigest(),
        "ts": datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    (d / "DONE").write_text(json.dumps(marker), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
