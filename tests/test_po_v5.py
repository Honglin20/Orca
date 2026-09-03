"""test_po_v5.py — retained early-era machinery tests (file kept for history).

Script-level unit tests for the machinery v7 KEEPS: the origin anchor freeze
from raw schedule_result.json, the deployed-set version stamp, gate_node's
stamp-verify wiring (finish-failed disclosure on tamper) and decision
pass-through, the accuracy rules file (check/apply/seed/merge — v7: the
cross-model pool machinery is deleted; the project mirror is the one
permanent home), and the retired-inputs grep. The profiling-mode resolver
died with the v7 mfu-only redesign; placeholder/probe-era state-machine
coverage lives (updated) in test_po_v6.py / test_po_v7.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workflows" / "prof-opt" / "agents" / "_po_scripts"
sys.path.insert(0, str(_SCRIPTS))

def _run_cli(args: list[str], env: dict | None = None,
             timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], capture_output=True,
                          text=True, timeout=timeout, env=env)


def _write_anchor(artifacts: Path, baseline: int = 1000, ratio: float = 0.5,
                  budget: float = 0.1) -> Path:
    """Origin anchor on disk with the canonical schema (target per formula)."""
    anchor = artifacts / "base" / "origin_anchor.json"
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text(json.dumps({
        "baseline_makespan_cycles": baseline,
        "latency_reduction_min": ratio,
        "accuracy_budget": budget,
        "target_cycles": int(baseline * (1 - ratio)) + 1,
        "frozen_at_round": 0}), encoding="utf-8")
    return anchor


# ── round_state ───────────────────────────────────────────────────────────────

def _round_state(artifacts: Path, command: str
                 ) -> subprocess.CompletedProcess:
    return _run_cli([str(_SCRIPTS / "round_state.py"),
                     "--artifacts", str(artifacts), command])


def test_round_state_current_zero_pads_and_ignores_non_numeric(tmp_path):
    art = tmp_path / "ws"
    out = _round_state(art, "current")
    assert json.loads(out.stdout) == {"round": 0, "round_dir": None}

    for name in ("001", "005", "junk", "00X"):
        (art / "rounds" / name).mkdir(parents=True)
    out = _round_state(art, "current")
    assert json.loads(out.stdout) == {"round": 5, "round_dir": "rounds/005"}


def test_round_state_bad_command_rejected(tmp_path):
    art = tmp_path / "ws"
    art.mkdir()
    proc = _round_state(art, "bogus")
    assert proc.returncode != 0   # argparse choices fail loud


# ── freeze_origin.sh: raw schedule_result.json -> immutable anchor ─────────────

_FREEZE_ORIGIN = (_REPO / "workflows" / "prof-opt" / "agents" /
                  "po_baseline" / "scripts" / "freeze_origin.sh")


def _freeze(tmp_path: Path, ratio: str, budget: str,
            ) -> subprocess.CompletedProcess:
    raw_dir = tmp_path / "base" / "profile" / "model"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "schedule_result.json").write_text(json.dumps({
        "serial_cycles": 400,
        "parallel_cycles": 310,
    }), encoding="utf-8")
    env = os.environ.copy()
    env["ORCA_ARTIFACTS_DIR"] = str(tmp_path)
    return subprocess.run(["bash", str(_FREEZE_ORIGIN), ratio, budget],
                          capture_output=True, text=True, env=env)


def test_freeze_origin_reads_raw_parallel_cycles(tmp_path):
    proc = _freeze(tmp_path, "0.5", "0.1")
    assert proc.returncode == 0, proc.stderr
    anchor = json.loads((tmp_path / "base" / "origin_anchor.json")
                        .read_text(encoding="utf-8"))
    assert anchor == {"baseline_makespan_cycles": 310,
                      "latency_reduction_min": 0.5, "accuracy_budget": 0.1,
                      "target_cycles": 156, "frozen_at_round": 0}


def test_freeze_origin_is_idempotent_and_rejects_drift(tmp_path):
    assert _freeze(tmp_path, "0.5", "0.1").returncode == 0
    before = (tmp_path / "base" / "origin_anchor.json").read_bytes()
    assert _freeze(tmp_path, "0.5", "0.1").returncode == 0
    assert (tmp_path / "base" / "origin_anchor.json").read_bytes() == before
    conflict = _freeze(tmp_path, "0.4", "0.1")
    assert conflict.returncode != 0
    assert "fresh_start" in conflict.stderr


@pytest.mark.parametrize("ratio,budget", [("0", "0.1"), ("1", "0.1"),
                                          ("1.5", "0.1"), ("0.5", "-1")])
def test_freeze_origin_rejects_invalid_ranges(tmp_path, ratio, budget):
    proc = _freeze(tmp_path, ratio, budget)
    assert proc.returncode != 0
    assert not (tmp_path / "base" / "origin_anchor.json").exists()

# ── gate CLI: retired flags rejected ─────────────────────────────────────────

def test_gate_cli_rejects_retired_flags(tmp_path):
    art = tmp_path / "ws"
    art.mkdir()
    for flag in ("--latency-reduction-min", "--stall-rounds"):
        proc = _run_cli([str(_SCRIPTS / "gate_decide.py"),
                         "--artifacts", str(art), flag, "0.5"])
        assert proc.returncode != 0, flag
        assert flag in proc.stderr


# ── deploy_scripts: version stamp ─────────────────────────────────────────────

_DEPLOY_SH = _SCRIPTS / "deploy_scripts.sh"


def _deploy_env(art: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "ORCA_PYTHON"}
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    return env


def test_deploy_writes_version_stamp_and_verify_roundtrip(tmp_path):
    art = tmp_path / "art"
    art.mkdir()
    proc = subprocess.run(["bash", str(_DEPLOY_SH)], capture_output=True,
                          text=True, timeout=60, env=_deploy_env(art))
    assert proc.returncode == 0, proc.stderr
    stamp = json.loads((art / "scripts" / ".VERSION").read_text(encoding="utf-8"))
    assert set(stamp) == {"manifest"} and len(stamp["manifest"]) == 64
    manifest1 = json.loads(proc.stdout)["manifest"]
    assert stamp["manifest"] == manifest1

    # steady state: verify passes, and a re-deploy keeps the same manifest
    verify = subprocess.run(["bash", str(_DEPLOY_SH), "--verify"],
                            capture_output=True, text=True, timeout=60,
                            env=_deploy_env(art))
    assert verify.returncode == 0, verify.stderr
    assert manifest1 in verify.stderr
    again = subprocess.run(["bash", str(_DEPLOY_SH)], capture_output=True,
                           text=True, timeout=60, env=_deploy_env(art))
    assert json.loads(again.stdout)["manifest"] == manifest1


def test_deploy_verify_detects_tampering(tmp_path):
    art = tmp_path / "art"
    art.mkdir()
    subprocess.run(["bash", str(_DEPLOY_SH)], capture_output=True, timeout=60,
                   env=_deploy_env(art))
    # tamper with one deployed file
    victim = art / "scripts" / "build_sig.py"
    victim.write_text(victim.read_text(encoding="utf-8") + "\n# tampered\n",
                      encoding="utf-8")
    proc = subprocess.run(["bash", str(_DEPLOY_SH), "--verify"],
                          capture_output=True, text=True, timeout=60,
                          env=_deploy_env(art))
    assert proc.returncode == 1
    assert "does not match" in proc.stderr

    # a missing stamp file fails the same way (never-deployed/tampered)
    (art / "scripts" / ".VERSION").unlink()
    proc2 = subprocess.run(["bash", str(_DEPLOY_SH), "--verify"],
                          capture_output=True, text=True, timeout=60,
                          env=_deploy_env(art))
    assert proc2.returncode == 1
    assert ".VERSION missing" in proc2.stderr


# ── gate_node: stamp verify wiring ───────────────────────────────────────────

def test_gate_node_stamp_mismatch_routes_finish_failed(tmp_path):
    art = tmp_path / "art"
    art.mkdir()
    subprocess.run(["bash", str(_DEPLOY_SH)], capture_output=True, timeout=60,
                   env=_deploy_env(art))
    # tamper -> the wrapper's verify branch fires BEFORE any decision
    victim = art / "scripts" / "gate_decide.py"
    victim.write_text("# tampered\n", encoding="utf-8")
    env = _deploy_env(art)
    proc = subprocess.run(["bash", str(_SCRIPTS / "gate_node.sh"),
                           "--max-rounds", "5"],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["decision"] == "finish-failed"
    assert ".VERSION" in payload["reason"] or "stamp" in payload["reason"]
    assert payload["error"].startswith("deploy --verify failed")
    # the disclosure payload carries the decision field set, never a bare
    # legacy-shaped object; the decision itself matches no explicit route
    # and lands in the catch-all
    assert payload["success_vids"] == [] and payload["in_flight"] == []
    assert payload["round"] == 0 and payload["target_cycles"] == 0


def test_gate_node_decision_passes_through(tmp_path):
    art = tmp_path / "art"
    art.mkdir()
    subprocess.run(["bash", str(_DEPLOY_SH)], capture_output=True, timeout=60,
                   env=_deploy_env(art))
    _write_anchor(art, baseline=1000, ratio=0.5)
    (art / "rounds" / "001").mkdir(parents=True)
    env = _deploy_env(art)
    proc = subprocess.run(["bash", str(_SCRIPTS / "gate_node.sh"),
                           "--max-rounds", "5", "--idle-round-cap", "5"],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["decision"] == "loop"
    assert payload["error"] == ""
    assert payload["target_cycles"] == 501
    assert payload["success_vids"] == [] and payload["in_flight"] == []


# ── rules_pool (v7: mirror-only, apply ladder, protected merge) ───────────────

_RULES_SH = _SCRIPTS / "rules_pool.py"


def _rules_env(art: Path, project_root: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "ORCA_HOME"}
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    return env


def _rule(**over) -> dict:
    base = {"id": "rule-0001", "change_pattern": "reduce_layers>=2",
            "statement": "降层 ≥2 精度崩", "direction": "harmful",
            "evidence_rounds": [3],
            "vids": ["r3-01"], "confidence": "low", "metric_gap": 0.61}
    base.update(over)
    return base


def _ws(tmp_path: Path) -> tuple[Path, Path]:
    art = tmp_path / "ws"
    art.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    return art, proj


def _run_rules(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_RULES_SH), *args],
                          capture_output=True, text=True, timeout=60, env=env)


def test_rules_pool_check_valid_and_annotation_tolerance(tmp_path):
    art, proj = _ws(tmp_path)
    (art / "accuracy_rules.json").write_text(json.dumps(
        {"rules": [_rule(borrowed=True)]}), encoding="utf-8")
    proc = _run_rules(_rules_env(art, proj), "check", "--artifacts", str(art))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"rules": 1, "errors": 0}


@pytest.mark.parametrize("bad,frag", [
    ("missing_field", "missing field"),
    ("dup_pattern", "duplicate change_pattern"),
    ("bad_direction", "direction must be one of"),
    ("bad_confidence", "confidence must be one of"),
    ("bad_gap", "metric_gap must be a finite number"),
    ("not_object", "not a JSON object"),
])
def test_rules_pool_check_fail_loud_matrix(tmp_path, bad, frag):
    art, proj = _ws(tmp_path)
    rules = [_rule()]
    if bad == "missing_field":
        rules = [{k: v for k, v in _rule().items() if k != "statement"}]
    elif bad == "dup_pattern":
        rules = [_rule(), _rule(id="rule-0002", evidence_rounds=[5],
                                vids=["r5-02"])]
    elif bad == "bad_direction":
        rules = [_rule(direction="maybe")]
    elif bad == "bad_confidence":
        rules = [_rule(confidence="certain")]
    elif bad == "bad_gap":
        rules = [_rule(metric_gap="0.6")]
    elif bad == "not_object":
        rules = ["not an object"]
    (art / "accuracy_rules.json").write_text(json.dumps({"rules": rules}),
                                             encoding="utf-8")
    proc = _run_rules(_rules_env(art, proj), "check", "--artifacts", str(art))
    assert proc.returncode == 2
    assert frag in proc.stderr
    assert "rule #1" in proc.stderr          # the failing row is named


def test_rules_pool_check_requires_confidence(tmp_path):
    """Confidence is ALWAYS apply's product (the LLM never writes it): a row
    missing it fails check — the node must run apply first."""
    art, proj = _ws(tmp_path)
    rule = _rule()
    rule.pop("confidence")
    (art / "accuracy_rules.json").write_text(json.dumps({"rules": [rule]}),
                                             encoding="utf-8")
    proc = _run_rules(_rules_env(art, proj), "check", "--artifacts", str(art))
    assert proc.returncode == 2
    assert "confidence" in proc.stderr


def test_rules_pool_check_missing_file_rc2(tmp_path):
    art, proj = _ws(tmp_path)
    proc = _run_rules(_rules_env(art, proj), "check", "--artifacts", str(art))
    assert proc.returncode == 2
    assert "missing" in proc.stderr


def test_rules_pool_apply_confidence_ladder(tmp_path):
    """apply derives confidence from evidence-round count — the mechanical
    half of the analyst split (the LLM never writes confidence)."""
    art, proj = _ws(tmp_path)
    rules = [
        {**_rule(), "id": "rule-0001", "change_pattern": "one:round",
         "evidence_rounds": [3]},                       # 1 round -> low
        {**_rule(), "id": "rule-0002", "change_pattern": "two:rounds",
         "evidence_rounds": [3, 5]},                     # 2 -> medium
        {**_rule(), "id": "rule-0003", "change_pattern": "three:rounds",
         "evidence_rounds": [3, 5, 7]},                  # 3 -> high
        {**_rule(), "id": "rule-0004", "change_pattern": "dup:rounds",
         "evidence_rounds": [3, 3, 3]},                  # dupes count once -> low
    ]
    for rule in rules:
        rule.pop("confidence")          # what the analyst actually writes
    (art / "accuracy_rules.json").write_text(json.dumps({"rules": rules}),
                                             encoding="utf-8")
    proc = _run_rules(_rules_env(art, proj), "apply", "--artifacts", str(art))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["recomputed"] == 4
    ladder = {r["change_pattern"]: r["confidence"] for r in json.loads(
        (art / "accuracy_rules.json").read_text(encoding="utf-8"))["rules"]}
    assert ladder == {"one:round": "low", "two:rounds": "medium",
                      "three:rounds": "high", "dup:rounds": "low"}
    # idempotent: a second apply recomputes nothing
    again = _run_rules(_rules_env(art, proj), "apply", "--artifacts", str(art))
    assert json.loads(again.stdout)["recomputed"] == 0


def test_rules_pool_seed_from_mirror_only(tmp_path):
    """v7: the seed's only source is the project mirror — the cross-run pool
    is deleted (single user, single machine; the pool created destructive
    overwrite paths)."""
    art, proj = _ws(tmp_path)
    (proj / "docs" / "prof-opt").mkdir(parents=True)
    (proj / "docs" / "prof-opt" / "accuracy_rules.json").write_text(json.dumps(
        {"rules": [_rule(id="rule-0001", change_pattern="mirror:own",
                         evidence_rounds=[2], vids=["r2-01"])]}),
        encoding="utf-8")
    proc = _run_rules(_rules_env(art, proj), "seed",
                      "--artifacts", str(art), "--project-root", str(proj))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["rules"] == 1 and out["mirror"].endswith("accuracy_rules.json")
    seeded = json.loads((art / "accuracy_rules.json")
                        .read_text(encoding="utf-8"))["rules"]
    assert seeded[0]["change_pattern"] == "mirror:own"  # verbatim
    check = _run_rules(_rules_env(art, proj), "check", "--artifacts", str(art))
    assert check.returncode == 0


def test_rules_pool_seed_refuses_existing_workspace_rules(tmp_path):
    """REUSE never re-seeds: the guard lives IN the script, not only in the
    node prompt (prompt-level guards are advisory; this one is mechanical)."""
    art, proj = _ws(tmp_path)
    (art / "accuracy_rules.json").write_text(json.dumps({"rules": []}),
                                             encoding="utf-8")
    proc = _run_rules(_rules_env(art, proj), "seed",
                      "--artifacts", str(art), "--project-root", str(proj))
    assert proc.returncode == 2
    assert "refused" in proc.stderr


def test_rules_pool_seed_missing_sources_cold_start(tmp_path):
    art, proj = _ws(tmp_path)
    proc = _run_rules(_rules_env(art, proj), "seed",
                      "--artifacts", str(art), "--project-root", str(proj))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["rules"] == 0
    assert json.loads((art / "accuracy_rules.json")
                      .read_text(encoding="utf-8")) == {"rules": []}
    assert "mirror missing" in proc.stderr


def test_rules_pool_seed_refuses_unparseable_mirror(tmp_path):
    """v7 destructive-overwrite fix: a broken mirror never degrades to an
    empty seed (that would silently discard the project's lessons)."""
    art, proj = _ws(tmp_path)
    (proj / "docs" / "prof-opt").mkdir(parents=True)
    (proj / "docs" / "prof-opt" / "accuracy_rules.json").write_text(
        "{not json", encoding="utf-8")
    proc = _run_rules(_rules_env(art, proj), "seed",
                      "--artifacts", str(art), "--project-root", str(proj))
    assert proc.returncode == 2
    assert "unparseable" in proc.stderr


def test_rules_pool_seed_drops_bad_mirror_lines(tmp_path):
    art, proj = _ws(tmp_path)
    (proj / "docs" / "prof-opt").mkdir(parents=True)
    (proj / "docs" / "prof-opt" / "accuracy_rules.json").write_text(json.dumps(
        {"rules": [_rule(), {"id": "broken"}]}), encoding="utf-8")
    proc = _run_rules(_rules_env(art, proj), "seed",
                      "--artifacts", str(art), "--project-root", str(proj))
    assert proc.returncode == 0, proc.stderr
    rules = json.loads((art / "accuracy_rules.json")
                       .read_text(encoding="utf-8"))["rules"]
    assert len(rules) == 1                      # bad row dropped
    assert "dropped bad rule row" in proc.stderr


def test_rules_pool_merge_overwrites_mirror(tmp_path):
    art, proj = _ws(tmp_path)
    (art / "accuracy_rules.json").write_text(json.dumps(
        {"rules": [_rule(evidence_rounds=[3], vids=["r3-01"])]}),
        encoding="utf-8")
    proc = _run_rules(_rules_env(art, proj), "merge",
                      "--artifacts", str(art), "--project-root", str(proj))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["merged"] == 1
    mirror = json.loads((proj / "docs" / "prof-opt" / "accuracy_rules.json")
                        .read_text(encoding="utf-8"))
    assert mirror == {"rules": [_rule(evidence_rounds=[3], vids=["r3-01"])]}


def test_rules_pool_merge_unparseable_source_refuses(tmp_path):
    """v7 §10: an unparseable workspace rule file must NEVER overwrite the
    mirror with an empty set — exit 2, the mirror survives."""
    art, proj = _ws(tmp_path)
    (art / "accuracy_rules.json").write_text("{broken", encoding="utf-8")
    (proj / "docs" / "prof-opt").mkdir(parents=True)
    (proj / "docs" / "prof-opt" / "accuracy_rules.json").write_text(
        json.dumps({"rules": [_rule()]}), encoding="utf-8")
    before = (proj / "docs" / "prof-opt" / "accuracy_rules.json").read_bytes()
    proc = _run_rules(_rules_env(art, proj), "merge",
                      "--artifacts", str(art), "--project-root", str(proj))
    assert proc.returncode == 2
    assert "REFUSED" in proc.stderr
    assert (proj / "docs" / "prof-opt" / "accuracy_rules.json") \
        .read_bytes() == before


def test_rules_pool_merge_empty_over_nonempty_needs_allow_empty(tmp_path):
    art, proj = _ws(tmp_path)
    (art / "accuracy_rules.json").write_text('{"rules": []}', encoding="utf-8")
    (proj / "docs" / "prof-opt").mkdir(parents=True)
    (proj / "docs" / "prof-opt" / "accuracy_rules.json").write_text(
        json.dumps({"rules": [_rule()]}), encoding="utf-8")
    env = _rules_env(art, proj)
    proc = _run_rules(env, "merge", "--artifacts", str(art),
                      "--project-root", str(proj))
    assert proc.returncode == 2
    assert "--allow-empty" in proc.stderr
    assert json.loads((proj / "docs" / "prof-opt" / "accuracy_rules.json")
                      .read_text(encoding="utf-8"))["rules"]  # untouched
    # explicit consent overwrites
    proc2 = _run_rules(env, "merge", "--artifacts", str(art),
                       "--project-root", str(proj), "--allow-empty")
    assert proc2.returncode == 0, proc2.stderr
    assert json.loads((proj / "docs" / "prof-opt" / "accuracy_rules.json")
                      .read_text(encoding="utf-8")) == {"rules": []}


def test_rules_pool_merge_without_workspace_rules_skips(tmp_path):
    art, proj = _ws(tmp_path)
    (proj / "docs" / "prof-opt").mkdir(parents=True)
    (proj / "docs" / "prof-opt" / "accuracy_rules.json").write_text(
        '{"sentinel": "do-not-touch"}', encoding="utf-8")
    proc = _run_rules(_rules_env(art, proj), "merge",
                      "--artifacts", str(art), "--project-root", str(proj))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["merged"] == 0
    # an early-failure run must not wipe the accumulated mirror
    assert (proj / "docs" / "prof-opt" / "accuracy_rules.json").read_text(
        encoding="utf-8") == '{"sentinel": "do-not-touch"}'
    assert "workspace rules missing" in proc.stderr


# ── retirement grep (mechanical acceptance, repeatable) ───────────────────────

def test_retired_inputs_never_reappear_anywhere_in_workflows():
    """The retired inputs must not be referenced anywhere under
    workflows/ — a single surviving {{ inputs.X }} ref crashes the render
    with StrictUndefined."""
    import subprocess
    hits = subprocess.run(
        ["grep", "-rn", "-E",
         r"inputs\.npu_chip|inputs\.npu_precision|inputs\.npu_core_num"
         r"|inputs\.write_back|inputs\.report_dir|inputs\.probe_epochs",
         str(_REPO / "workflows")],
        capture_output=True, text=True, timeout=60)
    assert hits.returncode == 1, f"retired input references survived:\n{hits.stdout}"
