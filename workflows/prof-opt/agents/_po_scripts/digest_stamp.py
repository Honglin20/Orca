#!/usr/bin/env python3
"""digest_stamp.py — the mechanical freshness seal for base/history_digest.md.

The digest is a DERIVED narrative cache (the history-curator subagent's
product); history.jsonl stays the only truth and base/frontier.json the only
number layer. This script is the boundary that keeps the cache honest —
the LLM never writes or transcribes the seal:

  stamp  run in Step 4 AFTER history-curator rewrote base/history_digest.md
         (both ending paths). Mechanically verifies the curator contract
         (sentinel first line, the global-lessons heading, one round block
         per closed round), enforces the byte cap,
         then writes base/history_digest.stamp.json atomically:
           {covered_round, history_lines, history_last_ts,
            digest_sha256, digest_bytes}
         covered_round = round_state.current_round (the round whose close
         this digest reflects) — derived from disk, never passed by hand.

  check  run at the NEXT round's Step 0 (and, stricter, by the emit gate via
         problems_for_emit). The digest must exist, be exactly the stamped
         bytes, and cover every history row that belongs to a closed round:

           R*       = highest round number in history.jsonl (0 when none)
           needed   = R*  when rounds/<R*> is resumable (a parseable
                      proposals.json with round == R* — the normal closed
                      round, or one a re-entered node may resume)
                    = R*-1 when rounds/<R*> is NOT resumable (a torn
                      mid-round workspace: its narrative is not closed, the
                      previous stamp is the honest state)
           required = needed >= 1

         A missing or lagging seal on a required digest fails loud (exit 2):
         exploring on a stale narrative is worse than stopping.

stdout: single-line JSON; every failure is a stderr reason + exit 2.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import history_lib  # noqa: E402
import round_state  # noqa: E402

DIGEST_PATH = "base/history_digest.md"
STAMP_PATH = "base/history_digest.stamp.json"
DIGEST_MAX_BYTES = 24_000
CURATOR_SENTINEL = "[subagent:history-curator v1 HDC7Q2]"
DIGEST_GLOBAL_HEADING = "## Global lessons"
DIGEST_ROUND_HEADING_PREFIX = "## Round "


class SealError(RuntimeError):
    """A digest seal violation — callers must fail loud, never patch."""


def digest_path(art: Path) -> Path:
    return art / DIGEST_PATH


def stamp_path(art: Path) -> Path:
    return art / STAMP_PATH


def _digest_bytes(digest: Path) -> bytes:
    try:
        data = digest.read_bytes()
    except FileNotFoundError as exc:
        raise SealError(f"history digest missing: {digest}") from exc
    if not data.strip():
        raise SealError(f"history digest is empty: {digest}")
    return data


def _round_heading(round_no: int) -> re.Pattern[str]:
    """`## Round r<R>` with a hard boundary — r1 must not be satisfied by a
    stray `## Round r10` block."""
    return re.compile(
        rf"^{re.escape(DIGEST_ROUND_HEADING_PREFIX)}r{round_no}(?!\d)")


def _check_curator_contract(text: str, history_rounds: set[int]) -> None:
    """Mechanical subset of the curator contract: sentinel first line, the
    global-lessons heading, and one round block per CLOSED round in history
    — a digest that keeps only the newest block has silently dropped the
    cross-round narrative, which is exactly what the seal exists to catch."""
    lines = [l.strip() for l in text.splitlines()]
    if not lines or lines[0] != CURATOR_SENTINEL:
        raise SealError(
            f"history digest first line is not the curator sentinel "
            f"{CURATOR_SENTINEL!r}")
    if DIGEST_GLOBAL_HEADING not in set(lines):
        raise SealError(
            f"history digest missing the {DIGEST_GLOBAL_HEADING!r} section")
    missing = [r for r in sorted(history_rounds)
               if not any(_round_heading(r).match(l) for l in lines)]
    if missing:
        raise SealError(
            "history digest is missing round blocks for closed round(s) "
            f"{missing} — every closed round needs its own "
            f"'{DIGEST_ROUND_HEADING_PREFIX}r<round>' block")


def stamp_digest(art: Path, max_bytes: int = DIGEST_MAX_BYTES) -> dict[str, Any]:
    """Seal the digest the curator just wrote (Step 4, both ending paths)."""
    digest = digest_path(art)
    data = _digest_bytes(digest)
    if len(data) > max_bytes:
        raise SealError(
            f"history digest is {len(data)} bytes, over the {max_bytes} "
            "cap — compress the older rounds to one-line lessons and "
            "re-dispatch the curator")

    rows = history_lib.read_rows(art / "history.jsonl")
    history_rounds = {r["round"] for r in rows
                      if isinstance(r.get("round"), int) and r["round"] >= 1}
    text = data.decode("utf-8")
    _check_curator_contract(text, history_rounds)

    stamped_round = round_state.current_round(art)
    if stamped_round < 1:
        raise SealError(
            "digest stamp outside a round (no rounds/<NNN>/ directory) — "
            "the curator runs in Step 4 of a live round, never before one")
    seal = {
        "covered_round": stamped_round,
        "history_lines": len(rows),
        "history_last_ts": rows[-1].get("ts") if rows else None,
        "digest_sha256": hashlib.sha256(data).hexdigest(),
        "digest_bytes": len(data),
    }
    dest = stamp_path(art)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(json.dumps(seal, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(dest)
    return seal


def _load_seal(art: Path) -> dict[str, Any]:
    path = stamp_path(art)
    try:
        seal = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SealError(f"history digest seal missing: {path}") from exc
    except ValueError as exc:  # unparseable JSON or torn UTF-8
        raise SealError(f"history digest seal unparseable: {path} ({exc})") from exc
    if not isinstance(seal, dict) or not isinstance(
            seal.get("covered_round"), int) or not isinstance(
            seal.get("digest_sha256"), str):
        raise SealError(
            f"history digest seal is malformed (needs covered_round and "
            f"digest_sha256): {path}")
    return seal


def _verify_digest_matches_seal(art: Path, seal: dict[str, Any]) -> None:
    data = _digest_bytes(digest_path(art))
    if (len(data) != seal.get("digest_bytes")
            or hashlib.sha256(data).hexdigest() != seal["digest_sha256"]):
        raise SealError(
            "history digest differs from its seal (edited or rewritten "
            "after stamping) — re-dispatch the curator and re-stamp")


def _resumable(art: Path, round_no: int) -> bool:
    """A round directory a (re-)entered propose node may legitimately work
    in: a parseable proposals.json whose round matches the directory."""
    path = art / f"rounds/{round_no:03d}" / "proposals.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):  # absent, torn JSON, torn UTF-8
        return False
    return isinstance(doc, dict) and doc.get("round") == round_no


def needed_coverage(art: Path) -> tuple[int, bool]:
    """(needed, required) — the seal coverage the CURRENT disk state demands
    (see module docstring). Pure read; never raises for a round-1 workspace."""
    rows = history_lib.read_rows(art / "history.jsonl")
    rounds = sorted({r["round"] for r in rows
                     if isinstance(r.get("round"), int) and r["round"] >= 1})
    if not rounds:
        return 0, False
    top = rounds[-1]
    needed = top if _resumable(art, top) else top - 1
    return needed, needed >= 1


def check_seal(art: Path) -> dict[str, Any]:
    """Step 0 freshness gate: fail loud on a missing, tampered, or stale
    seal; legal no-op (required=false) before the first closed round."""
    needed, required = needed_coverage(art)
    if not required:
        return {"ok": True, "required": False}
    seal = _load_seal(art)
    _verify_digest_matches_seal(art, seal)
    if seal["covered_round"] < needed:
        raise SealError(
            "history digest is stale: its seal covers through round "
            f"{seal['covered_round']} but closed rounds reach {needed} — "
            "the curator did not run after that round closed")
    return {"ok": True, "required": True,
            "covered_round": seal["covered_round"], "needed": needed}


def problems_for_emit(art: Path, round_no: int) -> list[str]:
    """The emit-gate variant: THIS round's digest must exist and be sealed —
    the stamp the next round's Step 0 will rely on is part of the round's
    disk contract, on BOTH ending paths."""
    problems: list[str] = []
    try:
        seal = _load_seal(art)
        _verify_digest_matches_seal(art, seal)
        if seal["covered_round"] < round_no:
            problems.append(
                f"history digest seal covers through round "
                f"{seal['covered_round']}, not the closing round {round_no} "
                "(re-dispatch the curator and re-stamp before emitting)")
    except SealError as exc:
        problems.append(str(exc))
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("command", choices=["stamp", "check"])
    ap.add_argument("--max-bytes", type=int, default=DIGEST_MAX_BYTES,
                    help="stamp only: the digest size cap "
                         f"(default {DIGEST_MAX_BYTES})")
    ns = ap.parse_args()
    art = Path(ns.artifacts)
    try:
        result = (stamp_digest(art, ns.max_bytes) if ns.command == "stamp"
                  else check_seal(art))
    except (SealError, history_lib.HistoryError, ValueError, OSError) as exc:
        print(f"digest_stamp: FAIL {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
