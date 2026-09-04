"""history_lib.py — the ONLY write path for history.jsonl (typed builders).

Bare dict appends are forbidden by design: every row goes through one of the
per-node builders so the field sets stay pinned to the history schema.
Rows are append-only multi-version snapshots — each append writes the FULL
merged state of a vid, so "latest version per vid" is just the last row
with that vid.

Row semantics: impl / latency_improved / latency_fail / success /
accuracy_fail / probe_insufficient. Terminal outcomes are written ONLY via
append_terminal (the watchdog's terminal action); the v5-only builders and
their field sets (round advance, probe gate, proxy accuracy) are retired
(kept on old workspace rows for READ compatibility only — never written
anymore).

Read side:
    read_rows(path)            -> all rows in append order
    read_latest(path)          -> {vid: last row}
Dedup side (mechanical rules — no LLM judgement):
    permanent set   = outcome in {advanced, promoted, unsupported_op}
                      ("advanced"/"promoted" are kept for READ compatibility
                      with old workspaces only — v6 never writes them);
                      judged over ANY version row (a later row cannot
                      resurrect a permanently-exhausted signature);
    latency_improved is a process state and NEVER blocks;
    structural_mismatch / variant_broken share ONE joint retry budget per sig
                      (total attempts with those outcomes <= 2, i.e. <= 1 retry);
    probe_insufficient permanently consumes the signature (v7: the proxy
                      budget is fixed epoch-only — no knob exists whose
                      change would reopen it; a genuine retry is a NEW
                      composition with a new signature).
    accuracy_fail is NOT permanent: a recovery-round composed proposal
                      produces a NEW change_sig that exact-match dedup admits
                      by design (the round-level rerouting signal is the
                      failed_sigs set in direction.json, not dedup).
The dedup key is unchanged in v6: (vid, change_sig) over full-snapshot rows
— repair iterations on the same vid/sig overwrite latest.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Field sets per builder (superset = history row schema). Keys are deliberately
# exhaustive; builders validate against these so a typo fails loudly.
IMPL_FIELDS = (
    "vid", "round", "seq", "parent_vid", "change_sig",
    "probe_epochs", "target_modules",
    "predicted_delta_cycles", "implemented", "base_at_proposal",
)
LATENCY_FIELDS = (
    "structural_check", "makespan_cycles", "latency_gate",
    "pred_actual_ratio", "outcome",
)
# terminal-row fields (v6 §4.3): the outcome-specific extras are written only
# when passed (None = omitted from the row) — an outcome that cannot have a
# field (e.g. latency_fail has no stopped_at_epoch) simply does not pass it.
TERMINAL_FIELDS = (
    "outcome",
    "gap",                        # final direction-normalized distance (budget units)
    "stopped_at_epoch",           # the epoch training actually stopped at (= E on success)
    "final_acc",                  # success: the final eval metric
    "over_budget_streak",         # accuracy_fail: the streak that fired the early stop
    "stage",                      # probe_insufficient: train_status stage at failure
    "max_retries_hit",            # probe_insufficient: the retry budget was exhausted
    "measured_makespan_cycles",   # latency_fail: the last measured makespan
)

TERMINAL_OUTCOMES = frozenset(
    {"success", "accuracy_fail", "probe_insufficient", "latency_fail"})
PERMANENT_OUTCOMES = frozenset({"advanced", "promoted", "unsupported_op"})
JOINT_RETRY_OUTCOMES = frozenset({"structural_mismatch", "variant_broken"})
JOINT_RETRY_MAX_ATTEMPTS = 2  # first failure + at most one retry


class HistoryError(RuntimeError):
    """Raised on schema violations — callers must fail loud, never patch."""


def history_path(artifacts_dir: str | Path) -> Path:
    return Path(artifacts_dir) / "history.jsonl"


# ── write side ────────────────────────────────────────────────────────────────

def _load_last_row(path: Path, vid: str) -> dict[str, Any]:
    last: dict[str, Any] | None = None
    if path.is_file():
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise HistoryError(f"history.jsonl:{line_no} is not valid JSON: {exc}") from exc
            if row.get("vid") == vid:
                last = row
    return dict(last) if last else {}


def _append(path: Path, vid: str, new_fields: dict[str, Any], allowed: tuple[str, ...],
            stage: str) -> dict[str, Any]:
    unknown = set(new_fields) - set(allowed)
    if unknown:
        raise HistoryError(f"{stage} builder got unknown fields {sorted(unknown)} "
                           f"(allowed: {allowed})")
    row = _load_last_row(path, vid)
    row.update(new_fields)
    row["vid"] = vid
    row["version"] = int(row.get("version", 0)) + 1
    row["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return row


def append_implemented(path: str | Path, vid: str, *, round: int, seq: int,
                       parent_vid: str | None, change_sig: str,
                       probe_epochs: int,
                       target_modules: list[str],
                       predicted_delta_cycles: int | None,
                       base_at_proposal: dict,
                       implemented: bool = True) -> dict:
    """First row of a vid (the proposal node's mechanical write after the
    implementer subagent returns). outcome stays unset unless broken.

    probe_epochs carries the proxy depth the variant judged under
    (verbatim from contracts.json proxy_budget — epoch-only in v7, no knob
    fields).

    base_at_proposal: the base pointer when the proposal was generated,
    e.g. {"vid": null, "makespan_cycles": 15288} — the lineage anchor for
    po_report's winner chain."""
    fields = {
        "vid": vid, "round": round, "seq": seq, "parent_vid": parent_vid,
        "change_sig": change_sig, "probe_epochs": probe_epochs,
        "target_modules": list(target_modules),
        "implemented": implemented, "base_at_proposal": dict(base_at_proposal),
    }
    if predicted_delta_cycles is not None:
        fields["predicted_delta_cycles"] = predicted_delta_cycles
    return _append(Path(path), vid, fields, IMPL_FIELDS, "append_implemented")


