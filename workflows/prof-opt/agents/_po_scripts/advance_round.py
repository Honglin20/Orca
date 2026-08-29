#!/usr/bin/env python3
"""advance_round.py — deterministic, replayable end-of-round advance.

Recomputes the round outcome from history.jsonl (never from node outputs) and
applies the fixed-order atomic step sequence. The phase (latency chase vs
accuracy recovery) comes from round_state.py mode — the frozen origin anchor
decides it, never a caller argument.

  latency mode  candidates = this round's latest `latency_pass` rows with
                makespan STRICTLY below the incumbent (best.json makespan,
                else the anchor's baseline makespan). Small strictly-better
                steps are legitimate advances. Winner = min makespan,
                tie -> vid order (no proxy tie in this mode).
  accuracy mode candidates = this round's latest `accuracy_pass` rows with
                makespan <= target_cycles. Winner = min gap, tie -> min
                makespan, tie -> vid order (gap is direction-normalized by
                the verdict script at write time).

Common actions run ONLY on a real advance (winner != incumbent, or a torn
write being repaired): best.json (tmp + os.replace) -> copy winner
onnx/profile/shadow into base/ and shadow/ (bottleneck_report.json
deliberately NOT copied) -> append_advanced(winner) -> marker LAST (the
commit point). No candidate / winner == incumbent -> marker only
(vid=null, improved=false). Every advance (no-op included) writes
rounds/<RRR>/direction.json (failed_sigs = this round's latency_fail AND
accuracy_fail change signatures — the next round's rerouting signal).

Idempotency key = (round, mode): a marker matching both is a no-op; a stale
marker (older round) replays under the CURRENT mode and converges.

Torn-write recovery (crash between best.json write and the marker): when
best.json already names this round's winner (best.round == current) and that
vid has NO `advanced` row of this round, the sequence is torn in flight —
(A) the recomputed winner equals best.vid (accuracy-mode tears always land
here: candidates include the winner itself), or (B) latency mode found no
candidate (the tear already made best.vid the incumbent, so the strict
improvement test can never re-admit it). Both repair by best.vid: complete
the copy + append_advanced, then write the marker. The benign first-entry
(the same-round latency-advanced best.vid passing the accuracy gate) has an
advanced row already, matches neither criterion, and takes the marker-only
path. Known disclosed residual: a latency tear whose winner already meets
the line, followed by a failing accuracy first-entry, closes with a no-op
marker — best/base diverge for at most one round; the gate never opens
full-train on it.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from history_lib import append_advanced, read_latest, read_rows  # noqa: E402
from round_state import current_round, mode_state  # noqa: E402

MARKER_NAME = ".round_advanced"
FAILED_OUTCOMES = frozenset({"latency_fail", "accuracy_fail"})


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _copy_tree_replace(src: Path, dst: Path) -> None:
    """Replace dst with src, excluding __pycache__ dirs and *.pyc files."""
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)

    def _excluded(rel: Path) -> bool:
        return "__pycache__" in rel.parts or rel.suffix == ".pyc"

    for item in sorted(src.rglob("*")):
        rel = item.relative_to(src)
        if _excluded(rel):
            continue
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _copy_winner(artifacts: Path, vid: str) -> None:
    variant_dir = artifacts / "variants" / vid
    onnx_src = variant_dir / "onnx" / "model.onnx"
    profile_src = variant_dir / "profile"
    shadow_src = variant_dir / "shadow"
    for required in (onnx_src, profile_src, shadow_src):
        if not required.exists():
            raise FileNotFoundError(
                f"advance_round: winner {vid} is missing {required}")
    base_dir = artifacts / "base"
    base_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(onnx_src, base_dir / "model.onnx")
    _copy_tree_replace(profile_src, base_dir / "profile")
    _copy_tree_replace(shadow_src, artifacts / "shadow")


def _load_origin_anchor(artifacts: Path) -> dict:
    path = artifacts / "base" / "origin_anchor.json"
    try:
        anchor = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"advance_round: origin anchor missing: {path} (the anchor is "
            f"frozen by the baseline stage and never moves)") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"advance_round: origin anchor unparseable: {exc}") from exc
    for key in ("baseline_makespan_cycles", "target_cycles"):
        if not isinstance(anchor.get(key), int):
            raise ValueError(
                f"advance_round: origin anchor lacks integer {key!r}: {anchor!r}")
    return anchor


def _candidates(latest: dict[str, dict], round_no: int, mode: str,
                incumbent_makespan: int, target: int) -> list[dict]:
    rows = [row for row in latest.values() if row.get("round") == round_no]
    if mode == "latency":
        cands = [row for row in rows
                 if row.get("outcome") == "latency_pass"
                 and "makespan_cycles" in row
                 and row["makespan_cycles"] < incumbent_makespan]
        return sorted(cands, key=lambda r: (r["makespan_cycles"], r["vid"]))
    # accuracy: rank on the verdict-normalized gap (direction folded in at
    # the verdict layer — never a raw proxy_acc sign convention here)
    cands = []
    for row in rows:
        if row.get("outcome") != "accuracy_pass" or "makespan_cycles" not in row:
            continue
        if row["makespan_cycles"] > target:
            continue
        if "gap" not in row:
            raise ValueError(
                f"advance_round: accuracy_pass row of {row.get('vid')!r} has "
                f"no gap — the probe verdict must record it")
        cands.append(row)
    return sorted(cands, key=lambda r: (r["gap"], r["makespan_cycles"], r["vid"]))


def _failed_sigs(latest: dict[str, dict], round_no: int) -> list[str]:
    sigs = {row.get("change_sig") for row in latest.values()
            if row.get("round") == round_no
            and row.get("outcome") in FAILED_OUTCOMES
            and row.get("change_sig")}
    return sorted(sigs)


def _advanced_this_round(history_path: Path, vid: str, round_no: int) -> bool:
    return any(row.get("vid") == vid and row.get("round") == round_no
               and row.get("outcome") == "advanced"
               for row in read_rows(history_path))


def advance(artifacts: Path) -> dict:
    round_no = current_round(artifacts)       # single source (round_state.py)
    if round_no == 0:
        raise FileNotFoundError("advance_round: no rounds/<NNN>/ directory exists yet")

    anchor = _load_origin_anchor(artifacts)   # the incumbent when no best yet
    mode_info = mode_state(artifacts)         # single source (round_state.py)
    mode = mode_info["mode"]
    target = mode_info["target_cycles"]

    marker_path = artifacts / MARKER_NAME
    marker: dict | None = None
    if marker_path.is_file():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"advance_round: .round_advanced unparseable: {exc}") from exc
        if marker.get("round") == round_no and marker.get("mode") == mode:
            return {"advanced": False, "round": round_no, "mode": mode,
                    "vid": marker.get("vid"),
                    "improved": marker.get("improved", False),
                    "best_updated": marker.get("best_updated", False),
                    "reason": "marker (round, mode) matches; already advanced"}

    history_path = artifacts / "history.jsonl"
    latest = read_latest(history_path)
    best: dict | None = None
    best_path = artifacts / "best.json"
    if best_path.is_file():
        try:
            best = json.loads(best_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"advance_round: best.json unparseable: {exc}") from exc

    incumbent_makespan = (int(best["makespan_cycles"]) if best is not None
                          else anchor["baseline_makespan_cycles"])

    cands = _candidates(latest, round_no, mode, incumbent_makespan, target)
    winner = cands[0] if cands else None

    # torn-write detection (see module docstring): best.json already names
    # this round's winner while the sequence never completed
    torn = False
    if (best is not None and best.get("round") == round_no
            and not _advanced_this_round(history_path, best.get("vid"), round_no)):
        if winner is not None and winner["vid"] == best["vid"]:
            torn = True          # (A) winner recomputation hits the torn write
        elif mode == "latency" and winner is None:
            torn = True          # (B) tear suppressed every strict candidate

    real_advance = torn or (
        winner is not None and (best is None or winner["vid"] != best["vid"]))

    round_rel = f"{round_no:03d}"
    if real_advance:
        row = winner if winner is not None else latest[best["vid"]]
        vid = row["vid"]
        _atomic_write_json(best_path, {
            "vid": vid,
            "makespan_cycles": row["makespan_cycles"],
            "proxy_acc": None if mode == "latency" else row.get("proxy_acc"),
            "round": round_no,
            "profile_dir": str(artifacts / "variants" / vid / "profile"),
        })
        _copy_winner(artifacts, vid)
        append_advanced(history_path, vid)
        _atomic_write_json(artifacts / "rounds" / round_rel / "direction.json", {
            "round": round_no, "mode": mode, "improved": True,
            "advanced_vid": vid, "failed_sigs": _failed_sigs(latest, round_no),
        })
        marker = {"round": round_no, "mode": mode, "vid": vid,
                  "improved": True, "best_updated": True}
        _atomic_write_json(marker_path, marker)
        return {"advanced": True, **marker,
                "reason": "torn write repaired" if torn else "winner advanced"}

    _atomic_write_json(artifacts / "rounds" / round_rel / "direction.json", {
        "round": round_no, "mode": mode, "improved": False,
        "advanced_vid": None, "failed_sigs": _failed_sigs(latest, round_no),
    })
    marker = {"round": round_no, "mode": mode, "vid": None,
              "improved": False, "best_updated": False}
    _atomic_write_json(marker_path, marker)
    return {"advanced": False, **marker,
            "reason": "no candidate or winner == incumbent; marker only"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", required=True)
    ns = ap.parse_args()
    try:
        result = advance(Path(ns.artifacts))
    except (OSError, ValueError, KeyError) as exc:
        print(f"advance_round: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
