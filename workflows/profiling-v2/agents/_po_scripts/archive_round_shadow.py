#!/usr/bin/env python3
"""archive_round_shadow.py — per-round source-tree archive (C1 + v2 facets).

After every gate pass, the current round's variant trees are archived so
each round keeps the exact code behind its proposal/verdict:
``variants/<vid>/shadow`` → ``rounds/<RRR>/<vid>/shadow`` (the model
closure) and — v2 §3 Step 3 — ``variants/<vid>/facets`` →
``rounds/<RRR>/<vid>/facets`` (the facet face: feature/loss pipeline copies;
archived only when present, so structure-only workspaces and variants
without a facets copy archive their shadow alone). The live variant trees
are mutable (later rounds reuse them); they are not a per-round record —
the archive is.

Rules (disclosure, never gate-blocking):
  * the round number comes from ``round_state.current_round`` (the single
    source; no hand-rolled heuristics). Round 0 (no numeric ``rounds/``
    directory) is a no-op that creates nothing;
  * the vid set is ``rounds/<RRR>/proposals.json`` → ``proposals[*].vid``
    (iterated as a list — never assumes exactly one proposal). A missing /
    unparseable file, a non-dict top level, or a non-list ``proposals`` is
    disclosed in the error file under the key ``__round__``;
  * each vid's source is copied SOURCE-ONLY (``__pycache__`` / ``*.pyc``
    ignored — no bytecode; sibling onnx/profile artifacts live outside the
    archived trees and are never touched) into a tmp dir
    ``rounds/<RRR>/<vid>/.<name>.tmp.<pid>`` then atomically renamed, so a
    destination is either fully present or absent. A stale tmp of the same
    name is removed first (pid reuse must not fake a failure). An existing
    complete destination dir is skipped — idempotent, safe under gate
    replay. A MISSING ``shadow`` is a per-vid error; a missing ``facets``
    is a legitimate skip (the facet face is optional per variant);
  * any per-vid failure (source missing / copy OSError) is one stderr line
    + a merged entry in ``rounds/<RRR>/shadow_archive_error.json``
    (repeated calls merge, they never clobber). A round where every vid
    succeeds DELETES a stale error file first, so replay never resurrects
    an old disclosure;
  * the script itself always exits 0 — archiving is a sidecar; the gate
    decision must never depend on it (the gate_node.sh mount guards with
    ``|| echo ... >&2``).

Usage:
    archive_round_shadow.py --artifacts DIR
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from round_state import current_round  # noqa: E402

_ERROR_FILENAME = "shadow_archive_error.json"
_ROUND_KEY = "__round__"  # error-file key for round-level (proposals) failures


def _merge_error(round_dir: Path, vid: str, error: str) -> None:
    """Merge-write one disclosure entry into ``shadow_archive_error.json``.

    Existing entries (other vids, earlier calls) survive; a corrupt existing
    file degrades to a fresh one (disclosed on stderr, never fatal).
    """
    path = round_dir / _ERROR_FILENAME
    data: dict[str, str] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"[archive_round_shadow] existing error file "
                             f"unreadable, rewriting it: {exc}\n")
            loaded = None
        if isinstance(loaded, dict):
            data = {str(k): str(v) for k, v in loaded.items()}
    data[vid] = error
    try:
        path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n",
                        encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"[archive_round_shadow] error file write failed "
                         f"(ignored): {exc}\n")


def _archive_tree(artifacts: Path, round_dir: Path, vid: str, name: str,
                  required: bool) -> str | None:
    """Copy one per-vid source tree (``shadow`` / v2 ``facets``) into
    ``rounds/<RRR>/<vid>/<name>``.

    Returns None on success (or already-archived skip — or the legitimate
    absent-facets skip); an error string the caller discloses. Atomic via
    tmp + rename; a failed copy removes its tmp.
    """
    src = artifacts / "variants" / vid / name
    if not src.is_dir():
        if required:
            return f"source missing or not a directory: variants/{vid}/{name}"
        return None          # facets face is per-variant optional — skip, no error
    dst = round_dir / vid / name
    if dst.is_dir():
        return None  # complete destination already archived — replay skip
    tmp = round_dir / vid / f".{name}.tmp.{os.getpid()}"
    try:
        round_dir.joinpath(vid).mkdir(parents=True, exist_ok=True)
        if tmp.is_dir():
            shutil.rmtree(tmp)  # stale tmp from a recycled pid — not a failure
        shutil.copytree(src, tmp,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        tmp.rename(dst)  # atomic same-fs rename: present or absent, never torn
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        return f"copy failed: {exc}"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", required=True,
                    help="workspace root (same as every _po_scripts consumer)")
    ns = ap.parse_args()
    artifacts = Path(ns.artifacts)

    try:
        round_no = current_round(artifacts)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"[archive_round_shadow] round_state failed "
                         f"(nothing archived): {exc}\n")
        return 0
    if round_no == 0:
        return 0  # no numeric rounds/ dir — nothing to archive, nothing created

    round_dir = artifacts / "rounds" / f"{round_no:03d}"
    proposals_path = round_dir / "proposals.json"
    try:
        doc = json.loads(proposals_path.read_text(encoding="utf-8"))
        proposals = doc.get("proposals") if isinstance(doc, dict) else None
        if not isinstance(proposals, list):
            raise ValueError("proposals is not a list")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        sys.stderr.write(f"[archive_round_shadow] proposals unreadable "
                         f"(nothing archived this round): {exc}\n")
        round_dir.mkdir(parents=True, exist_ok=True)
        _merge_error(round_dir, _ROUND_KEY, f"{type(exc).__name__}: {exc}")
        return 0

    vids = [p.get("vid") for p in proposals
            if isinstance(p, dict) and isinstance(p.get("vid"), str) and p.get("vid")]
    dropped = len(proposals) - len(vids)
    if dropped:
        # 丢弃可见（fail loud 精神）：无 vid 的 proposal 条目无法归档，stderr 披露
        sys.stderr.write(f"[archive_round_shadow] {dropped} proposal entries "
                         f"without a usable vid (skipped)\n")
    errors: list[tuple[str, str]] = []
    for vid in vids:
        # shadow is the mandatory model closure; facets is the v2 facet face
        # (optional per variant — structure-only workspaces have none)
        for name, required in (("shadow", True), ("facets", False)):
            err = _archive_tree(artifacts, round_dir, vid, name, required)
            if err is not None:
                sys.stderr.write(
                    f"[archive_round_shadow] {vid}/{name}: {err}\n")
                errors.append((f"{vid}/{name}", err))
    if errors:
        for vid, err in errors:
            _merge_error(round_dir, vid, err)
    elif vids:
        # 本轮确有 vid 且全部成功 → 删除 stale error 文件，旧披露不跨重放残留
        #（空 vid 集 = 无可归档，不算「全部成功」——不清历史披露）。
        try:
            (round_dir / _ERROR_FILENAME).unlink(missing_ok=True)
        except OSError as exc:
            sys.stderr.write(f"[archive_round_shadow] stale error file "
                             f"removal failed (ignored): {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