def append_outcome(path: str | Path, vid: str, outcome: str) -> dict:
    """Terminal outcome emitted before any latency/probe stage ran
    (implementation pre-check failures: variant_broken /
    structural_mismatch — written by the proposal node right after the
    implemented=False row)."""
    if outcome not in JOINT_RETRY_OUTCOMES:
        raise HistoryError(f"append_outcome only accepts {sorted(JOINT_RETRY_OUTCOMES)}, got {outcome!r}")
    return _append(Path(path), vid, {"outcome": outcome},
                   LATENCY_FIELDS, "append_outcome")


def append_latency(path: str | Path, vid: str, *, structural_check: str,
                   makespan_cycles: int | None, latency_gate: str | None,
                   pred_actual_ratio: float | None, outcome: str) -> dict:
    """L0 row (the batch latency recheck inside the proposal node).
    outcome: latency_improved (process) or one of the
    terminal L0 eliminations structural_mismatch / unsupported_op /
    latency_fail."""
    fields = {
        "structural_check": structural_check, "makespan_cycles": makespan_cycles,
        "latency_gate": latency_gate, "pred_actual_ratio": pred_actual_ratio,
        "outcome": outcome,
    }
    return _append(Path(path), vid, fields, LATENCY_FIELDS, "append_latency")


