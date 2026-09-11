#!/usr/bin/env python3
"""render_history.py — the mechanically derived base/history.md view (v2 §2.2).

Replaces the v8 LLM narrative layer (history-curator / digest_stamp are
retired): base/history.md is DERIVED, never hand-written. The render reads
exactly three on-disk sources — history.jsonl (history_lib.read_rows), the
in-flight vids' variants/<vid>/train_status.json, and the rounds/ numeric
directories (round_state's predicate) — zero training-side reads, zero LLM.

Round blocks (round set = 1..current):
  - a round with NO history row renders only a pointer
    ``### r<N> · 无提案 · 详见 rounds/<RRR>/analysis.md`` (the rationale lives
    in the analysis document and is mechanically never parsed here);
  - per vid (same-round vids ordered by seq — the multi-vid case only arises
    on anomalous replays and is rendered as-is):
    ``### r<N> · <vid> · <outcome> · 时延 <Δ%> · acc <final_acc>（gap <g>）``
    followed by ONE facet line ``结构: …；特征: …；loss: …``.
    ``latency_improved`` with no terminal row in any version renders the
    in-flight form ``in_flight · stage <s> · epoch <e> · gap <g>（<risk>）``
    — the in-flight row predicate AND the risk grading (pending_launch
    included, unparseable train_status fail loud) are IMPORTED from
    frontier_snapshot (single implementation, never re-derived here);
  - 时延 Δ% = (variant_makespan − baseline) / baseline, signed percentage;
    sources: the row's ``makespan_cycles``, else a latency_fail row's
    ``measured_makespan_cycles``; denominator =
    origin_anchor.baseline_makespan_cycles; no makespan at all → ``时延 -``;
  - facet phrases come from the vid's impl row (carried forward by the
    merged-snapshot rows); a row without all three phrases fails loud.

The file tail carries ``render_stamp: {"lines", "last_line_no"}`` (0/0 for an
empty history — a legal round-1 state) so the emit gate can verify freshness.

Fail loud (exit 2): unparseable history.jsonl; a missing/unparseable origin
anchor (or a non-integer baseline_makespan_cycles); an unparseable
train_status.json of an in-flight vid; an impl row missing a facet phrase; a
history row whose round has no rounds/<NNN> directory (torn workspace).

Usage:
    render_history.py --artifacts <ws>
Effect: atomically writes base/history.md (tmp + os.replace) and prints the
same content on stdout; failures exit 2.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frontier_snapshot  # noqa: E402 — the ONE in-flight predicate source
import history_lib  # noqa: E402
import round_state  # noqa: E402

_HEADER = """# profiling-v2 history（机械派生视图）

