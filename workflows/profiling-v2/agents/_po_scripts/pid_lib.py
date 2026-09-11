"""pid_lib.py — the single liveness predicate shared by the device ledger
and the emit gates (v7 §6.1; retires the two drifting copies).

One function, three-valued on purpose:

    liveness(pid) -> "alive" | "dead" | "unknown"

    posix          os.kill(pid, 0): ProcessLookupError -> dead,
                   PermissionError -> alive (another user's process is
                   still an owner), else alive.
    non-posix /    "unknown" — liveness CANNOT be determined, and the
    no signal API  caller MUST disclose "liveness unverifiable" and treat
                   the pid as NOT confirmed-alive. Pretending unknown means
                   alive is exactly the phantom-owner bug this module
                   exists to prevent (a recycled pid pinned behind a lock
                   forever).

No process is ever signalled here — this is an existence probe only.
"""
from __future__ import annotations

import os
import sys

ALIVE = "alive"
DEAD = "dead"
UNKNOWN = "unknown"


def liveness(pid: int) -> str:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return DEAD  # a non-positive pid names no process, deterministically
    kill = getattr(os, "kill", None)
    if kill is None:
        return UNKNOWN      # non-posix host: no signal API to probe with
    try:
        kill(pid, 0)
    except ProcessLookupError:
        return DEAD
    except PermissionError:
        return ALIVE  # alive, owned by another user — still an owner
    except OSError:
        # e.g. a PID namespace /proc view that raises where posix says it
        # should not — undecidable, never guessed
        return UNKNOWN
    except TypeError:
        return UNKNOWN      # a broken/absent signal API, same verdict
    return ALIVE


def liveness_disclosed(pid: int, what: str) -> tuple[bool, str | None]:
    """Convenience for callers that want (confirmed_alive, disclosure).

    Returns (True, None) when the pid is confirmed alive, (False, None)
    when confirmed dead, and (False, "<disclosure sentence>") when the
    liveness cannot be determined — the caller MUST surface the
    disclosure (in its output or log), never silently treat the pid as
    alive."""
    state = liveness(pid)
    if state == ALIVE:
        return True, None
    if state == DEAD:
        return False, None
    return False, (f"{what}: liveness unverifiable for pid {pid} on this "
                   f"host (non-posix / no signal API) — treated as NOT "
                   f"confirmed alive; refusing to guess")
