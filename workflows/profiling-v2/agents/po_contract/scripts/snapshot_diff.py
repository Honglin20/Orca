"""snapshot_diff.py — exemptions list from a pre/post snapshot pair.

An exemption is every file the contract-stage dry-runs touched inside the
user project: present in exactly one snapshot (created/deleted) or sha-changed
between the two. Output: {"exemptions": [relpath, ...]} sorted for stable
disclosure.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"FATAL: snapshot not found: {p}", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as exc:
        print(f"FATAL: snapshot {p} is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pre", required=True, help="snapshot_tree.py pre output")
    ap.add_argument("--post", required=True, help="snapshot_tree.py post output")
    ap.add_argument("--out", required=True, help="exemptions JSON output path")
    ns = ap.parse_args()

    pre = load(Path(ns.pre))
    post = load(Path(ns.post))
    diff = sorted(set(pre) ^ set(post)) + \
        sorted(k for k in set(pre) & set(post) if pre[k] != post[k])
    out = Path(ns.out)
    out.write_text(json.dumps({"exemptions": diff}, indent=2), encoding="utf-8")
    print(json.dumps({"exemptions": diff}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
