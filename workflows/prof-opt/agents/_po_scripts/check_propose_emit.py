#!/usr/bin/env python3
"""Pre-return gate for po_propose.

Verifies the proposal loop closed its disk state before the node emits:
proposals.json shape (mode-conditioned `target_pattern_id`: a
`bottleneck_analysis.json` name in placeholder mode, a non-empty free-form
label in mfu mode), per-vid history rows, verdict/direction/analysis files,
and the latency-phase advance marker. This is structural completeness only;
proposal quality and verdicts are not re-judged here.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_json(path: Path, what: str) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise ValueError(f"{what} unparseable: {path} ({exc})") from exc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", required=True)
    ns = ap.parse_args()
    art = Path(ns.artifacts)
    problems: list[str] = []

    scripts_dir = art / "scripts"
    if not scripts_dir.is_dir():
        print("check_propose_emit: FAIL scripts/ missing", file=sys.stderr)
        return 1
    sys.path.insert(0, str(scripts_dir))
    try:
        import history_lib
        import round_state
    except Exception as exc:
        print(f"check_propose_emit: FAIL cannot import shared scripts: {exc}",
              file=sys.stderr)
        return 1

    try:
        r = round_state.current_round(art)
        mode = round_state.mode_state(art)["mode"]
    except Exception as exc:
        print(f"check_propose_emit: FAIL round/mode unavailable: {exc}",
              file=sys.stderr)
        return 1

    profile_mode = "placeholder"
    try:
        pm = _load_json(art / "profile_mode.json", "profile_mode.json")
        if pm is None:
            problems.append("profile_mode.json missing (entry stage incomplete)")
        elif not isinstance(pm, dict) or pm.get("mode") not in ("placeholder", "mfu"):
            problems.append("profile_mode.json mode must be placeholder|mfu")
        else:
            profile_mode = pm["mode"]
    except ValueError as exc:
        problems.append(str(exc))

    rd = art / f"rounds/{r:03d}"
    try:
        proposals = _load_json(rd / "proposals.json", "proposals.json")
        if proposals is None:
            problems.append("rounds/<R>/proposals.json missing")
        elif not isinstance(proposals, dict):
            problems.append("proposals.json is not a JSON object")
        else:
            if proposals.get("round") != r:
                problems.append(f"proposals.json round != {r}")
            prop_list = proposals.get("proposals")
            if not isinstance(prop_list, list) or len(prop_list) > 3:
                problems.append("proposals must be a list of at most 3")
            elif prop_list:
                latest = history_lib.read_latest(art / "history.jsonl")
                names: set[str] | None = None
                if profile_mode == "placeholder":
                    analysis = _load_json(
                        art / "base" / "bottleneck_analysis.json",
                        "bottleneck_analysis.json")
                    if analysis is None:
                        problems.append(
                            "base/bottleneck_analysis.json missing "
                            "(placeholder mode)")
                    elif not isinstance(analysis, dict):
                        problems.append(
                            "base/bottleneck_analysis.json must be a JSON "
                            "object")
                    else:
                        entries = analysis.get("top_bottlenecks")
                        names = ({e.get("name") for e in entries
                                  if isinstance(e, dict)}
                                 if isinstance(entries, list) else set())
                        if not names:
                            problems.append(
                                "base/bottleneck_analysis.json "
                                "top_bottlenecks has no names")
                for prop in prop_list:
                    vid = prop.get("vid")
                    if not isinstance(vid, str) or not vid:
                        problems.append("proposal missing vid")
                        continue
                    for key in ("change_sig", "predicted_delta_cycles",
                                "edited_files", "target_pattern_id",
                                "predicted_acc_impact", "sota_reference"):
                        if key not in prop:
                            problems.append(f"{vid} proposal missing {key}")
                    if (not isinstance(prop.get("predicted_delta_cycles"), int)
                            or prop["predicted_delta_cycles"] >= 0):
                        problems.append(f"{vid} predicted_delta_cycles must be int < 0")
                    if not prop.get("edited_files"):
                        problems.append(f"{vid} edited_files must be non-empty")
                    tpid = prop.get("target_pattern_id")
                    if profile_mode == "mfu":
                        if not isinstance(tpid, str) or not tpid.strip():
                            problems.append(
                                f"{vid} target_pattern_id must be non-empty "
                                "(mfu mode)")
                    elif names is not None and tpid not in names:
                        problems.append(
                            f"{vid} target_pattern_id {tpid!r} is not a name "
                            "in base/bottleneck_analysis.json")
                    row = latest.get(vid)
                    if not row:
                        problems.append(f"{vid} has no history row")
                    elif row.get("round") != r or row.get("change_sig") != prop.get("change_sig"):
                        problems.append(f"{vid} history row does not match proposal")
            else:
                rationale = proposals.get("exhausted_rationale")
                if not isinstance(rationale, list) or not rationale:
                    problems.append("zero-proposal round must carry non-empty exhausted_rationale")
    except ValueError as exc:
        problems.append(str(exc))

    analysis_path = rd / "analysis.md"
    if not analysis_path.is_file() or analysis_path.stat().st_size == 0:
        problems.append("rounds/<R>/analysis.md missing or empty")
    verdicts = rd / "verdicts.jsonl"
    if not verdicts.is_file():
        problems.append("rounds/<R>/verdicts.jsonl missing")
    else:
        try:
            for line_no, line in enumerate(
                    verdicts.read_text(encoding="utf-8").splitlines(), 1):
                if line.strip() and not isinstance(json.loads(line), dict):
                    problems.append(f"verdicts.jsonl:{line_no} is not a JSON object")
        except json.JSONDecodeError as exc:
            problems.append(f"verdicts.jsonl unparseable: {exc}")

    if mode == "latency":
        marker = art / ".round_advanced"
        try:
            m = _load_json(marker, ".round_advanced")
        except ValueError as exc:
            problems.append(str(exc))
        else:
            if m is None:
                problems.append(".round_advanced missing in latency phase")
            elif m.get("round") != r or m.get("mode") != "latency":
                problems.append(".round_advanced does not record (round, latency)")
        direction = rd / "direction.json"
        try:
            d = _load_json(direction, "direction.json")
        except ValueError as exc:
            problems.append(str(exc))
        else:
            if d is None:
                problems.append("direction.json missing in latency phase")

    if problems:
        for p in problems:
            print(f"check_propose_emit: FAIL {p}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "round": r, "mode": mode}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
