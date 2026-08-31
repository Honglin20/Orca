#!/usr/bin/env python3
"""Pre-return gate for po_probe (v6, SPEC §6.2).

Verifies the launch-and-release disk state before emit, for every variant
whose LATEST history row is `latency_pass` (the training set; a vid that
already reached a terminal row is out of scope):

  1. verdict precondition: variants/<vid>/verdict.json carries
     makespan_cycles <= the frozen target_cycles (base/origin_anchor.json).
     A missing/above-line verdict is a torn workspace and must fail here.
  2. device lock: a devices/<idx>.lock exists whose vid matches the vid.
  3. training liveness: variants/<vid>/train/train.pid records a LIVE pid
     (with /proc cmdline attribution when readable — a recycled pid never
     counts) OR the training already reached a terminal state
     (variants/<vid>/train_status.json stage in killed|done|failed — the
     watchdog's product) with the terminal file on disk.
  4. watchdog: variants/<vid>/watchdog.pid records a LIVE pid (with /proc
     cmdline attribution — a recycled pid never counts) OR the training
     already reached a terminal state (a guardian that terminalized and
     exited before this gate ran is a legitimate fast path, its terminal
     file is the proof).
  5. liveness record: variants/<vid>/train/liveness.json parses and records
     epoch1_ok true.

Structural completeness only — launch outcomes and verdicts themselves are
never re-judged here (Step 0 already compared the verdict once; this gate
re-asserts it end-to-end).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

TERMINAL_TRAIN_STAGES = frozenset({"killed", "done", "failed"})


def _load_json(path: Path, what: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise ValueError(f"{what} unparseable: {path} ({exc})") from exc


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        # os.kill(pid, 0) on Windows TERMINATES the target — never signal a
        # pid we are only checking. /proc (when present) is the existence
        # probe; without it liveness cannot be disproven and counts alive.
        proc = Path(f"/proc/{pid}")
        return proc.exists() if proc.parent.is_dir() else True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by another user — still an owner
    return True


def _pid_attributed(pid: int, expect: str) -> bool:
    """True when the pid's /proc cmdline references `expect`. An unreadable
    /proc (non-POSIX host) cannot disprove attribution — count it alive; a
    READABLE cmdline that disagrees (pid reuse) must fail."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True
    if not cmdline:
        return True
    return expect in cmdline.replace(b"\0", b" ").decode("utf-8", "replace")