def append_terminal(path: str | Path, vid: str, *, outcome: str,
                    gap: float | None = None,
                    stopped_at_epoch: int | None = None,
                    final_acc: float | None = None,
                    over_budget_streak: int | None = None,
                    stage: str | None = None,
                    max_retries_hit: bool | None = None,
                    measured_makespan_cycles: int | None = None) -> dict:
    """Terminal row (v6 §4.3) — the ONLY writer of terminal outcomes; the
    watchdog's terminal action (and the report's last-resort disclosure
    path) both go through here.

    outcome must be one of TERMINAL_OUTCOMES (success / accuracy_fail /
    probe_insufficient / latency_fail); anything else fails loud. The
    per-outcome extras are written only when passed (None = omitted):
      success           gap / stopped_at_epoch (= E) / final_acc
      accuracy_fail     gap / stopped_at_epoch / over_budget_streak
      probe_insufficient stage / max_retries_hit
      latency_fail      measured_makespan_cycles (gap may repeat the last
                        recheck's gap when meaningful)
    The dedup key (vid, change_sig) is unchanged: the row rides the vid's
    full merged snapshot, so a repair iteration on the same vid/sig
    overwrites latest."""
    if outcome not in TERMINAL_OUTCOMES:
        raise HistoryError(
            f"append_terminal only accepts {sorted(TERMINAL_OUTCOMES)}, "
            f"got {outcome!r}")
    fields: dict[str, Any] = {"outcome": outcome}
    if gap is not None:
        fields["gap"] = gap
    if stopped_at_epoch is not None:
        fields["stopped_at_epoch"] = stopped_at_epoch
    if final_acc is not None:
        fields["final_acc"] = final_acc
    if over_budget_streak is not None:
        fields["over_budget_streak"] = over_budget_streak
    if stage is not None:
        fields["stage"] = stage
    if max_retries_hit is not None:
        fields["max_retries_hit"] = max_retries_hit
    if measured_makespan_cycles is not None:
        fields["measured_makespan_cycles"] = measured_makespan_cycles
    return _append(Path(path), vid, fields, TERMINAL_FIELDS, "append_terminal")


# ── read side ────────────────────────────────────────────────────────────────

def read_rows(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return []
    rows = []
    for line_no, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise HistoryError(f"history.jsonl:{line_no} is not valid JSON: {exc}") from exc
    return rows


def read_latest(path: str | Path) -> dict[str, dict[str, Any]]:
    """Latest version per vid (rows are full snapshots, so last-wins is enough)."""
    latest: dict[str, dict[str, Any]] = {}
    for row in read_rows(path):
        latest[row["vid"]] = row
    return latest


# ── dedup ────────────────────────────────────────────────────────────────────

def dedup_state(path: str | Path, change_sig: str,
                probe_epochs: int | None = None) -> dict[str, Any]:
    """Mechanical dedup verdict for a candidate change signature.

    probe_epochs is carried for disclosure only — the v7 proxy budget is
    fixed epoch-only, so a probe_insufficient signature is permanently
    consumed (no config knob exists whose change would reopen it; a genuine
    retry is a NEW composition).

    Returns {"blocked": bool, "reason": str}. Reads ALL version rows for the
    sig (permanent outcomes are judged on any version, not just the latest).
    """
    hits = [r for r in read_rows(path) if r.get("change_sig") == change_sig]

    for row in hits:
        if row.get("outcome") in PERMANENT_OUTCOMES:
            return {"blocked": True,
                    "reason": f"permanent outcome {row['outcome']!r} already recorded for this sig"}

    joint = sum(1 for row in hits if row.get("outcome") in JOINT_RETRY_OUTCOMES)
    if joint >= JOINT_RETRY_MAX_ATTEMPTS:
        return {"blocked": True,
                "reason": f"structural_mismatch/variant_broken joint budget exhausted "
                          f"({joint} attempts, max {JOINT_RETRY_MAX_ATTEMPTS})"}

    for row in hits:
        if row.get("outcome") == "probe_insufficient":
            return {"blocked": True,
                    "reason": "该签名已永久消费 (probe_insufficient — the proxy "
                              "budget is fixed epoch-only, no retry path "
                              "exists; compose a NEW signature instead)"}
    return {"blocked": False, "reason": "no prior terminal outcome for this sig"}


if __name__ == "__main__":
    # Small self-check CLI: print dedup verdict for one sig (pure read).
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--history", required=True)
    ap.add_argument("--sig", required=True)
    ap.add_argument("--probe-epochs", type=int, required=True,
                    help="the proxy depth from contracts.json proxy_budget "
                         "(disclosure only — the v7 budget is fixed "
                         "epoch-only, probe_insufficient is permanent)")
    ns = ap.parse_args()
    print(json.dumps(dedup_state(ns.history, ns.sig, ns.probe_epochs)))
    sys.exit(0)
