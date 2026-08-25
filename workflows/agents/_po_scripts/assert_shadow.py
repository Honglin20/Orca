#!/usr/bin/env python3
"""assert_shadow.py — runtime proof that shadow injection actually took effect.

Must run in the EXACT same invocation form as the training entry (injection
header exported, same interpreter); a bare `python -c` assertion is a
different form and can green-light a broken run. Embedded in every rendered
run script by render_run.sh, before the entry command.

Checks: for every name in ORCA_SHADOW_PKGS, import it and assert its
``__file__`` resolves under ORCA_SHADOW_DIR. Any failure -> non-zero exit.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path


def _is_under(path: Path, root: Path) -> bool:
    """True iff ``path`` equals ``root`` or lives somewhere below it."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def main() -> int:
    raw_shadow = os.environ.get("ORCA_SHADOW_DIR", "")
    pkgs = [p for p in os.environ.get("ORCA_SHADOW_PKGS", "").split(",") if p]
    if not raw_shadow or not pkgs:
        print(
            f"assert_shadow: ORCA_SHADOW_DIR/ORCA_SHADOW_PKGS not set "
            f"(dir={raw_shadow!r} pkgs={pkgs!r}) — injection header missing",
            file=sys.stderr,
        )
        return 2

    shadow = Path(raw_shadow).resolve()
    failures: list[dict] = []
    resolved: dict[str, str] = {}
    for name in pkgs:
        try:
            mod = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — report, then fail loud
            failures.append({"module": name, "error": f"import failed: {type(exc).__name__}: {exc}"})
            continue
        mod_file = getattr(mod, "__file__", None)
        if not mod_file:
            failures.append({"module": name, "error": "module has no __file__ (namespace package?)"})
            continue
        real = Path(mod_file).resolve()
        resolved[name] = str(real)
        if not _is_under(real, shadow):
            failures.append({"module": name, "resolved": str(real), "expected_root": str(shadow)})

    if failures:
        for f in failures:
            print(f"assert_shadow: FAIL {json.dumps(f)}", file=sys.stderr)
        print(
            "assert_shadow: shadow resolution broken — continuing would train "
            "the ORIGINAL code. Aborting.",
            file=sys.stderr,
        )
        return 1

    print(json.dumps({"assert_shadow": "ok", "shadow_dir": str(shadow), "resolved": resolved}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