> 本文件由 `render_history.py` 从 `history.jsonl`、在飞变体的
> `train_status.json` 与 `rounds/` 目录机械渲染，每轮至多两行；
> 唯一真相是 `history.jsonl`——不要手写或续写本文件。
"""
_FACET_FIELDS = ("structure_change", "feature_change", "loss_change")


def history_stamp(path: Path) -> dict[str, int]:
    """The freshness key: ``{"lines", "last_line_no"}`` over the jsonl's
    non-blank lines (the append-only writer emits one row per line, so this
    equals the row count; a missing/empty file is the 0/0 zero-row state).
    The emit gate recomputes THIS function — never its own copy."""
    if not path.is_file():
        return {"lines": 0, "last_line_no": 0}
    count = 0
    last = 0
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            count += 1
            last = no
    return {"lines": count, "last_line_no": last}


def parse_render_stamp(history_md: Path) -> dict[str, int] | None:
    """Read the render_stamp tail line back (None when the file or the stamp
    is missing/unparseable — the caller treats that as stale, never as
    fresh)."""
    if not history_md.is_file():
        return None
    try:
        lines = history_md.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if line.startswith("render_stamp:"):
            try:
                stamp = json.loads(line[len("render_stamp:"):].strip())
            except json.JSONDecodeError:
                return None
            if (isinstance(stamp, dict)
                    and isinstance(stamp.get("lines"), int)
                    and isinstance(stamp.get("last_line_no"), int)):
                return {"lines": stamp["lines"],
                        "last_line_no": stamp["last_line_no"]}
            return None
        if line.strip():
            break               # the stamp is the LAST non-empty line
    return None


def _show(value: Any) -> str:
    return "-" if value is None else str(value)


def _makespan_of(row: dict) -> int | None:
    """终态/实测行 makespan_cycles，退而 latency_fail 行的
    measured_makespan_cycles；两者皆无 → None（渲染 `时延 -`）。"""
    for key in ("makespan_cycles", "measured_makespan_cycles"):
        v = row.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    return None


def _latency_text(row: dict, baseline: int) -> str:
    ms = _makespan_of(row)
    if ms is None:
        return "时延 -"
    if baseline <= 0:
        raise ValueError(
            "origin anchor baseline_makespan_cycles must be > 0 to render "
            f"the signed 时延 percentage (got {baseline})")
    return f"时延 {(ms - baseline) / baseline * 100:+.1f}%"


def _num(value: Any) -> str:
    return "-" if value is None else f"{value:g}"


def render(art: Path) -> str:
    """Pure derivation → the full history.md text (no writes; main() owns
    the atomic file swap)."""
    _, baseline = history_lib.expected_base(art)   # anchor fail loud here
    current = round_state.current_round(art)
    rows = history_lib.read_rows(art / "history.jsonl")

    latest: dict[str, dict] = {}
    passed: set[str] = set()       # ANY version row reached latency_improved
    terminal: set[str] = set()     # ANY version row carries a terminal outcome
    round_vids: dict[int, set[str]] = {}
    for row in rows:
        vid = row.get("vid")
        if not isinstance(vid, str) or not vid:
            raise ValueError(f"history row without a vid string: {row!r}")
        rnd = row.get("round")
        if (isinstance(rnd, bool) or not isinstance(rnd, int)
                or not 1 <= rnd <= current):
            raise ValueError(
                f"history row of {vid} carries round {rnd!r} outside the "
                f"rounds/ directory range 1..{current} — torn workspace")
        latest[vid] = row
        round_vids.setdefault(rnd, set()).add(vid)
        if row.get("outcome") == "latency_improved":
            passed.add(vid)
        if row.get("outcome") in history_lib.TERMINAL_OUTCOMES:
            terminal.add(vid)

    lines = [_HEADER.rstrip("\n"), ""]
    for rnd in range(1, current + 1):
        vids = sorted(round_vids.get(rnd, ()),
                      key=lambda v: latest[v].get("seq") or 0)
        if not vids:
            lines.append(f"### r{rnd} · 无提案 · 详见 rounds/{rnd:03d}/analysis.md")
            lines.append("")
            continue
        for vid in vids:
            row = latest[vid]
            for field in _FACET_FIELDS:
                if not isinstance(row.get(field), str):
                    raise ValueError(
                        f"impl row of {vid} lacks the facet phrase "
                        f"{field!r} — v2 rows carry all three (torn or "
                        "legacy-schema workspace)")
            latency = _latency_text(row, baseline)
            if vid in passed and vid not in terminal:
                # in-flight: the predicate/risk (pending_launch included,
                # unparseable train_status fail loud) is frontier_snapshot's
                flight = frontier_snapshot._in_flight_row(art, vid)
                lines.append(
                    f"### r{rnd} · {vid} · in_flight"
                    f" · stage {_show(flight.get('stage'))}"
                    f" · epoch {_show(flight.get('epoch'))}"
                    f" · gap {_show(flight.get('gap'))}"
                    f"（{flight.get('risk')}） · {latency}")
            else:
                outcome = row.get("outcome") or "-"
                lines.append(
                    f"### r{rnd} · {vid} · {outcome} · {latency}"
                    f" · acc {_num(row.get('final_acc'))}"
                    f"（gap {_num(row.get('gap'))}）")
            lines.append(f"结构: {row['structure_change']}；"
                         f"特征: {row['feature_change']}；"
                         f"loss: {row['loss_change']}")
            lines.append("")

    stamp = history_stamp(art / "history.jsonl")
    lines.append("render_stamp: "
                 + json.dumps(stamp, ensure_ascii=False, sort_keys=True))
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", default=os.environ.get("ORCA_ARTIFACTS_DIR"))
    ns = ap.parse_args()
    if not ns.artifacts:
        print("render_history: --artifacts or ORCA_ARTIFACTS_DIR is required",
              file=sys.stderr)
        return 2
    art = Path(ns.artifacts)
    try:
        text = render(art)
        out = art / "base" / "history.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + f".tmp.{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, out)     # atomic: history.md is whole or old, never torn
    except (OSError, ValueError, KeyError, history_lib.HistoryError) as exc:
        print(f"render_history: FAIL {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
