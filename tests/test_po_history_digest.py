"""test_po_history_digest.py — digest_stamp.py seal contract tests.

The history digest is the proposal narrative layer's derived cache; these
tests pin the mechanical boundary that keeps it honest:
  - stamp: curator contract (sentinel / Global lessons / round blocks),
    byte cap, seal written from disk-derived state (never hand-passed)
  - check: required-vs-legal-no-op round semantics (round 1 / closed
    rounds / torn mid-round resume), staleness, tamper, missing pieces
  - problems_for_emit: the closing round must carry its own seal
  - the CLI argv contract the agent.md instructions rely on
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workflows" / "prof-opt" / "agents" / "_po_scripts"
sys.path.insert(0, str(_SCRIPTS))

import digest_stamp  # noqa: E402
import history_lib  # noqa: E402

_DIGEST_CLI = _SCRIPTS / "digest_stamp.py"


def _run_cli(args, timeout=60):
    return subprocess.run([sys.executable, str(_DIGEST_CLI)] + args,
                          capture_output=True, text=True, timeout=timeout)


def _ws(tmp_path: Path) -> Path:
    art = tmp_path / "ws"
    art.mkdir(parents=True)
    return art


def _impl_row(art: Path, round_no: int, vid: str) -> None:
    history_lib.append_implemented(
        art / "history.jsonl", vid, round=round_no, seq=1,
        change_sig=f"sig:{vid}", probe_epochs=1, target_modules=["m"],
        absorbs=[])


def _round(art: Path, round_no: int, *, resumable: bool | None = True) -> None:
    """Create rounds/<NNN>/; resumable=None leaves no directory at all,
    resumable=False leaves a torn (unparseable) proposals.json."""
    if resumable is None:
        return
    rd = art / "rounds" / f"{round_no:03d}"
    rd.mkdir(parents=True)
    if resumable:
        (rd / "proposals.json").write_text(
            json.dumps({"round": round_no, "proposals": []}),
            encoding="utf-8")
    else:
        (rd / "proposals.json").write_text("{torn", encoding="utf-8")


def _digest_text(*, sentinel: str = digest_stamp.CURATOR_SENTINEL,
                 global_heading: str = digest_stamp.DIGEST_GLOBAL_HEADING,
                 round_block: bool = True) -> str:
    lines = [sentinel, "# History Digest", "", global_heading,
             "what keeps failing and why", ""]
    if round_block:
        lines += [f"{digest_stamp.DIGEST_ROUND_HEADING_PREFIX}r1 — theme",
                  "the round's causal story", ""]
    lines += ["## Accuracy rules", "snapshot absent", ""]
    return "\n".join(lines)


def _write_digest(art: Path, text: str) -> None:
    (art / "base").mkdir(exist_ok=True)
    (art / "base" / "history_digest.md").write_text(text, encoding="utf-8")


def test_round1_workspace_needs_no_seal(tmp_path):
    """Before the first closed round the seal is a legal no-op — no digest,
    no stamp, no failure."""
    art = _ws(tmp_path)
    assert digest_stamp.needed_coverage(art) == (0, False)
    assert digest_stamp.check_seal(art) == {"ok": True, "required": False}


def test_stamp_and_check_cli_green_round(tmp_path):
    """The argv contract agent.md relies on: stamp in Step 4, check in the
    next round's Step 0 — both exit 0 with single-line JSON."""
    art = _ws(tmp_path)
    _round(art, 1)
    _impl_row(art, 1, "r1-01")
    _write_digest(art, _digest_text())

    proc = _run_cli(["--artifacts", str(art), "stamp"])
    assert proc.returncode == 0, proc.stderr
    seal = json.loads(proc.stdout)
    assert seal["covered_round"] == 1
    assert seal["digest_bytes"] > 0
    assert (art / "base" / "history_digest.stamp.json").is_file()

    proc = _run_cli(["--artifacts", str(art), "check"])
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "ok": True, "required": True, "covered_round": 1, "needed": 1}


def test_check_ok_on_torn_midround_resume(tmp_path):
    """A torn (stall-killed) latest round does not demand coverage it never
    earned: the previous round's seal stays the honest state and a re-entered
    node may resume."""
    art = _ws(tmp_path)
    _round(art, 1)
    _impl_row(art, 1, "r1-01")
    _write_digest(art, _digest_text())
    assert _run_cli(["--artifacts", str(art), "stamp"]).returncode == 0

    _round(art, 2, resumable=False)          # torn mid-round workspace
    _impl_row(art, 2, "r2-01")               # it did start writing history
    result = digest_stamp.check_seal(art)
    assert result == {"ok": True, "required": True,
                      "covered_round": 1, "needed": 1}


def test_check_fails_when_closed_round_not_covered(tmp_path):
    """A resumable latest round whose narrative never got curated (the
    curator skipped its closing emit) leaves a stale seal — Step 0 fails
    loud even though the emit gate should already have caught it."""
    art = _ws(tmp_path)
    _round(art, 1)
    _impl_row(art, 1, "r1-01")
    _write_digest(art, _digest_text())
    assert _run_cli(["--artifacts", str(art), "stamp"]).returncode == 0

    _round(art, 2)                           # closed, resumable
    _impl_row(art, 2, "r2-01")
    with pytest.raises(digest_stamp.SealError, match="stale"):
        digest_stamp.check_seal(art)


def test_check_fails_on_missing_seal_or_digest(tmp_path):
    art = _ws(tmp_path)
    _round(art, 1)
    _impl_row(art, 1, "r1-01")
    _write_digest(art, _digest_text())
    with pytest.raises(digest_stamp.SealError, match="seal missing"):
        digest_stamp.check_seal(art)

    assert _run_cli(["--artifacts", str(art), "stamp"]).returncode == 0
    (art / "base" / "history_digest.md").unlink()
    with pytest.raises(digest_stamp.SealError, match="digest missing"):
        digest_stamp.check_seal(art)


