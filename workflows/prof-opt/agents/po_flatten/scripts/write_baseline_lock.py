"""write_baseline_lock.py — write BASELINE.lock, the structural anchor.

Recomputable and deterministic: the model path, the optional pretrained
checkpoint (reference-only: path + sha, recorded ONLY when provided), and the
sha256 of every shadow *.py file. The lock is written atomically (tmp +
os.replace). Immutability is enforced downstream by the reuse gate, not here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
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
    ap.add_argument("--artifacts", required=True, help="$ORCA_ARTIFACTS_DIR")
    ap.add_argument("--model-path", required=True)
    ap.add_argument("--ckpt", default="",
                    help="pretrained ckpt path; empty string = no reference ckpt")
    ns = ap.parse_args()

    art = Path(ns.artifacts)
    shadow = art / "shadow"
    if not shadow.is_dir():
        print(f"FATAL: shadow dir not found: {shadow}", file=sys.stderr)
        return 2
    ckpt = Path(ns.ckpt) if ns.ckpt else None

    lock = {
        "model_path": ns.model_path,
        "pretrained_ckpt": str(ckpt.resolve()) if ckpt else "",
        "ckpt_sha256": sha256_file(ckpt) if ckpt else "",
        "py_files_sha256": {
            str(p.relative_to(shadow)).replace("\\", "/"): sha256_file(p)
            for p in sorted(shadow.rglob("*.py"))
        },
    }
    tmp = art / "BASELINE.lock.tmp"
    tmp.write_text(json.dumps(lock, indent=2), encoding="utf-8")
    os.replace(tmp, art / "BASELINE.lock")
    print(f"BASELINE.lock written over {len(lock['py_files_sha256'])} shadow py files"
          + (" (pretrained ckpt anchored)" if ckpt else " (no pretrained ckpt)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