def _check_vid(art: Path, vid: str, target: int, problems: list[str]) -> None:
    # 1. verdict precondition (inclusive boundary: == target holds)
    try:
        verdict = _load_json(art / "variants" / vid / "verdict.json",
                             f"{vid} verdict.json")
    except ValueError as exc:
        problems.append(str(exc))
        verdict = None
    if not isinstance(verdict, dict):
        problems.append(f"{vid} verdict.json missing or not an object — torn "
                        "workspace (propose and probe disagree)")
    else:
        ms = verdict.get("makespan_cycles")
        if not isinstance(ms, int) or isinstance(ms, bool):
            problems.append(f"{vid} verdict.json carries no makespan_cycles — "
                            "torn workspace")
        elif ms > target:
            problems.append(
                f"{vid} makespan {ms} > frozen target {target} — torn "
                "workspace (the verdict changed between propose and probe)")

    # 2. device lock named for this vid
    vdir = art / "variants" / vid
    lock_hit = None
    for lock in sorted((art / "devices").glob("*.lock")) \
            if (art / "devices").is_dir() else []:
        try:
            doc = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(doc, dict) and doc.get("vid") == vid:
            lock_hit = lock
            break
    if lock_hit is None:
        problems.append(f"{vid} has no devices/<idx>.lock naming it (the "
                        "training must run on a ledger-claimed card)")

    # 3. training pid alive OR terminal state with its terminal file
    pid_path = vdir / "train" / "train.pid"
    pid: int | None = None
    if not pid_path.is_file():
        problems.append(f"{vid} train/train.pid missing (no launch state)")
    else:
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            problems.append(f"{vid} train/train.pid is not an int: "
                            f"{pid_path.read_text(encoding='utf-8')!r}")
    try:
        train_status = _load_json(vdir / "train_status.json",
                                  f"{vid} train_status.json")
    except ValueError as exc:
        train_status = None
        problems.append(str(exc))
    terminal = (isinstance(train_status, dict)
                and train_status.get("stage") in TERMINAL_TRAIN_STAGES)
    if pid is not None and not terminal:
        if not _pid_alive(pid):
            problems.append(
                f"{vid} training pid {pid} is dead with no terminal "
                "train_status.json (stage killed|done|failed) — an "
                "unjudged dead launch")
        elif not _pid_attributed(pid, "train.rendered.sh"):
            problems.append(
                f"{vid} training pid {pid} is alive but its cmdline does not "
                "reference train.rendered.sh (pid reuse — not our training)")

    # 4. watchdog guardian: pid alive (attributed) OR terminal state (the
    #    real guardian loops until terminal — a dead pid with no terminal
    #    train_status.json means the training is unsupervised)
    wpid_path = vdir / "watchdog.pid"
    if not wpid_path.is_file():
        problems.append(f"{vid} watchdog.pid missing (the launch is "
                        "incomplete without its guardian)")
    else:
        try:
            wpid = int(wpid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            problems.append(f"{vid} watchdog.pid is not an int: "
                            f"{wpid_path.read_text(encoding='utf-8')!r}")
        else:
            if not terminal:
                if not _pid_alive(wpid):
                    problems.append(
                        f"{vid} watchdog pid {wpid} is dead with no terminal "
                        "train_status.json (stage killed|done|failed) — the "
                        "training is unsupervised")
                elif not _pid_attributed(wpid, "watch_variant.sh"):
                    problems.append(
                        f"{vid} watchdog pid {wpid} is alive but its cmdline "
                        "does not reference watch_variant.sh (pid reuse — not "
                        "our guardian)")

    # 5. liveness record
    try:
        liveness = _load_json(vdir / "train" / "liveness.json",
                              f"{vid} liveness.json")
    except ValueError as exc:
        liveness = None
        problems.append(str(exc))
    if not isinstance(liveness, dict):
        if not terminal:
            problems.append(f"{vid} train/liveness.json missing (the epoch-1 "
                            "liveness proof is a hard emit precondition)")
    elif liveness.get("epoch1_ok") is not True:
        problems.append(f"{vid} liveness.json epoch1_ok is not true")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", required=True)
    ns = ap.parse_args()
    art = Path(ns.artifacts)
    problems: list[str] = []

    scripts_dir = art / "scripts"
    if not scripts_dir.is_dir():
        print("check_probe_emit: FAIL scripts/ missing", file=sys.stderr)
        return 1
    sys.path.insert(0, str(scripts_dir))
    try:
        import history_lib
        import round_state
    except Exception as exc:
        print(f"check_probe_emit: FAIL cannot import shared scripts: {exc}",
              file=sys.stderr)
        return 1

    try:
        r = round_state.current_round(art)
    except Exception as exc:
        print(f"check_probe_emit: FAIL round unavailable: {exc}",
              file=sys.stderr)
        return 1

    try:
        anchor = _load_json(art / "base" / "origin_anchor.json",
                            "base/origin_anchor.json")
    except ValueError as exc:
        anchor = None
        problems.append(str(exc))
    target = anchor.get("target_cycles") if isinstance(anchor, dict) else None
    if not isinstance(target, int) or isinstance(target, bool):
        problems.append("base/origin_anchor.json target_cycles unavailable "
                        "(baseline stage incomplete)")
        target = None

    try:
        latest = history_lib.read_latest(art / "history.jsonl")
    except Exception as exc:
        print(f"check_probe_emit: FAIL history unreadable: {exc}",
              file=sys.stderr)
        return 1
    pending = sorted(vid for vid, row in latest.items()
                     if row.get("outcome") == "latency_pass")

    for vid in pending:
        if target is not None:
            _check_vid(art, vid, target, problems)

    if problems:
        for p in problems:
            print(f"check_probe_emit: FAIL {p}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "round": r, "probed": pending}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
