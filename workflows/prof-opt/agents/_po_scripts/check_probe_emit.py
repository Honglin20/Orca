#!/usr/bin/env python3
"""Pre-return gate for po_probe.

Verifies terminal probe outcomes and the phase-specific disk closure before
emit. It never re-judges a probe outcome; it only checks that the files and
history rows required by the node contract exist and parse.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


TERMINAL_PROBE = {"accuracy_pass", "accuracy_fail", "probe_insufficient"}


def _load_json(path: Path, what: str):
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
        mode = round_state.mode_state(art)["mode"]
        r = round_state.current_round(art)
    except Exception as exc:
        print(f"check_probe_emit: FAIL mode/round unavailable: {exc}",
              file=sys.stderr)
        return 1

    rd = art / f"rounds/{r:03d}"
    if mode == "latency":
        marker_path = art / ".round_advanced"
        try:
            marker = _load_json(marker_path, ".round_advanced")
        except ValueError as exc:
            problems.append(str(exc))
        else:
            if marker is None or marker.get("round") != r or marker.get("mode") != "latency":
                problems.append(".round_advanced does not record (round, latency)")
    else:
        results_path = rd / "probe_results.jsonl"
        history_path = art / "history.jsonl"
        rows = history_lib.read_rows(history_path)
        latest = history_lib.read_latest(history_path)
        best = _load_json(art / "best.json", "best.json")
        if best is None or not isinstance(best.get("vid"), str):
            problems.append("best.json missing or lacks vid")
        else:
            best_vid = best["vid"]
            best_has_prior_probe = any(
                row.get("vid") == best_vid
                and row.get("round") != r
                and row.get("outcome") in TERMINAL_PROBE
                for row in rows)
            if not best_has_prior_probe:
                expected_vids = {best_vid}
            else:
                expected_vids = {
                    row["vid"]
                    for row in rows
                    if row.get("round") == r
                    and row.get("outcome") == "latency_pass"
                }

            if not results_path.is_file() or results_path.stat().st_size == 0:
                problems.append("rounds/<R>/probe_results.jsonl missing or empty")
            else:
                result_vids: set[str] = set()
                try:
                    for line_no, line in enumerate(
                            results_path.read_text(encoding="utf-8").splitlines(), 1):
                        if not line.strip():
                            continue
                        row = json.loads(line)
                        vid = row.get("vid")
                        outcome = row.get("outcome")
                        if not isinstance(vid, str) or outcome not in TERMINAL_PROBE:
                            problems.append(
                                f"probe_results.jsonl:{line_no} missing terminal outcome")
                            continue
                        result_vids.add(vid)
                        latest_row = latest.get(vid)
                        if not latest_row or latest_row.get("outcome") not in TERMINAL_PROBE:
                            problems.append(
                                f"{vid} has no terminal probe row in history.jsonl")
                except json.JSONDecodeError as exc:
                    problems.append(f"probe_results.jsonl unparseable: {exc}")

                missing = expected_vids - result_vids
                if missing:
                    problems.append(
                        "probe_results.jsonl missing terminal rows for "
                        f"{sorted(missing)}")
                for vid in result_vids - expected_vids:
                    problems.append(
                        f"probe_results.jsonl contains unexpected vid {vid}")

        for rel in ("analysis.md", "direction.json"):
            path = rd / rel
            if not path.is_file() or path.stat().st_size == 0:
                problems.append(f"rounds/<R>/{rel} missing or empty")
        try:
            marker = _load_json(art / ".round_advanced", ".round_advanced")
        except ValueError as exc:
            problems.append(str(exc))
        else:
            if marker is None or marker.get("round") != r or marker.get("mode") != "accuracy":
                problems.append(".round_advanced does not record (round, accuracy)")
    if problems:
        for p in problems:
            print(f"check_probe_emit: FAIL {p}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "round": r, "mode": mode}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
