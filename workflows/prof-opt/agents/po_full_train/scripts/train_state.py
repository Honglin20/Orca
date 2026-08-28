"""train_state.py — one-line liveness verdict for a detached training run.

Reads the pid/rc pair under the given run directory (``.train_pid`` /
``.train_rc`` — the wrapper group leader writes both) and prints exactly one
of:

    DONE rc=<n>        the run finished with that exit code
    RUNNING pid=<n>    no rc yet and the pid still answers kill -0
    DEAD no-rc pid=<n> no rc and the pid is gone (crashed or was killed)

The verdict is the stdout line; the exit code is always 0 (a DEAD verdict is
an observation for the caller's retry path, not a script failure).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    d = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    rc_file, pid_file = d / ".train_rc", d / ".train_pid"
    if rc_file.is_file():
        print(f"DONE rc={rc_file.read_text(encoding='utf-8').strip()}")
        return 0
    try:
        pid = pid_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        pid = ""
    try:
        os.kill(int(pid), 0)
        print(f"RUNNING pid={pid}")
    except (OSError, ValueError):
        print(f"DEAD no-rc pid={pid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
