#!/usr/bin/env python3
"""rules_pool.py — mechanical operations on the accuracy-rule pool (no LLM).

Rules are only ever EXTRACTED from measured outcomes (probe rows + lineage);
this script never invents, judges, or rewrites a rule's content — it moves
validated rows between the three homes they live in:

  workspace   $ORCA_ARTIFACTS_DIR/accuracy_rules.json   (the in-run truth)
  mirror      <project_root>/docs/prof-opt/accuracy_rules.json (this project's
              machine-readable truth; full overwrite at terminal merge)
  pool        $ORCA_HOME/prof-opt/accuracy_rules_pool.json (cross-run,
              model_hash-keyed; local to this user and machine)

Workspace rule schema — every field required (annotation fields such as
`borrowed` are tolerated: required-set validation, not a closed schema):
  id / change_pattern / statement / direction (harmful|benign) /
  generality (model_specific|plausibly_general) / evidence_rounds [int] /
  vids [str] / confidence (low|medium|high) / metric_gap (finite number)

Pool entry schema:
  {change_pattern, direction, statement, generality,
   evidence: [{model_hash, rounds, vids}],
   confirm_models, refute_models, general, quarantined}
`general <=> |confirm_models| >= 2`, `quarantined <=> |refute_models| >= 2` —
set membership makes at-least-once re-merges idempotent. A quarantined entry
is never seeded.

model_hash = sha256 over the sorted (rel_path, sha256) sequence of
BASELINE.lock's py_files_sha256 map — anchored to the ORIGINAL model closure,
never to an advanced shadow. When the lock is absent (first-run seed timing),
the just-copied shadow *.py closure (excluding __pycache__/*.pyc) hashes
identically.

Subcommands:
  check  --artifacts <ws>                  validate the workspace rule file
  seed   --artifacts <ws> --project-root <root>
         rebuild the workspace rule file from mirror + pool (REFUSED when
         one already exists — reuse keeps the in-run truth)
  merge  --artifacts <ws> --project-root <root>
         terminal-state handoff: workspace -> mirror (full overwrite) + pool
         (evidence/confirm/refute set maintenance). Best-effort: a failure
         is disclosed on stderr and never blocks the terminal state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path

RULES_REQUIRED = ("id", "change_pattern", "statement", "direction",
                  "generality", "evidence_rounds", "vids", "confidence",
                  "metric_gap")
DIRECTIONS = ("harmful", "benign")
GENERALITIES = ("model_specific", "plausibly_general")
CONFIDENCES = ("low", "medium", "high")
SOURCE_RANK = {"mirror": 0, "pool_exact": 1, "pool_general": 2,
               "pool_borrowed": 3}
POOL_DISCLOSURE = " (pool entry: no local measured gap)"
GENERAL_DISCLOSURE = " (general pool entry: no local measured gap)"


def _ws_rules_path(artifacts: Path) -> Path:
    return artifacts / "accuracy_rules.json"


def _mirror_rules_path(project_root: Path) -> Path:
    return project_root / "docs" / "prof-opt" / "accuracy_rules.json"


def _pool_path() -> Path:
    home = os.environ.get("ORCA_HOME") or str(Path.home() / ".orca")
    return Path(home) / "prof-opt" / "accuracy_rules_pool.json"


# ── validation ────────────────────────────────────────────────────────────────

def validate_rules(rules: list) -> list[str]:
    """Errors for a workspace rules list; index-addressed (rule #1 = first)."""
    errors: list[str] = []
    seen: dict[str, int] = {}
    for i, rule in enumerate(rules, 1):
        if not isinstance(rule, dict):
            errors.append(f"rule #{i}: not a JSON object")
            continue
        missing = [k for k in RULES_REQUIRED if k not in rule]
        if missing:
            errors.append(f"rule #{i} ({rule.get('id', '?')}): missing "
                          f"field(s) {missing}")
            continue
        if rule["direction"] not in DIRECTIONS:
            errors.append(f"rule #{i} ({rule['id']}): direction must be one "
                          f"of {DIRECTIONS}, got {rule['direction']!r}")
        if rule["generality"] not in GENERALITIES:
            errors.append(f"rule #{i} ({rule['id']}): generality must be one "
                          f"of {GENERALITIES}, got {rule['generality']!r}")
        if rule["confidence"] not in CONFIDENCES:
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
            errors.append(f"rule #{i} ({rule['id']}): vids must be a list "
                          f"of strings")
        prev = seen.get(rule["change_pattern"])
        if prev is not None:
            errors.append(f"rule #{i} ({rule['id']}): duplicate "
                          f"change_pattern (already rule #{prev})")
        else:
            seen[rule["change_pattern"]] = i
    return errors


def _load_rules_doc(path: Path, what: str) -> tuple[list, list[str], bool,
                                                    list[str]]:
    """Read a {"rules": [...]} document -> (raw_rules, notes, missing,
    validation_errors). Missing/unparseable -> empty rules + disclosure."""
    if not path.is_file():
        return [], [f"{what} missing ({path})"], True, []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [], [f"{what} unparseable ({exc})"], False, []
    if not isinstance(doc, dict) or not isinstance(doc.get("rules"), list):
        return [], [f"{what} is not a {{'rules': [...]}} object"], False, []
    rules = doc["rules"]
    return rules, [], False, validate_rules(rules)


def _drop_bad_rows(rules: list, errors: list[str], what: str
                   ) -> tuple[list, list[str]]:
    """Keep only the rows no error names (best-effort sources: seed/merge)."""
    bad = {int(m.group(1)) for e in errors
           for m in [re.search(r"#(\d+)", e)] if m}
    kept = [r for i, r in enumerate(rules, 1) if i not in bad]
    notes = [f"{what}: dropped bad rule row(s): " + "; ".join(errors)] \
        if errors else []
    return kept, notes


# ── model_hash ────────────────────────────────────────────────────────────────

def _closure_hash(mapping: dict[str, str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(mapping):
        h.update(rel.encode("utf-8"))
        h.update(mapping[rel].encode("utf-8"))
    return h.hexdigest()


def compute_model_hash(artifacts: Path) -> str | None:
    """Anchor the model closure: BASELINE.lock's map when present, else the
    shadow closure (identical tree at first-run seed timing)."""
    lock_path = artifacts / "BASELINE.lock"
    if lock_path.is_file():
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            mapping = lock.get("py_files_sha256")
            if isinstance(mapping, dict) and mapping:
                return _closure_hash(mapping)
        except (json.JSONDecodeError, OSError):
            pass  # fall through to the shadow closure
    shadow = artifacts / "shadow"
    if not shadow.is_dir():
        return None
    mapping = {}
    for p in sorted(shadow.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        rel = str(p.relative_to(shadow)).replace("\\", "/")
        mapping[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return _closure_hash(mapping) if mapping else None


# ── seed ──────────────────────────────────────────────────────────────────────

def _confidence_by_evidence(rounds: list[int]) -> str:
    n = len(set(rounds))
    if n >= 3:
        return "high"
    if n == 2:
        return "medium"
    return "low"


def _downgrade(confidence: str) -> str:
    order = ["low", "medium", "high"]
    return order[max(0, order.index(confidence) - 1)]  # floor: low stays low


def _pool_rule(entry: dict, model_hash: str, disclosure: str) -> dict:
    """Materialize a pool entry as a workspace rule (pool- prefixed id).

    Evidence comes from this model's own records when present, else the
    union of all pool records. metric_gap is 0.0 with a disclosure sentence:
    the pool does not preserve per-run gap numbers, and pool-sourced rows
    never take part in gap-based confidence ladders."""
    matching = [e for e in entry.get("evidence", [])
                if e.get("model_hash") == model_hash]
    records = matching if matching else entry.get("evidence", [])
    rounds: list[int] = []
    vids: list[str] = []
    for rec in records:
        rounds.extend(int(r) for r in rec.get("rounds", []))
        vids.extend(str(v) for v in rec.get("vids", []))
    return {
        "id": "pool-",
        "change_pattern": entry["change_pattern"],
        "statement": str(entry.get("statement", "")) + disclosure,
        "direction": entry["direction"],
        "generality": entry.get("generality", "model_specific"),
        "evidence_rounds": sorted(set(rounds)),
        "vids": sorted(set(vids)),
        "confidence": _confidence_by_evidence(rounds),
        "metric_gap": 0.0,
    }


def _load_pool_entries(notes: list[str]) -> list:
    try:
        doc = json.loads(_pool_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        notes.append(f"pool missing ({_pool_path()}) — treated as an empty source")
        return []
    except (json.JSONDecodeError, OSError) as exc:
        notes.append(f"pool unreadable ({exc}) — treated as an empty source")
        return []
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        notes.append("pool is not an {'entries': [...]} object — treated as "
                     "an empty source")
        return []
    good = []
    for i, entry in enumerate(doc["entries"], 1):
        if (isinstance(entry, dict) and "change_pattern" in entry
                and entry.get("direction") in DIRECTIONS):
            good.append(entry)
        else:
            notes.append(f"pool entry #{i} dropped (not a well-formed entry)")
    return good


def seed(artifacts: Path, project_root: Path) -> dict:
    ws_path = _ws_rules_path(artifacts)
    if ws_path.is_file():
        raise ValueError(
            f"seed refused: {ws_path} already exists — the workspace rule "
            f"file is the in-run truth and is never re-seeded (a fresh "
            f"rebuild needs fresh_start)")

    notes: list[str] = []
    mirror_raw, mirror_notes, _, mirror_errors = _load_rules_doc(
        _mirror_rules_path(project_root), "project mirror")
    mirror_rules, drop_notes = _drop_bad_rows(mirror_raw, mirror_errors,
                                              "project mirror")
    notes.extend(mirror_notes + drop_notes)
    entries = _load_pool_entries(notes)

    model_hash = compute_model_hash(artifacts)
    if model_hash is None:
        notes.append("model closure unresolvable (no BASELINE.lock and no "
                     "shadow *.py) — model_hash-keyed pool source skipped")

    candidates: list[tuple[str, dict]] = [("mirror", r) for r in mirror_rules]
    for entry in entries:
        if entry.get("quarantined"):
            continue  # a quarantined entry is never seeded
        if model_hash is not None and any(
                e.get("model_hash") == model_hash
                for e in entry.get("evidence", [])):
            candidates.append(("pool_exact",
                               _pool_rule(entry, model_hash, POOL_DISCLOSURE)))
        elif entry.get("general"):
            candidates.append(("pool_general",
                               _pool_rule(entry, "", GENERAL_DISCLOSURE)))
        elif entry.get("generality") == "plausibly_general":
            rule = _pool_rule(entry, "", POOL_DISCLOSURE)
            rule["borrowed"] = True
            rule["confidence"] = _downgrade(rule["confidence"])
            candidates.append(("pool_borrowed", rule))
    # stable sort by source priority: a lower-rank (more local) source wins
    # the change_pattern; mirror first regardless of file order
    candidates.sort(key=lambda c: SOURCE_RANK[c[0]])

    sources = {"mirror": 0, "pool_exact": 0, "pool_general": 0,
               "pool_borrowed": 0}
    chosen: dict[str, dict] = {}
    for source, rule in candidates:
        pattern = rule["change_pattern"]
        if pattern in chosen:
            kept = chosen[pattern]
            if kept["direction"] != rule["direction"]:
                kept["statement"] += (f" [conflicting observation dropped: "
                                      f"direction={rule['direction']} "
                                      f"({rule['id']})]")
            continue
        chosen[pattern] = rule
        sources[source] += 1

    rules: list[dict] = []
    pool_seq = 0
    for pattern in sorted(chosen):
        rule = chosen[pattern]
        if rule["id"] == "pool-":
            pool_seq += 1
            rule = {**rule, "id": f"pool-{pool_seq:04d}"}
        rules.append(rule)

    errors = validate_rules(rules)
    if errors:  # an internal materialization bug, never bad input
        raise ValueError("; ".join(errors))

    ws_path.write_text(
        json.dumps({"rules": rules}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    return {"rules": len(rules), **sources,
            "model_hash": model_hash or "unresolved", "notes": notes}


# ── merge ─────────────────────────────────────────────────────────────────────

def merge(artifacts: Path, project_root: Path) -> dict:
    notes: list[str] = []
    ws_path = _ws_rules_path(artifacts)
    if not ws_path.is_file():
        notes.append(f"workspace rules missing ({ws_path}) — nothing to "
                     f"merge, mirror and pool untouched")
        return {"merged": 0, "confirm_added": 0, "refute_added": 0,
                "model_hash": "unresolved", "notes": notes}
    ws_raw, load_notes, _, ws_errors = _load_rules_doc(ws_path,
                                                       "workspace rules")
    rules, drop_notes = _drop_bad_rows(ws_raw, ws_errors, "workspace rules")
    notes.extend(load_notes + drop_notes)

    mirror_path = _mirror_rules_path(project_root)
    try:
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.write_text(
            json.dumps({"rules": rules}, indent=2, ensure_ascii=False),
            encoding="utf-8")
    except OSError as exc:
        notes.append(f"mirror write failed ({exc}) — pool merge continues")

    model_hash = compute_model_hash(artifacts)
    pool_path = _pool_path()
    confirm_added = refute_added = 0
    if model_hash is None:
        notes.append("model closure unresolvable — pool merge skipped "
                     "(mirror still written)")
        return {"merged": len(rules), "confirm_added": 0, "refute_added": 0,
                "model_hash": "unresolved", "mirror": str(mirror_path),
                "pool": str(pool_path), "notes": notes}

    entries = _load_pool_entries(notes)
    for rule in rules:
        key = (rule["change_pattern"], rule["direction"])
        entry = next((e for e in entries
                      if (e.get("change_pattern"), e.get("direction")) == key),
                     None)
        if entry is None:
            entries.append({
                "change_pattern": rule["change_pattern"],
                "direction": rule["direction"],
                "statement": rule["statement"],
                "generality": rule["generality"],
                "evidence": [{"model_hash": model_hash,
                              "rounds": sorted(set(rule["evidence_rounds"])),
                              "vids": sorted(set(rule["vids"]))}],
                "confirm_models": [model_hash],  # the founding model counts
                "refute_models": [],
                "general": False, "quarantined": False,
            })
            confirm_added += 1
        else:
            entry["statement"] = rule["statement"]  # latest observation wins
            record = next((e for e in entry.get("evidence", [])
                           if e.get("model_hash") == model_hash), None)
            if record is None:
                entry.setdefault("evidence", []).append({
                    "model_hash": model_hash,
                    "rounds": sorted(set(rule["evidence_rounds"])),
                    "vids": sorted(set(rule["vids"]))})
                confirms = entry.setdefault("confirm_models", [])
                if model_hash not in confirms:
                    confirms.append(model_hash)
                    confirm_added += 1
            else:
                record["rounds"] = sorted(set(record.get("rounds", []))
                                          | set(rule["evidence_rounds"]))
                record["vids"] = sorted(set(record.get("vids", []))
                                        | set(rule["vids"]))
        # cross-model refutation: same pattern, opposite direction observed
        for other in entries:
            if (other.get("change_pattern") == rule["change_pattern"]
                    and other.get("direction") != rule["direction"]):
                refutes = other.setdefault("refute_models", [])
                if model_hash not in refutes:
                    refutes.append(model_hash)
                    refute_added += 1

    for entry in entries:
        entry["general"] = len(entry.get("confirm_models", [])) >= 2
        entry["quarantined"] = len(entry.get("refute_models", [])) >= 2

    try:
        pool_path.parent.mkdir(parents=True, exist_ok=True)
        pool_path.write_text(
            json.dumps({"entries": entries}, indent=2, ensure_ascii=False),
            encoding="utf-8")
    except OSError as exc:
        notes.append(f"pool write failed ({exc}) — mirror already written")

    return {"merged": len(rules), "confirm_added": confirm_added,
            "refute_added": refute_added, "model_hash": model_hash,
            "mirror": str(mirror_path), "pool": str(pool_path),
            "notes": notes}


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("check", "seed", "merge"):
        sp = sub.add_parser(name)
        sp.add_argument("--artifacts", required=True)
        if name in ("seed", "merge"):
            sp.add_argument("--project-root", required=True)
    ns = ap.parse_args()
    artifacts = Path(ns.artifacts)

    if ns.command == "check":
        rules, notes, missing, errors = _load_rules_doc(
            _ws_rules_path(artifacts), "workspace rules")
        if missing:
            print(f"rules_pool check: FAIL workspace rules missing "
                  f"({_ws_rules_path(artifacts)})", file=sys.stderr)
            return 2
        for note in notes:
            print(f"rules_pool check: note: {note}", file=sys.stderr)
        if errors:
            for err in errors:
                print(f"rules_pool check: FAIL {err}", file=sys.stderr)
            return 2
        print(json.dumps({"rules": len(rules), "errors": 0}))
        return 0

    try:
        project_root = Path(ns.project_root)
        result = (seed(artifacts, project_root) if ns.command == "seed"
                  else merge(artifacts, project_root))
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
