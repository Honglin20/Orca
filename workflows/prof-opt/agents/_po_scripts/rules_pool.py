#!/usr/bin/env python3
"""rules_pool.py — mechanical operations on the accuracy rules (no LLM, v7 §10).

Rules are only ever EXTRACTED from measured outcomes (terminal rows +
lineage); this script never invents, judges, or rewrites a rule's content.
Two homes (v7 deletes the cross-model pool machinery — SOURCE_RANK /
borrowed downgrade / confirm-refute sets / general-quarantine thresholds /
cross-model refutation were unreachable in a single-user single-model
reality and created destructive-overwrite paths):

  workspace   $ORCA_ARTIFACTS_DIR/accuracy_rules.json   (the in-run truth)
  mirror      <project_root>/docs/prof-opt/accuracy_rules.json (this
              project's machine-readable truth; written at terminal merge)

Workspace rule schema — every field required after `apply` (annotation
fields are tolerated: required-set validation, not a closed schema):
  id / change_pattern / statement / direction (harmful|benign) /
  evidence_rounds [int] / vids [str] /
  confidence (low|medium|high — ALWAYS written by `apply`, never by the
  LLM) / metric_gap (finite number)

Division of labor (v7): the accuracy-analyst LLM produces exactly three
judgment values (pattern normalization / has-a-lesson / statement);
`apply` derives confidence from the evidence-round count — the ladder:

    1 distinct round -> low; 2 -> medium; 3+ -> high

Subcommands:
  check  --artifacts <ws>                validate the workspace rule file
  apply  --artifacts <ws>                (re)compute confidence for every
         rule from its evidence_rounds — idempotent; runs after every
         accuracy-analyst dispatch, before check
  seed   --artifacts <ws> --project-root <root>
         rebuild the workspace rule file from the project mirror (REFUSED
         when one already exists — reuse keeps the in-run truth)
  merge  --artifacts <ws> --project-root <root> [--allow-empty]
         terminal-state handoff: workspace -> mirror (full overwrite,
         protected: an unparseable source file exits 2 instead of
         overwriting the mirror with an empty set; overwriting a non-empty
         mirror with an EMPTY rule set requires --allow-empty)
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

RULES_REQUIRED_PRE_APPLY = ("id", "change_pattern", "statement", "direction",
                            "evidence_rounds", "vids", "metric_gap")
RULES_REQUIRED = RULES_REQUIRED_PRE_APPLY + ("confidence",)
DIRECTIONS = ("harmful", "benign")
CONFIDENCES = ("low", "medium", "high")


def _ws_rules_path(artifacts: Path) -> Path:
    return artifacts / "accuracy_rules.json"


def _mirror_rules_path(project_root: Path) -> Path:
    return project_root / "docs" / "prof-opt" / "accuracy_rules.json"


# ── validation ────────────────────────────────────────────────────────────────

def validate_rules(rules: list, *, pre_apply: bool = False) -> list[str]:
    """Errors for a workspace rules list; index-addressed (rule #1 = first).
    pre_apply tolerates a missing `confidence` (apply fills it in)."""
    required = RULES_REQUIRED_PRE_APPLY if pre_apply else RULES_REQUIRED
    errors: list[str] = []
    seen: dict[str, int] = {}
    for i, rule in enumerate(rules, 1):
        if not isinstance(rule, dict):
            errors.append(f"rule #{i}: not a JSON object")
            continue
        missing = [k for k in required if k not in rule]
        if missing:
            errors.append(f"rule #{i} ({rule.get('id', '?')}): missing "
                          f"field(s) {missing}")
            continue
        if rule["direction"] not in DIRECTIONS:
            errors.append(f"rule #{i} ({rule['id']}): direction must be one "
                          f"of {DIRECTIONS}, got {rule['direction']!r}")
        if not pre_apply and rule["confidence"] not in CONFIDENCES:
            errors.append(f"rule #{i} ({rule['id']}): confidence must be one "
                          f"of {CONFIDENCES}, got {rule['confidence']!r}")
        gap = rule["metric_gap"]
        if (isinstance(gap, bool) or not isinstance(gap, (int, float))
                or not math.isfinite(gap)):
            errors.append(f"rule #{i} ({rule['id']}): metric_gap must be a "
                          f"finite number, got {gap!r}")
        if not (isinstance(rule["evidence_rounds"], list)
                and all(isinstance(r, int) and not isinstance(r, bool)
                        for r in rule["evidence_rounds"])):
            errors.append(f"rule #{i} ({rule['id']}): evidence_rounds must "
                          f"be a list of ints")
        if not (isinstance(rule["vids"], list)
                and all(isinstance(v, str) for v in rule["vids"])):
            errors.append(f"rule #{i} ({rule['id']}): vids must be a list of "
                          f"strings")
        prev = seen.get(rule["change_pattern"])
        if prev is not None:
            errors.append(f"rule #{i} ({rule['id']}): duplicate "
                          f"change_pattern (already rule #{prev})")
        else:
            seen[rule["change_pattern"]] = i
    return errors


def _read_rules_doc(path: Path, what: str) -> tuple[list | None, list[str], bool]:
    """Read a {"rules": [...]} document -> (rules|None, notes, unparseable).

    rules=None + unparseable=True means the file EXISTS but cannot be read
    as a rules document — the caller must refuse to treat it as an empty
    set (v7's destructive-overwrite fix)."""
    if not path.is_file():
        return None, [f"{what} missing ({path})"], False
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return None, [f"{what} unparseable ({exc})"], True
    if not isinstance(doc, dict) or not isinstance(doc.get("rules"), list):
        return None, [f"{what} is not a {{'rules': [...]}} object"], True
    return doc["rules"], [], False


def _drop_bad_rows(rules: list, errors: list[str], what: str
                   ) -> tuple[list, list[str]]:
    """Keep only the rows no error names (best-effort sources: seed)."""
    bad = {int(m.group(1)) for e in errors
           for m in [re.search(r"#(\d+)", e)] if m}
    kept = [r for i, r in enumerate(rules, 1) if i not in bad]
    notes = [f"{what}: dropped bad rule row(s): " + "; ".join(errors)] \
        if errors else []
    return kept, notes


# ── apply (confidence ladder — mechanical, never the LLM's call) ─────────────

def confidence_by_evidence(rounds: list[int]) -> str:
    n = len(set(rounds))
    if n >= 3:
        return "high"
    if n == 2:
        return "medium"
    return "low"


def apply_confidence(artifacts: Path) -> dict:
    ws_path = _ws_rules_path(artifacts)
    rules, notes, unparseable = _read_rules_doc(ws_path, "workspace rules")
    if rules is None and unparseable:
        raise ValueError("; ".join(notes) + " — refusing to apply over an "
                         "unparseable file")
    if rules is None:
        return {"rules": 0, "recomputed": 0, "notes": notes}
    errors = validate_rules(rules, pre_apply=True)
    if errors:
        raise ValueError("; ".join(errors))
    recomputed = 0
    for rule in rules:
        ladder = confidence_by_evidence(rule["evidence_rounds"])
        if rule.get("confidence") != ladder:
            recomputed += 1
        rule["confidence"] = ladder
    ws_path.write_text(
        json.dumps({"rules": rules}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    return {"rules": len(rules), "recomputed": recomputed, "notes": notes}


# ── seed ──────────────────────────────────────────────────────────────────────

def seed(artifacts: Path, project_root: Path) -> dict:
    ws_path = _ws_rules_path(artifacts)
    if ws_path.is_file():
        raise ValueError(
            f"seed refused: {ws_path} already exists — the workspace rule "
            f"file is the in-run truth and is never re-seeded (a fresh "
            f"rebuild needs fresh_start)")

    notes: list[str] = []
    mirror_raw, mirror_notes, mirror_unparseable = _read_rules_doc(
        _mirror_rules_path(project_root), "project mirror")
    if mirror_raw is None and mirror_unparseable:
        # never seed from a broken mirror (an empty seed would silently
        # discard the project's measured lessons)
        raise ValueError("; ".join(mirror_notes) + " — fix the mirror file "
                         "or remove it; refusing to degrade to an empty seed")
    mirror_notes = mirror_notes if mirror_raw is None else []
    rules = mirror_raw or []
    errors = validate_rules(rules)
    rules, drop_notes = _drop_bad_rows(rules, errors, "project mirror")
    notes.extend(mirror_notes + drop_notes)

    ws_path.write_text(
        json.dumps({"rules": rules}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    return {"rules": len(rules), "mirror": str(_mirror_rules_path(project_root)),
            "notes": notes}


# ── merge (terminal handoff, protected against destructive overwrites) ───────

def merge(artifacts: Path, project_root: Path, allow_empty: bool = False) -> dict:
    notes: list[str] = []
    ws_path = _ws_rules_path(artifacts)
    mirror_path = _mirror_rules_path(project_root)
    rules, load_notes, unparseable = _read_rules_doc(ws_path,
                                                    "workspace rules")
    if rules is None:
        if unparseable:
            # v7 §10: an unparseable source NEVER overwrites the mirror
            # with an empty set — exit 2 (the caller discloses and the
            # mirror survives)
            raise ValueError("; ".join(load_notes) + " — merge REFUSED: the "
                             "workspace rule file is unparseable, "
                             "overwriting the mirror with an empty set "
                             "would destroy the project's measured lessons")
        notes.append(f"workspace rules missing ({ws_path}) — nothing to "
                     f"merge, mirror untouched")
        return {"merged": 0, "mirror": str(mirror_path), "notes": notes}
    notes.extend(load_notes)
    errors = validate_rules(rules)
    rules, drop_notes = _drop_bad_rows(rules, errors, "workspace rules")
    notes.extend(drop_notes)

    if not rules and not allow_empty:
        existing, _, existing_unparseable = _read_rules_doc(
            mirror_path, "project mirror")
        if existing_unparseable:
            raise ValueError(
                f"project mirror unparseable ({mirror_path}) — merge REFUSED "
                f"rather than overwrite a broken-but-present mirror")
        if existing:
            raise ValueError(
                f"merge REFUSED: the workspace rule set is EMPTY and the "
                f"mirror holds {len(existing)} rule(s) — overwriting "
                f"measured lessons with nothing needs an explicit "
                f"--allow-empty")

    try:
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.write_text(
            json.dumps({"rules": rules}, indent=2, ensure_ascii=False),
            encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"mirror write failed ({exc})") from exc
    return {"merged": len(rules), "mirror": str(mirror_path), "notes": notes}


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("check", "apply", "seed", "merge"):
        sp = sub.add_parser(name)
        sp.add_argument("--artifacts", required=True)
        if name in ("seed", "merge"):
            sp.add_argument("--project-root", required=True)
        if name == "merge":
            sp.add_argument("--allow-empty", action="store_true",
                            help="allow an EMPTY workspace rule set to "
                                 "overwrite a non-empty mirror (explicit "
                                 "consent — otherwise refused)")
    ns = ap.parse_args()
    artifacts = Path(ns.artifacts)

    if ns.command == "check":
        rules, notes, unparseable = _read_rules_doc(
            _ws_rules_path(artifacts), "workspace rules")
        if rules is None and unparseable:
            print(f"rules_pool check: FAIL workspace rules unparseable "
                  f"({_ws_rules_path(artifacts)})", file=sys.stderr)
            return 2
        for note in notes:
            print(f"rules_pool check: note: {note}", file=sys.stderr)
        if rules is None:
            print(f"rules_pool check: FAIL workspace rules missing "
                  f"({_ws_rules_path(artifacts)})", file=sys.stderr)
            return 2
        errors = validate_rules(rules)
        if errors:
            for err in errors:
                print(f"rules_pool check: FAIL {err}", file=sys.stderr)
            return 2
        print(json.dumps({"rules": len(rules), "errors": 0}))
        return 0

    try:
        if ns.command == "apply":
            result = apply_confidence(artifacts)
        elif ns.command == "seed":
            result = seed(artifacts, Path(ns.project_root))
        else:
            result = merge(artifacts, Path(ns.project_root),
                           allow_empty=getattr(ns, "allow_empty", False))
    except (OSError, ValueError, KeyError) as exc:
        print(f"rules_pool {ns.command}: FAIL {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    for note in result.pop("notes", []):
        print(f"rules_pool {ns.command}: note: {note}", file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
