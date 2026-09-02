"""write_baseline_lock.py — write BASELINE.lock, the structural anchor.

Recomputable and deterministic: the model path and the sha256 of every
shadow *.py file (v7: the pretrained-ckpt anchor is deleted — training
always starts from a fixed-seed random initialization, so there is no
checkpoint to anchor). The lock is written atomically (tmp +
os.replace). Immutability is enforced downstream by the reuse gate, not
here. The `version` field pins the lock schema (v7 = 2): a workspace
whose lock predates it fails the reuse gate loudly and rebuilds via
fresh_start (never silently migrated).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

LOCK_VERSION = 2


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", required=True, help="$ORCA_ARTIFACTS_DIR")
    ap.add_argument("--model-path", required=True)
    ns = ap.parse_args()

    art = Path(ns.artifacts)
    shadow = art / "shadow"
    if not shadow.is_dir():
        print(f"FATAL: shadow dir not found: {shadow}", file=sys.stderr)
        return 2

    lock = {
        "version": LOCK_VERSION,
        "model_path": ns.model_path,
        "py_files_sha256": {
            str(p.relative_to(shadow)).replace("\\", "/"): sha256_file(p)
            for p in sorted(shadow.rglob("*.py"))
        },
    }
    tmp = art / "BASELINE.lock.tmp"
    tmp.write_text(json.dumps(lock, indent=2), encoding="utf-8")
    os.replace(tmp, art / "BASELINE.lock")
    print(f"BASELINE.lock written over {len(lock['py_files_sha256'])} "
          f"shadow py files (schema v{LOCK_VERSION})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
