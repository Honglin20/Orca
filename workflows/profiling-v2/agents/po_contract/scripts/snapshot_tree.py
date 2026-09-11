"""snapshot_tree.py — sha256 snapshot of a directory tree (side-effect diffing).

Maps every snapshotted file to its sha256 so a pre/post pair reveals every
in-place change a dry-run made. Skips non-files and the workspace/VCS noise
entries: a top-level ``artifacts`` directory, any ``.git`` / ``__pycache__``
component. Output JSON is sorted for stable diffs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="directory to snapshot")
    ap.add_argument("--out", required=True, help="snapshot JSON output path")
    ns = ap.parse_args()

    root, out = Path(ns.root), Path(ns.out)
    if not root.is_dir():
        print(f"FATAL: snapshot root not found: {root}", file=sys.stderr)
        return 2

    snap = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root)).replace("\\", "/")
        parts = rel.split("/")
        if not p.is_file() or parts[0] == "artifacts" or ".git" in parts or "__pycache__" in parts:
            continue
        snap[rel] = sha256_file(p)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap, indent=2, sort_keys=True), encoding="utf-8")
    print(f"snapshot: {len(snap)} files -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