def test_check_fails_on_tampered_digest(tmp_path):
    """Any post-stamp edit — including a well-meant one — breaks the byte
    and hash agreement: re-curate and re-stamp, never hand-patch."""
    art = _ws(tmp_path)
    _round(art, 1)
    _impl_row(art, 1, "r1-01")
    _write_digest(art, _digest_text())
    assert _run_cli(["--artifacts", str(art), "stamp"]).returncode == 0

    _write_digest(art, _digest_text() + "one more lesson\n")
    with pytest.raises(digest_stamp.SealError, match="differs from its seal"):
        digest_stamp.check_seal(art)


@pytest.mark.parametrize("bad", ["sentinel", "global", "round"])
def test_stamp_enforces_curator_contract(tmp_path, bad):
    """The stamp refuses a digest that skips the curator contract: sentinel
    first line, the Global lessons heading, and a round block whenever
    history has reached a round."""
    art = _ws(tmp_path)
    _round(art, 1)
    _impl_row(art, 1, "r1-01")
    text = _digest_text(
        sentinel="[subagent:history-curator v1 FORGED0]" if bad == "sentinel"
        else digest_stamp.CURATOR_SENTINEL,
        global_heading="## Lessons" if bad == "global"
        else digest_stamp.DIGEST_GLOBAL_HEADING,
        round_block=(bad != "round"))
    _write_digest(art, text)
    with pytest.raises(digest_stamp.SealError):
        digest_stamp.stamp_digest(art)
    assert not (art / "base" / "history_digest.stamp.json").exists()


def test_stamp_requires_every_closed_round_block(tmp_path):
    """A digest keeping only the newest round's block has silently dropped
    the cross-round narrative: every closed round in history needs its own
    `## Round r<R>` block before the seal is written."""
    art = _ws(tmp_path)
    _round(art, 1)
    _impl_row(art, 1, "r1-01")
    _round(art, 2)
    _impl_row(art, 2, "r2-01")
    _write_digest(art, _digest_text())          # carries only the r1 block
    with pytest.raises(digest_stamp.SealError,
                       match=r"round blocks for closed round\(s\) \[2\]"):
        digest_stamp.stamp_digest(art)

    text = _digest_text().replace(
        f"{digest_stamp.DIGEST_ROUND_HEADING_PREFIX}r1 — theme",
        f"{digest_stamp.DIGEST_ROUND_HEADING_PREFIX}r1 — theme\n\n"
        f"{digest_stamp.DIGEST_ROUND_HEADING_PREFIX}r2 — newer theme")
    _write_digest(art, text)
    assert digest_stamp.stamp_digest(art)["covered_round"] == 2


def test_cli_check_fails_loud_exit_2_on_stale(tmp_path):
    """The CLI face the agent.md fail-loud instructions rely on: a stale
    seal exits 2 with the reason on stderr, never a silent pass."""
    art = _ws(tmp_path)
    _round(art, 1)
    _impl_row(art, 1, "r1-01")
    _write_digest(art, _digest_text())
    assert _run_cli(["--artifacts", str(art), "stamp"]).returncode == 0

    _round(art, 2)
    _impl_row(art, 2, "r2-01")
    proc = _run_cli(["--artifacts", str(art), "check"])
    assert proc.returncode == 2
    assert "stale" in proc.stderr


def test_stamp_without_round_block_legal_when_history_empty(tmp_path):
    """A zero-proposal FIRST round leaves no history rows: no round block is
    required, and the seal still carries the stamped round for the emit
    gate."""
    art = _ws(tmp_path)
    _round(art, 1)
    _write_digest(art, _digest_text(round_block=False))
    seal = digest_stamp.stamp_digest(art)
    assert seal["covered_round"] == 1
    assert seal["history_lines"] == 0
    assert digest_stamp.check_seal(art) == {"ok": True, "required": False}


def test_stamp_enforces_byte_cap(tmp_path):
    art = _ws(tmp_path)
    _round(art, 1)
    _impl_row(art, 1, "r1-01")
    _write_digest(art, _digest_text())
    with pytest.raises(digest_stamp.SealError, match="cap"):
        digest_stamp.stamp_digest(art, max_bytes=64)
    assert not (art / "base" / "history_digest.stamp.json").exists()


def test_stamp_outside_any_round_fails(tmp_path):
    """The curator runs in Step 4 of a live round; a seal with no rounds/
    directory would claim coverage of nothing — fail loud instead."""
    art = _ws(tmp_path)
    _write_digest(art, _digest_text())
    with pytest.raises(digest_stamp.SealError, match="outside a round"):
        digest_stamp.stamp_digest(art)


def test_problems_for_emit_demands_this_rounds_seal(tmp_path):
    """The emit gate variant: the closing round must carry its own seal —
    a previous round's fresh-enough seal is not enough here."""
    art = _ws(tmp_path)
    assert digest_stamp.problems_for_emit(art, 1)  # nothing sealed at all

    _round(art, 1)
    _impl_row(art, 1, "r1-01")
    _write_digest(art, _digest_text())
    assert digest_stamp.stamp_digest(art)["covered_round"] == 1
    assert digest_stamp.problems_for_emit(art, 1) == []

    problems = digest_stamp.problems_for_emit(art, 2)
    assert len(problems) == 1
    assert "covers through round 1" in problems[0]

    _write_digest(art, _digest_text(round_block=False))
    problems = digest_stamp.problems_for_emit(art, 1)
    assert len(problems) == 1
    assert "differs from its seal" in problems[0]
