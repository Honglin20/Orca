#!/usr/bin/env python3
"""Pre-return gate for po_propose (v7, SPEC §5).

Verifies the single-variant convergence loop closed its disk state before
the node emits:

  1. proposals.json parses with `round == R` and holds EXACTLY ONE
      proposal with a non-empty change description. Prediction is optional
      calibration evidence, not an admission gate. A zero-proposal round is legal
     only with a non-empty `exhausted_rationale`.
  2. The round's variant carries `variants/<vid>/assessment.md` (the
     variant-assessor subagent's product — sentinel first line, non-empty
     body, the six required section headings including the conclusion
     sub-section `### 被牺牲信息与预期精度代价`). The v6 two-document +
     conformance.md bundle is deleted.
  3. The analysis stamp uses the v7 key
     `<vid>|<change_sig>|sha256(variants/<vid>/declaration.json)` — both
     repair classes (latency / structural) rewrite declaration.json, so
     the key changes with them and the mechanical re-entry guard holds.
  4. The vid has its expected history row; a latency-passing vid has a
      `latency_improved` row, an eliminated vid its terminal/elimination
     outcome (the mechanical avoid list in base/frontier.json derives the
     rerouting signal — no hand-written direction file exists in v8).
  4b. Composition lineage (v8): the proposal's `absorbs` list names only
     vids that exist in history (never the vid itself), and
     architecture_decision.md carries the `## absorbs` / `## avoids`
     sections whose `r<round>-<seq>` references all exist on disk.
  5. variants/<vid>/repair_trace.json, when present, records
     repair_count == len(attempts) and repair_count <= 5.
  6. rounds/<R>/analysis.md exists, is non-empty, and carries the
     `## latency` section — required on BOTH ending paths.
  7. The history digest (`base/history_digest.md`) exists and carries a seal
     that covers THIS closing round (digest_stamp.py) — the next round's
     Step 0 reads the digest as its brief input, so the seal is part of the
     round's disk contract on BOTH ending paths.
  8. On success (and only then) it fires the per-round docs-manifest push
     (§5.6) — fail-soft, never blocks the emit.

This is structural completeness only; proposal quality, verdicts, and the
soft-alignment judgment ("does the variant still make sense vs the
baseline") are not re-judged here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

VAS_SENTINEL = "[subagent:variant-assessor v1 VAS4K9]"
CANDIDATE_SENTINELS = {
    "semantic.md": "[subagent:semantic-architecture-proposer v1 SAP1A1]",
    "hardware.md": "[subagent:hardware-architecture-proposer v1 HAP1B1]",
    "sota.md": "[subagent:sota-architecture-proposer v1 SOTA1C1]",
}
SELECTOR_SENTINEL = "[subagent:architecture-selector v1 ASC1D1]"
# variant-assessor section contract (§5.3): the last heading of the
# document is its conclusion section and carries the required sub-section
ASSESSMENT_HEADINGS = ("## 任务语义", "## 输入输出", "## 架构动机",
                       "## 逐模块职责与物理意义", "## 训练目标与指标方向",
                       "## 与基线差异")
ASSESSMENT_SUBSECTION = "### 被牺牲信息与预期精度代价"
# §5.3 outcomes a round's single vid may legitimately end on at emit time
LEGAL_END_OUTCOMES = frozenset({
    "latency_improved", "latency_fail", "structural_mismatch", "variant_broken",
    "unsupported_op"})
REPAIR_MAX = 5


def _load_json(path: Path, what: str) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise ValueError(f"{what} unparseable: {path} ({exc})") from exc


def _check_assessment(path: Path, vid: str, problems: list[str]) -> None:
    """Sentinel + non-empty body + the six section headings + the
    conclusion sub-section (§5.3)."""
    if not path.is_file():
        problems.append(f"{vid} assessment.md missing ({path})")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != VAS_SENTINEL:
        problems.append(f"{vid} assessment.md first line is not the sentinel "
                        f"{VAS_SENTINEL!r}")
        return
    body = [l for l in lines[1:] if l.strip()]
    if not body:
        problems.append(f"{vid} assessment.md body is empty (sentinel only)")
    present = {l.strip() for l in lines}
    for heading in ASSESSMENT_HEADINGS:
        if heading not in present:
            problems.append(f"{vid} assessment.md missing section heading "
                            f"{heading!r}")
    if ASSESSMENT_SUBSECTION not in present:
        problems.append(f"{vid} assessment.md missing the conclusion "
                        f"sub-section {ASSESSMENT_SUBSECTION!r}")


def _check_first_line(path: Path, sentinel: str, problems: list[str]) -> None:
    if not path.is_file():
        problems.append(f"required architecture document missing: {path}")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != sentinel:
        problems.append(f"{path} first line is not {sentinel!r}")


def _check_analysis_stamp(art: Path, vid: str, sig: str,
                          problems: list[str]) -> None:
    """The v7 stamp key: vid|change_sig|sha256(declaration.json) — changes
    with either repair class, so a stale stamp can never green-light a
    skipped re-assessment (§5.3 stamp fix)."""
    stamp_path = art / "variants" / vid / ".analysis_stamp.json"
    decl_path = art / "variants" / vid / "declaration.json"
    if not decl_path.is_file():
        problems.append(f"{vid} declaration.json missing (the stamp key "
                        "hashes it)")
        return
    expected = (f"{vid}|{sig}|"
                f"{hashlib.sha256(decl_path.read_bytes()).hexdigest()}")
    stamp = _load_json(stamp_path, f"{vid} .analysis_stamp.json")
    if not isinstance(stamp, dict) or stamp.get("key") != expected:
        got = stamp.get("key") if isinstance(stamp, dict) else None
        problems.append(
            f"{vid} .analysis_stamp.json does not carry the v7 key "
            f"{expected!r} (got {got!r}) — the assessment was not stamped "
            "against the CURRENT declaration (re-run the variant-assessor "
            "and write the stamp)")


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
            f"budget of {REPAIR_MAX} — the 6th repair must be "
            "intercepted, never emitted")


def _check_decision_lineage(path: Path, known_vids: set[str],
                            problems: list[str]) -> None:
    """v8 composition-lineage gate on the selector's decision document: the
    `## absorbs` / `## avoids` sections must exist, and every vid-shaped
    reference inside them must name a vid that exists in history."""
    if not path.is_file():
        problems.append(f"architecture_decision.md missing ({path})")
        return
    lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines()]
    for section in ("## absorbs", "## avoids"):
        if section not in lines:
            problems.append(f"architecture_decision.md missing the {section} "
                            "section (composition provenance is declared, "
                            "never implied)")
    section_text: dict[str, list[str]] = {}
    current = None
    for line in lines:
        if line.startswith("## "):
            current = line
            section_text[current] = []
        elif current is not None:
            section_text[current].append(line)
    vid_pattern = re.compile(r"\br\d+-\d+\b")
    for section in ("## absorbs", "## avoids"):
        for token in vid_pattern.findall("\n".join(section_text.get(section, []))):
            if token not in known_vids:
                problems.append(
                    f"architecture_decision.md {section} references "
                    f"{token}, which has no history row — provenance is "
                    "mechanical, never invented")


def _push_docs_manifest(art: Path) -> None:
    """Per-round docs-manifest push (v7 §5.6): once the gate passes, the
    round's analysis / candidates / decision docs go live on the web panel
    immediately instead of waiting for the report's final pass. push_curves
    is fail-soft by contract — a dead daemon must never block the emit —
    but its stderr notes are relayed so a push failure stays visible."""
    script = art / "scripts" / "push_curves.py"
    if not script.is_file():
        sys.stderr.write("check_propose_emit: docs push skipped "
                         "(push_curves.py not deployed)\n")
        return
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--artifacts", str(art), "--docs"],
            capture_output=True, text=True, timeout=30)
    except Exception as exc:  # noqa: BLE001 — fail-soft by contract
        sys.stderr.write(f"check_propose_emit: docs push failed "
                         f"(best-effort, ignored): {exc}\n")
        return
    if proc.stderr.strip():
        sys.stderr.write("check_propose_emit: docs push notes:\n"
                         + proc.stderr)
    if proc.returncode != 0:
        sys.stderr.write(f"check_propose_emit: docs push rc={proc.returncode} "
                         "(best-effort, ignored)\n")


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
        import digest_stamp
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

    rd = art / f"rounds/{r:03d}"
    for name, sentinel in CANDIDATE_SENTINELS.items():
        _check_first_line(rd / "candidates" / name, sentinel, problems)
    _check_first_line(rd / "architecture_decision.md", SELECTOR_SENTINEL,
                      problems)
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
                    "proposal per round")
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

    try:
        # Single source (history_lib): the anchor is ALWAYS the frozen origin
        # baseline in v8 (no promotion); a stale base/incumbent.json or a
        # missing anchor fails loud here.
        history_lib.expected_base(art)
    except ValueError as exc:
        problems.append(str(exc))

    _check_decision_lineage(rd / "architecture_decision.md",
                            set(latest), problems)

    if proposal:
        vid = proposal.get("vid")
        if not isinstance(vid, str) or not vid:
            problems.append("proposal missing vid")
        else:
            for key in ("change_sig", "edited_files", "change_spec",
                        "absorbs", "target_pattern_id",
                        "predicted_acc_impact",
                        "sota_reference"):
                if key not in proposal:
                    problems.append(f"{vid} proposal missing {key}")
            if not proposal.get("edited_files"):
                problems.append(f"{vid} edited_files must be non-empty")
            elif any(not (art / "shadow" / rel).is_file()
                     for rel in proposal["edited_files"]):
                problems.append(f"{vid} edited_files contains a path absent "
                                "from the origin baseline shadow")
            if "op_delta" in proposal:
                problems.append(f"{vid} proposal must not contain op_delta")
            absorbs = proposal.get("absorbs")
            if not isinstance(absorbs, list) or not all(
                    isinstance(v, str) and v for v in absorbs):
                problems.append(
                    f"{vid} absorbs must be a list of vid strings "
                    "(composition lineage; empty is legal)")
            else:
                if vid in absorbs:
                    problems.append(
                        f"{vid} absorbs names the vid itself — a design "
                        "cannot absorb itself")
                unknown = [v for v in absorbs if v not in latest]
                if unknown:
                    problems.append(
                        f"{vid} absorbs names vid(s) absent from history: "
                        f"{unknown} — provenance is mechanical, never invented")
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
            elif row.get("outcome") == "latency_improved" \
                    and row.get("latency_gate") != "pass":
                problems.append(f"{vid} latency_improved row lacks latency_gate "
                                "'pass'")
            elif row.get("absorbs") != absorbs:
                problems.append(
                    f"{vid} history row absorbs {row.get('absorbs')!r} does "
                    "not match the proposal's composition lineage")

            vdir = art / "variants" / vid
            _check_assessment(vdir / "assessment.md", vid, problems)
            _check_analysis_stamp(art, vid, proposal.get("change_sig", ""),
                                  problems)
            _check_repair_trace(vdir / "repair_trace.json", vid, problems)

    analysis_path = rd / "analysis.md"
    if not analysis_path.is_file() or analysis_path.stat().st_size == 0:
        problems.append("rounds/<R>/analysis.md missing or empty")
    else:
        headings = {l.strip() for l in
                    analysis_path.read_text(encoding="utf-8").splitlines()}
        if "## latency" not in headings:
            problems.append("rounds/<R>/analysis.md has no '## latency' "
                            "section (written every round, both ending "
                            "paths)")
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

    problems.extend(digest_stamp.problems_for_emit(art, r))

    if problems:
        for p in problems:
            print(f"check_propose_emit: FAIL {p}", file=sys.stderr)
        return 1
    _push_docs_manifest(art)
    print(json.dumps({"ok": True, "round": r}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
