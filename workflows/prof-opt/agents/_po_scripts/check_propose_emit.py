#!/usr/bin/env python3
"""Pre-return gate for po_propose (v6, SPEC §5.3).

Verifies the single-variant convergence loop closed its disk state before
the node emits:

  1. proposals.json parses with `round == R` and holds EXACTLY ONE
     proposal whose PREDICTED makespan (base makespan +
     predicted_delta_cycles) is <= the frozen target_cycles (inclusive
     boundary, same as the recheck gate). A zero-proposal round is legal
     only with a non-empty `exhausted_rationale`.
  2. The round's variant carries the three §4.1 analysis documents —
     business_logic.md / information_analysis.md (variant mode: sentinel
     first line, non-empty body, required section headings incl. the
     conclusion section) and conformance.md (present, non-empty, records
     both analyst sentinels).
  3. The vid has its expected history row; a latency-passing vid has a
     `latency_pass` row, an eliminated vid its terminal/elimination
     outcome, and a latency_fail elimination also lands in
     rounds/<R>/direction.json `failed_sigs`.
  4. variants/<vid>/repair_trace.json, when present, records
     repair_count == len(attempts) and repair_count <= 5 (§5.2 — the
     over-budget repair is intercepted here even if the recheck guard was
     bypassed).
  5. rounds/<R>/analysis.md exists, is non-empty, and carries the
     `## latency` section — required on BOTH ending paths (reached the
     line and latency_fail elimination; SPEC §5.1 Step6 "written every
     round").

This is structural completeness only; proposal quality, verdicts, and the
soft-alignment judgment ("does the variant still make sense vs the
baseline") are not re-judged here.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BL_SENTINEL = "[subagent:business-logic-analyst v1 BLA7K4]"
INFO_SENTINEL = "[subagent:information-analyst v1 IXA3N7]"
# variant-mode section contracts (§4.1): the last heading of each document is
# its conclusion section — the node's mechanical Step2 gate checks the same
# set, so the emit gate re-asserts it end-to-end.
BUSINESS_HEADINGS = ("## 任务语义", "## 输入输出", "## 架构动机",
                     "## 逐模块职责与物理意义", "## 训练目标与指标方向",
                     "## 与基线差异")
INFO_VARIANT_HEADINGS = ("## 信息核心", "## 近似与牺牲项",
                         "## 被牺牲信息与预期精度代价")
# §4.3 outcomes a round's single vid may legitimately end on at emit time
LEGAL_END_OUTCOMES = frozenset({
    "latency_pass", "latency_fail", "structural_mismatch", "variant_broken",
    "unsupported_op"})
REPAIR_MAX = 5


def _load_json(path: Path, what: str) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise ValueError(f"{what} unparseable: {path} ({exc})") from exc


def _check_doc(path: Path, label: str, sentinel: str,
               headings: tuple[str, ...], problems: list[str]) -> None:
    """Sentinel + non-empty body + required section headings (§4.1)."""
    if not path.is_file():
        problems.append(f"{label} missing ({path})")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != sentinel:
        problems.append(f"{label} first line is not the sentinel {sentinel!r}")
        return
    body = [l for l in lines[1:] if l.strip()]
    if not body:
        problems.append(f"{label} body is empty (sentinel only)")
    present = {l.strip() for l in lines}
    for heading in headings:
        if heading not in present:
            problems.append(f"{label} missing section heading {heading!r}")


def _check_repair_trace(path: Path, vid: str, problems: list[str]) -> None:
    if not path.is_file():
        return          # never failed a measurement: no ledger is legal
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        problems.append(f"{vid} repair_trace.json unparseable ({exc})")
        return
    if not isinstance(doc, dict):
        problems.append(f"{vid} repair_trace.json is not a JSON object")
        return
    count = doc.get("repair_count")
    attempts = doc.get("attempts")
    if not isinstance(count, int) or isinstance(count, bool):
        problems.append(f"{vid} repair_trace repair_count must be an int")
        return
    if not isinstance(attempts, list):
        problems.append(f"{vid} repair_trace attempts must be a list")
        return
    if count != len(attempts):
        problems.append(
            f"{vid} repair_trace repair_count={count} != len(attempts)="
            f"{len(attempts)} (the trace writer keeps them equal — a mismatch "
            "means a hand edit)")
    if count > REPAIR_MAX:
        problems.append(
            f"{vid} repair_trace repair_count={count} exceeds the repair "
            f"budget of {REPAIR_MAX} (§5.2) — the 6th repair must be "
            "intercepted, never emitted")


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
    except Exception as exc:
        print(f"check_propose_emit: FAIL round unavailable: {exc}",
              file=sys.stderr)
        return 1
    if r == 0:
        print("check_propose_emit: FAIL no rounds/<NNN>/ directory exists",
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

    # admission line: base makespan + frozen target (both single-sourced)
    try:
        summary = _load_json(art / "base" / "profile" / "profile_summary.json",
                             "base/profile/profile_summary.json")
        anchor = _load_json(art / "base" / "origin_anchor.json",
                            "base/origin_anchor.json")
    except ValueError as exc:
        summary = anchor = None
        problems.append(str(exc))
    base_ms = (summary.get("makespan_cycles")
               if isinstance(summary, dict) else None)
    target = (anchor.get("target_cycles")
              if isinstance(anchor, dict) else None)
    if not isinstance(base_ms, int) or not isinstance(target, int):
        problems.append("base makespan / target_cycles unavailable "
                        "(baseline stage incomplete)")

    rd = art / f"rounds/{r:03d}"
    proposal: dict = {}
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
            if not isinstance(prop_list, list):
                problems.append("proposals must be a list")
            elif len(prop_list) > 1:
                problems.append(
                    f"proposals holds {len(prop_list)} entries — exactly ONE "
                    "proposal per round (v6 §5.1)")
            elif prop_list:
                proposal = prop_list[0]
            elif not (isinstance(proposals.get("exhausted_rationale"), list)
                      and proposals["exhausted_rationale"]):
                problems.append("zero-proposal round must carry non-empty "
                                "exhausted_rationale")
    except ValueError as exc:
        problems.append(str(exc))

    latest: dict = {}
    try:
        latest = history_lib.read_latest(art / "history.jsonl")
    except Exception as exc:
        problems.append(f"history.jsonl unreadable: {exc}")

    if proposal:
        vid = proposal.get("vid")
        if not isinstance(vid, str) or not vid:
            problems.append("proposal missing vid")
        else:
            for key in ("change_sig", "predicted_delta_cycles", "edited_files",
                        "target_pattern_id", "predicted_acc_impact",
                        "sota_reference"):
                if key not in proposal:
                    problems.append(f"{vid} proposal missing {key}")
            delta = proposal.get("predicted_delta_cycles")
            if not isinstance(delta, int) or isinstance(delta, bool) \
                    or delta >= 0:
                problems.append(
                    f"{vid} predicted_delta_cycles must be an int < 0")
            elif isinstance(base_ms, int) and isinstance(target, int):
                predicted = base_ms + delta
                if predicted > target:   # inclusive: == target is admissible
                    problems.append(
                        f"{vid} predicted makespan {predicted} > target "
                        f"{target} — the proposal never met the admission "
                        "line (§5.1)")
            if not proposal.get("edited_files"):
                problems.append(f"{vid} edited_files must be non-empty")
            tpid = proposal.get("target_pattern_id")
            if not isinstance(tpid, str) or not tpid.strip():
                problems.append(
                    f"{vid} target_pattern_id must be a non-empty free-form "
                    "label")

            row = latest.get(vid)
            if not row:
                problems.append(f"{vid} has no history row")
            elif row.get("round") != r \
                    or row.get("change_sig") != proposal.get("change_sig"):
                problems.append(f"{vid} history row does not match proposal")
            elif row.get("outcome") not in LEGAL_END_OUTCOMES:
                problems.append(
                    f"{vid} history row outcome {row.get('outcome')!r} is not "
                    "a legal round ending (expected one of "
                    f"{sorted(LEGAL_END_OUTCOMES)})")
            elif row.get("outcome") == "latency_pass" \
                    and row.get("latency_gate") != "pass":
                problems.append(f"{vid} latency_pass row lacks latency_gate "
                                "'pass'")
            if row and row.get("outcome") == "latency_fail":
                try:
                    direction = _load_json(rd / "direction.json",
                                           "direction.json")
                except ValueError as exc:
                    direction = None
                    problems.append(str(exc))
                sig = proposal.get("change_sig")
                if not isinstance(direction, dict):
                    problems.append(
                        "rounds/<R>/direction.json missing on the latency_fail "
                        "path (§5.1 Step4: failed_sigs must land there)")
                elif sig not in direction.get("failed_sigs", []):
                    problems.append(
                        f"direction.json failed_sigs does not contain {vid}'s "
                        "change_sig (§5.1 Step4)")

            vdir = art / "variants" / vid
            _check_doc(vdir / "business_logic.md",
                       f"{vid} business_logic.md", BL_SENTINEL,
                       BUSINESS_HEADINGS, problems)
            _check_doc(vdir / "information_analysis.md",
                       f"{vid} information_analysis.md", INFO_SENTINEL,
                       INFO_VARIANT_HEADINGS, problems)
            conf = vdir / "conformance.md"
            if not conf.is_file() or conf.stat().st_size == 0:
                problems.append(f"{vid} conformance.md missing or empty")
            else:
                text = conf.read_text(encoding="utf-8")
                for sentinel in (BL_SENTINEL, INFO_SENTINEL):
                    if sentinel not in text:
                        problems.append(
                            f"{vid} conformance.md does not record {sentinel}")
            _check_repair_trace(vdir / "repair_trace.json", vid, problems)

    analysis_path = rd / "analysis.md"
    if not analysis_path.is_file() or analysis_path.stat().st_size == 0:
        problems.append("rounds/<R>/analysis.md missing or empty")
    else:
        headings = {l.strip() for l in
                    analysis_path.read_text(encoding="utf-8").splitlines()}
        if "## latency" not in headings:
            problems.append("rounds/<R>/analysis.md has no '## latency' "
                            "section (§5.1 Step6: written every round, both "
                            "ending paths)")
    verdicts = rd / "verdicts.jsonl"
    if not proposal:
        # a zero-proposal round never measured anything: no verdicts file is
        # the honest disk state (the recheck never ran)
        pass
    elif not verdicts.is_file():
        problems.append("rounds/<R>/verdicts.jsonl missing")
    else:
        try:
            for line_no, line in enumerate(
                    verdicts.read_text(encoding="utf-8").splitlines(), 1):
                if line.strip() and not isinstance(json.loads(line), dict):
                    problems.append(f"verdicts.jsonl:{line_no} is not a JSON object")
        except json.JSONDecodeError as exc:
            problems.append(f"verdicts.jsonl unparseable: {exc}")

    if problems:
        for p in problems:
            print(f"check_propose_emit: FAIL {p}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "round": r, "profile_mode": profile_mode}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
