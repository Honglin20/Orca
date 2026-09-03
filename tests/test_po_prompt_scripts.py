"""test_po_prompt_scripts.py — behavior tests for the scripts extracted from
prof-opt agent.md fences (prompt-cleanliness batch: deterministic logic lives
in scripts, the prompt keeps a single-line call).

Each test pins the contract the prompt used to inline: the exact output
shape, the fail-loud paths, and the skip rules — so a regression in any
extracted script is caught without an E2E run.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_AGENTS = _REPO / "workflows" / "prof-opt" / "agents"
_FLATTEN = _AGENTS / "po_flatten" / "scripts"
_BASELINE = _AGENTS / "po_baseline" / "scripts"
_CONTRACT = _AGENTS / "po_contract" / "scripts"
_PO = _AGENTS / "_po_scripts"
sys.path.insert(0, str(_PO))

from build_sig import build_change_sig  # noqa: E402


def _run(cmd: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, env=merged)


# ── po_flatten extractions ────────────────────────────────────────────────────

def test_list_shadow_pkgs_enumeration(tmp_path: Path):
    shadow = tmp_path / "shadow"
    (shadow / "pkg_a").mkdir(parents=True)
    (shadow / "pkg_a" / "__init__.py").write_text("")
    (shadow / "solo.py").write_text("")
    (shadow / "notes.txt").write_text("")
    r = _run(["bash", str(_FLATTEN / "list_shadow_pkgs.sh"), str(shadow)])
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["pkg_a", "solo"]
    # missing dir fails loud, never emits an empty list silently
    assert _run(["bash", str(_FLATTEN / "list_shadow_pkgs.sh"),
                 str(tmp_path / "nope")]).returncode == 2


def test_check_stdlib_clash_gates(tmp_path: Path):
    ok_dir = tmp_path / "ok"; ok_dir.mkdir()
    (ok_dir / "model.py").write_text("")
    (ok_dir / "mypkg").mkdir()
    r = _run([sys.executable, str(_FLATTEN / "check_stdlib_clash.py"),
              "--shadow", str(ok_dir)])
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("stdlib-collision-check: ok")

    clash_dir = tmp_path / "clash"; clash_dir.mkdir()
    (clash_dir / "json.py").write_text("")  # collides with the stdlib
    r = _run([sys.executable, str(_FLATTEN / "check_stdlib_clash.py"),
              "--shadow", str(clash_dir)])
    assert r.returncode == 1
    assert "FATAL" in r.stderr and "json" in r.stderr

    assert _run([sys.executable, str(_FLATTEN / "check_stdlib_clash.py"),
                 "--shadow", str(tmp_path / "nope")]).returncode == 2


def test_write_baseline_lock_anchor(tmp_path: Path):
    shadow = tmp_path / "shadow"
    (shadow / "pkg").mkdir(parents=True)
    (shadow / "pkg" / "model.py").write_text("class M:\n    pass\n")
    r = _run([sys.executable, str(_FLATTEN / "write_baseline_lock.py"),
              "--artifacts", str(tmp_path), "--model-path", "pkg/model.py"])
    assert r.returncode == 0, r.stderr
    lock = json.loads((tmp_path / "BASELINE.lock").read_text(encoding="utf-8"))
    # v7 schema: version / model_path / py_files_sha256 (the ckpt anchor is
    # deleted — training always starts from a fixed-seed random init)
    assert set(lock) == {"version", "model_path", "py_files_sha256"}
    assert lock["version"] == 2
    assert lock["model_path"] == "pkg/model.py"
    assert list(lock["py_files_sha256"]) == ["pkg/model.py"]
    digest = hashlib.sha256(
        (shadow / "pkg" / "model.py").read_bytes()).hexdigest()
    assert lock["py_files_sha256"]["pkg/model.py"] == digest
    # the ckpt flag is gone: passing it fails loud
    assert _run([sys.executable, str(_FLATTEN / "write_baseline_lock.py"),
                 "--artifacts", str(tmp_path), "--model-path", "pkg/model.py",
                 "--ckpt", ""]).returncode != 0


# ── po_contract extractions ───────────────────────────────────────────────────

def test_snapshot_tree_and_diff_exemptions(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("v1")
    (root / "sub").mkdir()
    (root / "sub" / "b.py").write_text("b")
    (root / "artifacts").mkdir()
    (root / "artifacts" / "junk.py").write_text("")   # skipped: workspace
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("")          # skipped: VCS
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "c.pyc").write_bytes(b"")  # skipped: cache
    pre = tmp_path / "pre.json"
    r = _run([sys.executable, str(_CONTRACT / "snapshot_tree.py"),
              "--root", str(root), "--out", str(pre)])
    assert r.returncode == 0, r.stderr
    snap = json.loads(pre.read_text(encoding="utf-8"))
    assert sorted(snap) == ["a.py", "sub/b.py"]

    (root / "a.py").write_text("v2")        # changed
    (root / "new.py").write_text("n")       # created
    (root / "sub" / "b.py").unlink()        # deleted
    post = tmp_path / "post.json"
    _run([sys.executable, str(_CONTRACT / "snapshot_tree.py"),
          "--root", str(root), "--out", str(post)])
    out = tmp_path / "exemptions.json"
    r = _run([sys.executable, str(_CONTRACT / "snapshot_diff.py"),
              "--pre", str(pre), "--post", str(post), "--out", str(out)])
    assert r.returncode == 0, r.stderr
    # faithful ordering: created/deleted group first, then the changed group
    # (each sorted) — the exact order the extracted fence produced
    assert json.loads(r.stdout) == {"exemptions":
                                    ["new.py", "sub/b.py", "a.py"]}
    assert json.loads(out.read_text(encoding="utf-8"))["exemptions"] == \
        ["new.py", "sub/b.py", "a.py"]
    # a missing snapshot is a loud invocation error, not an empty diff
    assert _run([sys.executable, str(_CONTRACT / "snapshot_diff.py"),
                 "--pre", str(tmp_path / "nope.json"), "--post", str(post),
                 "--out", str(out)]).returncode == 2


def test_shadow_pkgs_csv_resolution_order(tmp_path: Path):
    # v7 C10: the resolver is a SHARED deployed script (the probe render and
    # the contract stage both call it — never an inline one-liner)
    (tmp_path / "readiness").mkdir()
    (tmp_path / "readiness" / "readiness.json").write_text(
        '{"shadow_pkgs": ["from_readiness"]}', encoding="utf-8")
    r = _run([sys.executable, str(_PO / "shadow_pkgs_csv.py"),
              "--artifacts", str(tmp_path)])
    assert r.returncode == 0 and r.stdout.strip() == "from_readiness"
    # contracts.json, once assembled, wins over readiness
    (tmp_path / "contracts.json").write_text(
        '{"shadow": {"shadow_pkgs": ["a", "b"]}}', encoding="utf-8")
    r = _run([sys.executable, str(_PO / "shadow_pkgs_csv.py"),
              "--artifacts", str(tmp_path)])
    assert r.returncode == 0 and r.stdout.strip() == "a,b"
    # neither source -> fail loud
    empty = tmp_path / "empty"; empty.mkdir()
    r = _run([sys.executable, str(_PO / "shadow_pkgs_csv.py"),
              "--artifacts", str(empty)])
    assert r.returncode == 1 and "shadow_pkgs not found" in r.stderr


# ── po_baseline extraction ─────────────────────────────────────────────────────

def test_freeze_origin_guard_reads_raw_schedule_result(tmp_path: Path):
    art = tmp_path / "art"
    raw_dir = art / "base" / "profile" / "model"
    raw_dir.mkdir(parents=True)
    (raw_dir / "schedule_result.json").write_text(json.dumps({
        "serial_cycles": 200,
        "parallel_cycles": 100,
    }), encoding="utf-8")
    env = {"ORCA_ARTIFACTS_DIR": str(art)}
    script = ["bash", str(_BASELINE / "freeze_origin.sh"), "0.5", "0.05"]

    result = _run(script, env=env)
    assert result.returncode == 0, result.stderr
    anchor = json.loads((art / "base" / "origin_anchor.json")
                        .read_text(encoding="utf-8"))
    assert anchor["baseline_makespan_cycles"] == 100
    assert anchor["target_cycles"] == 51

    result = _run(script, env=env)
    assert result.returncode == 0, result.stderr
    assert _run(["bash", str(_BASELINE / "freeze_origin.sh")], env=env).returncode != 0

# ── deployed (_po_scripts) extractions ────────────────────────────────────────

def test_write_done_marker_sha_pinned(tmp_path: Path):
    vdir = tmp_path / "variants" / "r1-01"; vdir.mkdir(parents=True)
    decl = vdir / "declaration.json"
    decl.write_text('{"vid": "r1-01"}', encoding="utf-8")
    r = _run([sys.executable, str(_PO / "write_done_marker.py"),
              "--vid", "r1-01"], env={"ORCA_ARTIFACTS_DIR": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    marker = json.loads((vdir / "DONE").read_text(encoding="utf-8"))
    assert marker["vid"] == "r1-01"
    assert marker["declaration_sha256"] == hashlib.sha256(
        b'{"vid": "r1-01"}').hexdigest()
    assert "T" in marker["ts"]  # ISO timestamp
    # missing declaration -> loud, no marker written
    r = _run([sys.executable, str(_PO / "write_done_marker.py"),
              "--vid", "r9-99"], env={"ORCA_ARTIFACTS_DIR": str(tmp_path)})
    assert r.returncode == 2 and "declaration" in r.stderr


def test_append_impl_row_cli(tmp_path: Path):
    hist = tmp_path / "history.jsonl"
    r = _run([sys.executable, str(_PO / "append_impl_row.py"),
              "--history", str(hist), "--vid", "r1-01",
              "--round", "1", "--seq", "1", "--parent-vid", "null",
              "--change-sig", "activation:gelu->relu:b.0",
              "--probe-epochs", "1",
              "--target-modules", '["b.0"]',
              "--predicted-delta-cycles", "-100",
              "--base-at-proposal", '{"vid": null, "makespan_cycles": 15288}'])
    assert r.returncode == 0, r.stderr
    rows = [json.loads(ln) for ln in
            hist.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["implemented"] is True
    assert "probe_max_steps" not in rows[0]      # v7: the knob fields are gone
    assert rows[0]["base_at_proposal"]["makespan_cycles"] == 15288
    # terminal-skip path: implemented=False + the outcome row in one call
    r = _run([sys.executable, str(_PO / "append_impl_row.py"),
              "--history", str(hist), "--vid", "r1-02",
              "--round", "1", "--seq", "2", "--parent-vid", "r1-01",
              "--change-sig", "norm:rm:b.1",
              "--probe-epochs", "1", "--target-modules", '["b.1"]',
              "--predicted-delta-cycles", "-50",
              "--base-at-proposal", '{"vid": "r1-01", "makespan_cycles": 900}',
              "--not-implemented", "--outcome", "variant_broken"])
    assert r.returncode == 0, r.stderr
    rows = [json.loads(ln) for ln in
            hist.read_text(encoding="utf-8").splitlines()]
    last_two = [r for r in rows if r["vid"] == "r1-02"]
    assert last_two[0]["implemented"] is False
    assert last_two[-1]["outcome"] == "variant_broken"
    # malformed JSON flag -> loud argparse/json error, nothing written
    before = hist.read_text(encoding="utf-8")
    r = _run([sys.executable, str(_PO / "append_impl_row.py"),
              "--history", str(hist), "--vid", "r1-03",
              "--round", "1", "--seq", "3", "--parent-vid", "null",
              "--change-sig", "x", "--probe-epochs", "1",
              "--target-modules", "not-json",
              "--predicted-delta-cycles", "-1",
              "--base-at-proposal", '{"vid": null}'])
    assert r.returncode == 2
    assert hist.read_text(encoding="utf-8") == before
    # --outcome without --not-implemented is rejected: a silent outcome row on
    # an implemented=True vid would burn the sig's joint retry budget
    r = _run([sys.executable, str(_PO / "append_impl_row.py"),
              "--history", str(hist), "--vid", "r1-04",
              "--round", "1", "--seq", "4", "--parent-vid", "null",
              "--change-sig", "y", "--probe-epochs", "1",
              "--target-modules", '["m"]', "--predicted-delta-cycles", "-1",
              "--base-at-proposal", '{"vid": null}',
              "--outcome", "variant_broken"])
    assert r.returncode == 2 and "--not-implemented" in r.stderr


def test_build_sig_cli_matches_builder():
    r = _run([sys.executable, str(_PO / "build_sig.py"),
              "--lever", "activation", "--params", "gelu->relu",
              "--modules", '["b.1", "b.0"]'])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == build_change_sig(
        "activation", "gelu->relu", ["b.1", "b.0"])
    assert _run([sys.executable, str(_PO / "build_sig.py"),
                 "--lever", "", "--params", "p",
                 "--modules", '["m"]']).returncode == 2


def test_healed_files_script_is_gone():
    """v7 P5 deletes the heal ledger (write-only artifact): the script must
    not exist anywhere under the workflow tree."""
    assert not (_PO / "healed_files.py").exists()


# ── manifest direction gate (check_flatten.sh embedded check) ─────────────────

_DIRECTION_HEREDOC = None


def _direction_check_proc(manifest: Path) -> subprocess.CompletedProcess:
    """Run check_flatten.sh's embedded direction check on a standalone
    manifest — the heredoc python is self-contained (reads sys.argv[1]),
    so extract it once and drive it directly."""
    global _DIRECTION_HEREDOC
    import re
    if _DIRECTION_HEREDOC is None:
        text = (_FLATTEN / "check_flatten.sh").read_text(encoding="utf-8")
        blocks = re.findall(r"<<'PY'[^\n]*\n(.*?)\nPY\n", text, re.DOTALL)
        hits = [b for b in blocks if "Training And Evaluation" in b]
        assert len(hits) == 1, "direction-check heredoc not uniquely found"
        _DIRECTION_HEREDOC = hits[0]
    return _run([sys.executable, "-c", _DIRECTION_HEREDOC, str(manifest)])

def test_manifest_direction_gate_matrix(tmp_path: Path):
    """The tightened judgment: EVERY list item in the Training And
    Evaluation section is checked — a direction marker, or the explicit
    (non-metric) tag; keyword-shaped items cannot slip past, and the old
    hyphen spelling still fails."""
    def manifest(items: str) -> Path:
        p = tmp_path / f"m{abs(hash(items)) % 10**8}.md"
        p.write_text(
            "# manifest\n\n## Training And Evaluation\n" + items +
            "\n\n## Data And Environment\n- Interpreter: /usr/bin/python3\n",
            encoding="utf-8")
        return p

    # green: metric items with markers + a tagged non-metric note
    ok = manifest("- top1 accuracy: higher_better\n"
                  "- val loss: lower_better\n"
                  "- trains with AdamW, one cycle (non-metric)\n")
    proc = _direction_check_proc(ok)
    assert proc.returncode == 0, proc.stderr

    # an untagged non-metric item is rejected (no keyword escape)
    bad = manifest("- top1 accuracy: higher_better\n"
                   "- trains with AdamW, one cycle\n")
    proc = _direction_check_proc(bad)
    assert proc.returncode == 1
    assert "non-metric" in proc.stderr

    # the old hyphen spelling is rejected
    hyphen = manifest("- top1 accuracy: higher-better\n")
    proc = _direction_check_proc(hyphen)
    assert proc.returncode == 1
    assert "hyphen" in proc.stderr

    # no metric item at all is rejected
    none = manifest("- trains with AdamW (non-metric)\n")
    proc = _direction_check_proc(none)
    assert proc.returncode == 1
    assert "no ranking-metric" in proc.stderr
