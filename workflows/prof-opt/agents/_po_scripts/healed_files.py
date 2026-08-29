"""healed_files.py — emit-field helper: a healed-file list as JSON.

Prints the marker file's non-empty lines as a JSON array (an absent or empty
file prints ``[]``) for ``emit_result.py --field "healed_files=$(...)"``. The
absent-file case is the normal steady state, never an error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", required=True,
                    help="healed-file marker path, e.g. $ORCA_ARTIFACTS_DIR/.po_probe_healed.txt")
    p = Path(ap.parse_args().path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.is_file() else []
    print(json.dumps([ln for ln in lines if ln.strip()]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
