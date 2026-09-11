"""check_stdlib_clash.py — fail loud when a shadow top-level name collides
with the Python standard library.

The import injection prepends the shadow to PYTHONPATH but never shadows the
stdlib, so a colliding name would silently resolve back to the original
module at import time — the shadow would not be the code that runs. Exit 0
prints the ok line with the enumerated top-level names; exit 1 lists the
collisions on stderr; exit 2 names a bad invocation.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shadow", required=True, help="shadow root directory")
    shadow = Path(ap.parse_args().shadow)
    if not shadow.is_dir():
        print(f"FATAL: shadow dir not found: {shadow}", file=sys.stderr)
        return 2
    names = sorted({(p.name[:-3] if p.suffix == ".py" else p.name)
                    for p in shadow.iterdir()})
    clash = [n for n in names if n in sys.stdlib_module_names]
    if clash:
        print(f"FATAL: shadow top-level names collide with the Python standard "
              f"library: {clash} — rename/restructure the shadow copy is NOT allowed; "
              f"report for a manual decision", file=sys.stderr)
        return 1
    print("stdlib-collision-check: ok", names)
    return 0


if __name__ == "__main__":
    sys.exit(main())
