"""test_po_v5.py — v5 mechanism tests (sequential-gating redesign).

Script-level unit tests for the v5 mechanics: round_state (the single round
source), the frozen origin anchor (analyze --freeze-origin), the v5 history
builders (advanced / probe gap), the anchor-budget verdicts, the dual-mode
round advance ((round, mode) idempotency key, torn-write repair,
direction.json), the v5 gate decision order, the deployed-set version stamp,
the profiling-mode resolver (env -> npu-smi -> fallback, column-aware chip
parse), the accuracy-rule pool (check/seed/merge), the reuse gate's
profiling-mode consistency, and the v5 latency recheck. The smoke section
drives the real script chain end to end on one fixture workspace.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workflows" / "prof-opt" / "agents" / "_po_scripts"
sys.path.insert(0, str(_SCRIPTS))

import history_lib  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_po_scripts import _write_profile_fixture  # noqa: E402


def _run_cli(args: list[str], env: dict | None = None,
             timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], capture_output=True,
                          text=True, timeout=timeout, env=env)


def _write_anchor(artifacts: Path, baseline: int = 1000, ratio: float = 0.5,
                  budget: float = 0.1) -> Path:
    """Origin anchor on disk with the v5 schema (target per SPEC formula)."""
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


def test_round_state_working_marker_linkage(tmp_path):
    art = tmp_path / "ws"
    (art / "rounds" / "001").mkdir(parents=True)
    # no marker -> max(current, 1)
    assert json.loads(_round_state(art, "working").stdout) == \
        {"round": 1, "round_dir": "rounds/001"}

    # marker at the CURRENT round -> next round
    (art / ".round_advanced").write_text(
        json.dumps({"round": 1, "mode": "latency"}), encoding="utf-8")
    assert json.loads(_round_state(art, "working").stdout) == \
        {"round": 2, "round_dir": "rounds/002"}

    # stale marker (older round) -> still max(current, 1)
    (art / ".round_advanced").write_text(
        json.dumps({"round": 0, "mode": "latency"}), encoding="utf-8")
    assert json.loads(_round_state(art, "working").stdout) == \
        {"round": 1, "round_dir": "rounds/001"}


def test_round_state_mode_two_states_and_missing_anchor_rc2(tmp_path):
    art = tmp_path / "ws"
    (art / "rounds" / "001").mkdir(parents=True)
    _write_anchor(art, baseline=1000, ratio=0.5)   # target = 501

    # no best.json -> latency
    assert json.loads(_round_state(art, "mode").stdout)["mode"] == "latency"

    (art / "best.json").write_text(json.dumps(
        {"vid": "r1-01", "makespan_cycles": 700, "proxy_acc": None,
         "round": 1, "profile_dir": "x"}), encoding="utf-8")
    state = json.loads(_round_state(art, "mode").stdout)
    assert state["mode"] == "latency" and state["target_cycles"] == 501

    (art / "best.json").write_text(json.dumps(
        {"vid": "r1-01", "makespan_cycles": 501, "proxy_acc": 0.9,
         "round": 1, "profile_dir": "x"}), encoding="utf-8")
    state = json.loads(_round_state(art, "mode").stdout)
    assert state["mode"] == "accuracy"
    assert state["best_makespan"] == 501

    # missing anchor -> exit 2 fail loud
    shutil.rmtree(art / "base")
    proc = _round_state(art, "mode")
    assert proc.returncode == 2
    assert "origin_anchor" in proc.stderr


def test_round_state_bad_command_rejected(tmp_path):
    art = tmp_path / "ws"
    art.mkdir()
    proc = _round_state(art, "bogus")
    assert proc.returncode != 0   # argparse choices fail loud


# ── analyze --freeze-origin ───────────────────────────────────────────────────

def _freeze(tmp_path: Path, ratio: str, budget: str,
            ) -> subprocess.CompletedProcess:
    profile_dir = tmp_path / "base" / "profile"
    if not (profile_dir / "profile_summary.json").is_file():
        _write_profile_fixture(profile_dir)   # fixture makespan = 310
    return _run_cli([str(_SCRIPTS / "analyze.py"),
                     "--profile-dir", str(profile_dir), "--freeze-origin",
                     "--latency-reduction-min", ratio,
                     "--accuracy-budget", budget])


def test_analyze_freeze_origin_first_write_formula(tmp_path):
    proc = _freeze(tmp_path, "0.5", "0.1")
    assert proc.returncode == 0, proc.stderr
    anchor = json.loads((tmp_path / "base" / "origin_anchor.json")
                        .read_text(encoding="utf-8"))
    # fixture baseline 310 x (1 - 0.5) + 1 = 156 (<= target <=> strictly below)
    assert anchor == {"baseline_makespan_cycles": 310,
                      "latency_reduction_min": 0.5, "accuracy_budget": 0.1,
                      "target_cycles": 156, "frozen_at_round": 0}
    assert "origin_anchor" in json.loads(proc.stdout)


def test_analyze_freeze_origin_idempotent_noop(tmp_path):
    assert _freeze(tmp_path, "0.5", "0.1").returncode == 0
    before = (tmp_path / "base" / "origin_anchor.json").read_bytes()
    proc = _freeze(tmp_path, "0.5", "0.1")
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "base" / "origin_anchor.json").read_bytes() == before
    assert "no-op" in json.loads(proc.stdout)["origin_anchor"]


def test_analyze_freeze_origin_conflict_rc2(tmp_path):
    assert _freeze(tmp_path, "0.5", "0.1").returncode == 0
    proc = _freeze(tmp_path, "0.4", "0.1")   # a different line
    assert proc.returncode == 2
    assert "IMMUTABLE" in proc.stderr
    assert "fresh_start" in proc.stderr


@pytest.mark.parametrize("ratio,budget", [("0", "0.1"), ("1", "0.1"),
                                          ("1.5", "0.1"), ("0.5", "-1")])
def test_analyze_freeze_origin_range_validation_rc2(tmp_path, ratio, budget):
    proc = _freeze(tmp_path, ratio, budget)
    assert proc.returncode == 2
    assert not (tmp_path / "base" / "origin_anchor.json").exists()


def test_analyze_freeze_origin_requires_both_params(tmp_path):
    profile_dir = tmp_path / "base" / "profile"
    _write_profile_fixture(profile_dir)
    proc = _run_cli([str(_SCRIPTS / "analyze.py"),
                     "--profile-dir", str(profile_dir), "--freeze-origin",
                     "--latency-reduction-min", "0.5"])
    assert proc.returncode == 2


def test_analyze_without_freeze_never_touches_anchor(tmp_path):
    profile_dir = tmp_path / "base" / "profile"
    _write_profile_fixture(profile_dir)
    anchor = profile_dir.parent / "origin_anchor.json"
    anchor.write_text('{"sentinel": "untouched"}', encoding="utf-8")
    mtime_before = os.stat(anchor).st_mtime_ns
    proc = _run_cli([str(_SCRIPTS / "analyze.py"),
                     "--profile-dir", str(profile_dir)])
    assert proc.returncode == 0, proc.stderr
    assert anchor.read_text(encoding="utf-8") == '{"sentinel": "untouched"}'
    assert os.stat(anchor).st_mtime_ns == mtime_before   # not rewritten


# ── history: advanced builder + probe gap ────────────────────────────────────

def test_history_append_advanced_writes_latency_field_set(tmp_path):
    hist = tmp_path / "history.jsonl"
    history_lib.append_implemented(
        hist, "r1-01", round=1, seq=1, parent_vid=None,
        change_sig="activation:gelu->relu:m", probe_epochs=1,
        probe_max_steps=None, probe_data_value=None,
        target_modules=["m"], predicted_delta_cycles=-100,
        base_at_proposal={"vid": None, "makespan_cycles": 1000})
    history_lib.append_latency(hist, "r1-01", structural_check="pass",
                               makespan_cycles=900, latency_gate="pass",
                               pred_actual_ratio=1.0, outcome="latency_pass")
    row = history_lib.append_advanced(hist, "r1-01")
    assert row["outcome"] == "advanced"
    # the marker row rides the promoted (LATENCY) field set on a full snapshot
    assert set(row) >= set(history_lib.LATENCY_FIELDS) | set(history_lib.IMPL_FIELDS)
    latest = history_lib.read_latest(hist)
    assert latest["r1-01"]["outcome"] == "advanced"
    assert latest["r1-01"]["makespan_cycles"] == 900


def test_history_permanent_set_v5(tmp_path):
    assert history_lib.PERMANENT_OUTCOMES == \
        frozenset({"advanced", "promoted", "unsupported_op"})
    # accuracy_fail is NOT permanent: a composed proposal's NEW sig passes
    # exact-match dedup by design
    hist = tmp_path / "history.jsonl"
    history_lib.append_implemented(
        hist, "r1-01", round=1, seq=1, parent_vid=None,
        change_sig="reduce_layers:2", probe_epochs=1, probe_max_steps=None,
        probe_data_value=None, target_modules=["m"], predicted_delta_cycles=-10,
        base_at_proposal={"vid": None, "makespan_cycles": 100})
    history_lib.append_latency(hist, "r1-01", structural_check="pass",
                               makespan_cycles=90, latency_gate="pass",
                               pred_actual_ratio=1.0, outcome="latency_pass")
    history_lib.append_probe(hist, "r1-01", proxy_acc=0.2,
                             promote_gate="fail", outcome="accuracy_fail",
                             gap=0.61)
    state = history_lib.dedup_state(hist, "reduce_layers:2", 1, None, None)
    assert state["blocked"] is False


def test_history_probe_gap_written_and_omitted(tmp_path):
    hist = tmp_path / "history.jsonl"
    row = history_lib.append_probe(
        hist, "r1-01", proxy_acc=0.9, promote_gate="pass",
        outcome="accuracy_pass", gap=0.05)
    assert row["gap"] == 0.05
    assert "gap" in history_lib.PROBE_FIELDS

    hist2 = tmp_path / "h2.jsonl"
    row2 = history_lib.append_probe(
        hist2, "r1-02", proxy_acc=None, promote_gate="fail",
        outcome="probe_insufficient")          # gap=None -> omitted
    assert "gap" not in row2


# ── gate CLI (v5): retired flags rejected ────────────────────────────────────

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
    victim = art / "scripts" / "predict_delta.py"
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


def test_gate_node_decision_passes_through(tmp_path):
    art = tmp_path / "art"
    art.mkdir()
    subprocess.run(["bash", str(_DEPLOY_SH)], capture_output=True, timeout=60,
                   env=_deploy_env(art))
    _write_anchor(art, baseline=1000, ratio=0.5)
    (art / "rounds" / "001").mkdir(parents=True)
    env = _deploy_env(art)
    proc = subprocess.run(["bash", str(_SCRIPTS / "gate_node.sh"),
                           "--max-rounds", "5"],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["decision"] == "loop"
    assert payload["error"] == ""
    assert payload["mode"] == "latency" and payload["target_cycles"] == 501


# ── resolve_profile_mode ─────────────────────────────────────────────────────

_RESOLVE_SH = _SCRIPTS / "resolve_profile_mode.sh"
_BASH = shutil.which("bash") or "/bin/bash"   # hermetic-PATH runs bypass PATH


def _resolve_env(art: Path | None = None, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ORCA_PO_NPU")}
    if art is not None:
        env["ORCA_ARTIFACTS_DIR"] = str(art)
    env.update(extra)
    return env


def test_resolve_profile_mode_env_priority_and_enums(tmp_path):
    art = tmp_path / "ws"
    art.mkdir()
    env = _resolve_env(art, ORCA_PO_NPU_CHIP="6613", ORCA_PO_NPU_PRECISION="AMP",
                       ORCA_PO_NPU_CORES="2")
    proc = subprocess.run(["bash", str(_RESOLVE_SH)], capture_output=True,
                          text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"mode": "mfu", "chip": "6613",
                                       "precision": "AMP", "core_num": 2,
                                       "resolved_by": "env"}
    # the file lands verbatim
    assert json.loads((art / "profile_mode.json").read_text(encoding="utf-8")) \
        == json.loads(proc.stdout)

    # defaults when the knobs are omitted
    env2 = _resolve_env(art, ORCA_PO_NPU_CHIP="1951")
    proc2 = subprocess.run(["bash", str(_RESOLVE_SH)], capture_output=True,
                           text=True, timeout=60, env=env2)
    payload2 = json.loads(proc2.stdout)
    assert payload2["precision"] == "INT8" and payload2["core_num"] == 1


@pytest.mark.parametrize("extra", [
    {"ORCA_PO_NPU_CHIP": "9900"},
    {"ORCA_PO_NPU_CHIP": "6613", "ORCA_PO_NPU_PRECISION": "FP4"},
    {"ORCA_PO_NPU_CHIP": "1951", "ORCA_PO_NPU_CORES": "3"},
])
def test_resolve_profile_mode_illegal_env_enums_rc2(tmp_path, extra):
    art = tmp_path / "ws"
    art.mkdir()
    proc = subprocess.run(["bash", str(_RESOLVE_SH)], capture_output=True,
                          text=True, timeout=60, env=_resolve_env(art, **extra))
    assert proc.returncode == 2
    assert "FATAL" in proc.stderr


_NPU_SMI_TABLE = """+------------------------------------------------------------------+
| npu-smi 23.1.0                            Version: 23.1.0         |
+======================+===============+================================+
| NPU     Name         | Health       | Memory-Usage                   |
|======================+===============+================================|
| 0     {name}         | OK           | {mem}                          |
+======================+===============+================================+
"""


def _hermetic_env(tmp_path: Path, *extra_dirs: Path) -> dict:
    """PATH holding ONLY the few tools resolve_profile_mode.sh needs
    (python3 + dirname symlinked from the parent env) plus the given stub
    dirs — npu-smi's presence/absence is then deterministic."""
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir(exist_ok=True)
    for name, target in (("python3", sys.executable),
                         ("dirname", shutil.which("dirname"))):
        link = stub_dir / name
        if not link.exists() and target:
            link.symlink_to(target)
    env = _resolve_env(None)
    env["PATH"] = ":".join([str(d) for d in extra_dirs] + [str(stub_dir)])
    return env


def _with_npu_smi_stub(tmp_path: Path, name: str, mem: str) -> dict:
    npu_dir = tmp_path / "npu-stub"
    npu_dir.mkdir(exist_ok=True)
    # printf is a bash builtin: the stub needs NOTHING from PATH
    (npu_dir / "npu-smi").write_text(
        f"#!{_BASH}\nprintf '%s' '{_NPU_SMI_TABLE.format(name=name, mem=mem)}'\n",
        encoding="utf-8")
    (npu_dir / "npu-smi").chmod(0o755)
    return _hermetic_env(tmp_path, npu_dir)


def test_resolve_profile_mode_npu_smi_model_column_hit(tmp_path):
    env = _with_npu_smi_stub(tmp_path, "6613", "0 / 32768 MB")
    proc = subprocess.run([_BASH, str(_RESOLVE_SH), "--stdout-only"],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"mode": "mfu", "chip": "6613",
                                       "precision": "INT8", "core_num": 1,
                                       "resolved_by": "npu-smi"}


def test_resolve_profile_mode_npu_smi_memory_false_positive_rc2(tmp_path):
    """"1951 MB" in a MEMORY column must never be read as the chip model —
    the parse is column-aware, so an unrecognized Name column exits 2 with
    guidance instead of guessing."""
    env = _with_npu_smi_stub(tmp_path, "910B3", "123 / 1951 MB")
    proc = subprocess.run([_BASH, str(_RESOLVE_SH), "--stdout-only"],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 2
    assert "ORCA_PO_NPU_CHIP" in proc.stderr


def test_resolve_profile_mode_env_beats_npu_smi(tmp_path):
    env = _with_npu_smi_stub(tmp_path, "6613", "0 / 32768 MB")
    env["ORCA_PO_NPU_CHIP"] = "1951"
    proc = subprocess.run([_BASH, str(_RESOLVE_SH), "--stdout-only"],
                          capture_output=True, text=True, timeout=60, env=env)
    assert json.loads(proc.stdout)["chip"] == "1951"
    assert json.loads(proc.stdout)["resolved_by"] == "env"


def test_resolve_profile_mode_fallback_when_no_env_no_tool(tmp_path):
    env = _hermetic_env(tmp_path)      # python3 only: no npu-smi anywhere
    proc = subprocess.run([_BASH, str(_RESOLVE_SH), "--stdout-only"],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"mode": "placeholder", "chip": "",
                                       "precision": None, "core_num": None,
                                       "resolved_by": "fallback"}


def test_resolve_profile_mode_stdout_only_never_writes(tmp_path):
    art = tmp_path / "ws"
    art.mkdir()
    env = _hermetic_env(tmp_path)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    proc = subprocess.run([_BASH, str(_RESOLVE_SH), "--stdout-only"],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 0
    assert not (art / "profile_mode.json").exists()   # read-only resolution


# ── rules_pool ────────────────────────────────────────────────────────────────

_RULES_SH = _SCRIPTS / "rules_pool.py"


def _rules_env(art: Path, project_root: Path, orca_home: Path) -> dict:
    env = dict(os.environ)
    env["ORCA_HOME"] = str(orca_home)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    return env


def _rule(**over) -> dict:
    base = {"id": "rule-0001", "change_pattern": "reduce_layers>=2",
            "statement": "降层 ≥2 精度崩", "direction": "harmful",
            "generality": "model_specific", "evidence_rounds": [3],
            "vids": ["r3-01"], "confidence": "low", "metric_gap": 0.61}
    base.update(over)
    return base


def _ws_with_model(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    """(artifacts, project_root, orca_home, model_hash) with a shadow
    closure anchored by BASELINE.lock."""
    import hashlib as _h
    art = tmp_path / "ws"
    (art / "shadow" / "pkg").mkdir(parents=True)
    (art / "shadow" / "pkg" / "model.py").write_text("# m\n", encoding="utf-8")
    mapping = {"pkg/model.py": _h.sha256(b"# m\n").hexdigest()}
    (art / "BASELINE.lock").write_text(json.dumps(
        {"model_path": "model.py", "pretrained_ckpt": "", "ckpt_sha256": "",
         "py_files_sha256": mapping}), encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    home = tmp_path / "orca-home"
    home.mkdir()
    h = _h.sha256()
    for rel in sorted(mapping):
        h.update(rel.encode())
        h.update(mapping[rel].encode())
    return art, proj, home, h.hexdigest()


def _run_rules(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_RULES_SH), *args],
                          capture_output=True, text=True, timeout=60, env=env)


def test_rules_pool_check_valid_and_borrowed_tolerance(tmp_path):
    art, proj, home, _ = _ws_with_model(tmp_path)
    (art / "accuracy_rules.json").write_text(json.dumps(
        {"rules": [_rule(borrowed=True)]}), encoding="utf-8")
    proc = _run_rules(_rules_env(art, proj, home), "check", "--artifacts", str(art))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"rules": 1, "errors": 0}


@pytest.mark.parametrize("bad,frag", [
    ("missing_field", "missing field"),
    ("dup_pattern", "duplicate change_pattern"),
    ("bad_direction", "direction must be one of"),
    ("bad_generality", "generality must be one of"),
    ("bad_confidence", "confidence must be one of"),
    ("bad_gap", "metric_gap must be a finite number"),
    ("not_object", "not a JSON object"),
])
def test_rules_pool_check_fail_loud_matrix(tmp_path, bad, frag):
    art, proj, home, _ = _ws_with_model(tmp_path)
    rules = [_rule()]
    if bad == "missing_field":
        rules = [{k: v for k, v in _rule().items() if k != "statement"}]
    elif bad == "dup_pattern":
        rules = [_rule(), _rule(id="rule-0002", evidence_rounds=[5],
                                vids=["r5-02"])]
    elif bad == "bad_direction":
        rules = [_rule(direction="maybe")]
    elif bad == "bad_generality":
        rules = [_rule(generality="universal")]
    elif bad == "bad_confidence":
        rules = [_rule(confidence="certain")]
    elif bad == "bad_gap":
        rules = [_rule(metric_gap="0.6")]
    elif bad == "not_object":
        rules = ["not an object"]
    (art / "accuracy_rules.json").write_text(json.dumps({"rules": rules}),
                                             encoding="utf-8")
    proc = _run_rules(_rules_env(art, proj, home), "check", "--artifacts", str(art))
    assert proc.returncode == 2
    assert frag in proc.stderr
    assert "rule #1" in proc.stderr          # the failing row is named


def test_rules_pool_check_missing_file_rc2(tmp_path):
    art, proj, home, _ = _ws_with_model(tmp_path)
    proc = _run_rules(_rules_env(art, proj, home), "check", "--artifacts", str(art))
    assert proc.returncode == 2
    assert "missing" in proc.stderr


def _pool_entry(pattern, *, direction="harmful", evidence=(), general=False,
                quarantined=False, generality="model_specific",
                confirms=(), refutes=()):
    return {"change_pattern": pattern, "direction": direction,
            "statement": f"pool lesson: {pattern}", "generality": generality,
            "evidence": [{"model_hash": h, "rounds": list(r), "vids": list(v)}
                         for h, r, v in evidence],
            "confirm_models": list(confirms), "refute_models": list(refutes),
            "general": general, "quarantined": quarantined}


def test_rules_pool_seed_four_sources(tmp_path):
    art, proj, home, mh = _ws_with_model(tmp_path)
    # source 1: project mirror
    (proj / "docs" / "prof-opt").mkdir(parents=True)
    (proj / "docs" / "prof-opt" / "accuracy_rules.json").write_text(json.dumps(
        {"rules": [_rule(id="rule-0001", change_pattern="mirror:own",
                         evidence_rounds=[2], vids=["r2-01"])]}),
        encoding="utf-8")
    # source 2: model_hash exact match; source 3: general; source 4:
    # plausibly_general; plus one quarantined (never seeded)
    (home / "prof-opt").mkdir(parents=True)
    (home / "prof-opt" / "accuracy_rules_pool.json").write_text(json.dumps(
        {"entries": [
            _pool_entry("exact:model", evidence=[(mh, [4], ["r4-01"])]),
            _pool_entry("general:one", evidence=[("aa", [1], ["a1"]),
                                                 ("bb", [2], ["b1"])],
                        general=True, confirms=["aa", "bb"]),
            _pool_entry("plausibly:one", generality="plausibly_general",
                        evidence=[("cc", [7], ["c1"])]),
            _pool_entry("quarantined:one", quarantined=True),
        ]}), encoding="utf-8")

    proc = _run_rules(_rules_env(art, proj, home), "seed",
                      "--artifacts", str(art), "--project-root", str(proj))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["rules"] == 4
    assert out["mirror"] == 1 and out["pool_exact"] == 1
    assert out["pool_general"] == 1 and out["pool_borrowed"] == 1

    rules = {r["change_pattern"]: r for r in json.loads(
        (art / "accuracy_rules.json").read_text(encoding="utf-8"))["rules"]}
    assert set(rules) == {"mirror:own", "exact:model", "general:one",
                          "plausibly:one"}       # quarantined never seeded
    # source 1 verbatim
    assert rules["mirror:own"]["id"] == "rule-0001"
    # source 3: pool- prefix, gap 0.0 + disclosure, evidence union, no
    # downgrade, not borrowed
    g = rules["general:one"]
    assert g["id"].startswith("pool-")
    assert g["metric_gap"] == 0.0
    assert "(general pool entry: no local measured gap)" in g["statement"]
    assert g["evidence_rounds"] == [1, 2] and g["vids"] == ["a1", "b1"]
    assert "borrowed" not in g
    assert g["confidence"] == "medium"        # 2 evidence rounds, no downgrade
    # source 4: borrowed + pool- prefix (1 round -> low, floor holds)
    p = rules["plausibly:one"]
    assert p["borrowed"] is True and p["confidence"] == "low"
    assert p["id"].startswith("pool-")
    # source 2: this model's own evidence, pool- prefix, disclosure
    e = rules["exact:model"]
    assert e["evidence_rounds"] == [4] and e["vids"] == ["r4-01"]
    assert "borrowed" not in e
    assert e["metric_gap"] == 0.0
    # the seeded file itself passes check
    check = _run_rules(_rules_env(art, proj, home), "check",
                       "--artifacts", str(art))
    assert check.returncode == 0


def test_rules_pool_seed_conflict_project_measured_wins(tmp_path):
    art, proj, home, mh = _ws_with_model(tmp_path)
    (proj / "docs" / "prof-opt").mkdir(parents=True)
    (proj / "docs" / "prof-opt" / "accuracy_rules.json").write_text(json.dumps(
        {"rules": [_rule(change_pattern="same:pattern", direction="harmful",
                         statement="measured here")]}), encoding="utf-8")
    (home / "prof-opt").mkdir(parents=True)
    (home / "prof-opt" / "accuracy_rules_pool.json").write_text(json.dumps(
        {"entries": [
            # same pattern, OPPOSITE direction, borrowed-class source
            _pool_entry("same:pattern", direction="benign",
                        generality="plausibly_general"),
        ]}), encoding="utf-8")
    proc = _run_rules(_rules_env(art, proj, home), "seed",
                      "--artifacts", str(art), "--project-root", str(proj))
    assert proc.returncode == 0, proc.stderr
    rules = json.loads((art / "accuracy_rules.json")
                       .read_text(encoding="utf-8"))["rules"]
    assert len(rules) == 1
    assert rules[0]["direction"] == "harmful"          # project measured wins
    assert "conflicting observation dropped" in rules[0]["statement"]


def test_rules_pool_seed_refuses_existing_workspace_rules(tmp_path):
    """REUSE never re-seeds: the guard lives IN the script, not only in the
    node prompt (prompt-level guards are advisory; this one is mechanical)."""
    art, proj, home, _ = _ws_with_model(tmp_path)
    (art / "accuracy_rules.json").write_text(json.dumps({"rules": []}),
                                             encoding="utf-8")
    proc = _run_rules(_rules_env(art, proj, home), "seed",
                      "--artifacts", str(art), "--project-root", str(proj))
    assert proc.returncode == 2
    assert "refused" in proc.stderr


def test_rules_pool_seed_missing_sources_cold_start(tmp_path):
    art, proj, home, _ = _ws_with_model(tmp_path)
    proc = _run_rules(_rules_env(art, proj, home), "seed",
                      "--artifacts", str(art), "--project-root", str(proj))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["rules"] == 0
    assert json.loads((art / "accuracy_rules.json")
                      .read_text(encoding="utf-8")) == {"rules": []}
    assert "mirror missing" in proc.stderr and "pool missing" in proc.stderr


def test_rules_pool_seed_drops_bad_mirror_lines(tmp_path):
    art, proj, home, _ = _ws_with_model(tmp_path)
    (proj / "docs" / "prof-opt").mkdir(parents=True)
    (proj / "docs" / "prof-opt" / "accuracy_rules.json").write_text(json.dumps(
        {"rules": [_rule(), {"id": "broken"}]}), encoding="utf-8")
    proc = _run_rules(_rules_env(art, proj, home), "seed",
                      "--artifacts", str(art), "--project-root", str(proj))
    assert proc.returncode == 0, proc.stderr
    rules = json.loads((art / "accuracy_rules.json")
                       .read_text(encoding="utf-8"))["rules"]
    assert len(rules) == 1                      # bad row dropped
    assert "dropped bad rule row" in proc.stderr


def test_rules_pool_seed_model_hash_lock_equals_shadow(tmp_path):
    """The pool key anchors the ORIGINAL closure: lock-based and shadow-based
    hashes agree on the same tree (first-run seed timing equivalence)."""
    import rules_pool  # noqa: E402
    art, proj, home, mh = _ws_with_model(tmp_path)
    assert rules_pool.compute_model_hash(art) == mh
    # lock absent -> shadow closure direct (identical tree, same value)
    (art / "BASELINE.lock").unlink()
    assert rules_pool.compute_model_hash(art) == mh


def test_rules_pool_merge_set_semantics_and_idempotency(tmp_path):
    art, proj, home, mh = _ws_with_model(tmp_path)
    (art / "accuracy_rules.json").write_text(json.dumps(
        {"rules": [_rule(evidence_rounds=[3], vids=["r3-01"])]}),
        encoding="utf-8")
    env = _rules_env(art, proj, home)

    first = _run_rules(env, "merge", "--artifacts", str(art),
                       "--project-root", str(proj))
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["confirm_added"] == 1   # founder counts

    # at-least-once re-merge: same model, same evidence -> NO set growth
    second = _run_rules(env, "merge", "--artifacts", str(art),
                        "--project-root", str(proj))
    out2 = json.loads(second.stdout)
    assert out2["confirm_added"] == 0 and out2["refute_added"] == 0

    # a DIFFERENT model's merge on the same pattern -> confirm + general
    other_ws = tmp_path / "other-ws"
    (other_ws / "shadow" / "pkg").mkdir(parents=True)
    (other_ws / "shadow" / "pkg" / "model.py").write_text("# other\n",
                                                          encoding="utf-8")
    (other_ws / "accuracy_rules.json").write_text(json.dumps(
        {"rules": [_rule(evidence_rounds=[4], vids=["r4-01"])]}),
        encoding="utf-8")
    third = _run_rules(_rules_env(other_ws, proj, home), "merge",
                       "--artifacts", str(other_ws), "--project-root", str(proj))
    assert third.returncode == 0, third.stderr
    entry = json.loads((home / "prof-opt" / "accuracy_rules_pool.json")
                       .read_text(encoding="utf-8"))["entries"][0]
    assert entry["general"] is True                # |confirm_models| == 2
    assert set(entry["confirm_models"]) == {mh, entry["confirm_models"][1]}

    # mirror holds the machine-readable project truth — full overwrite per
    # merge, so it reflects the LATEST merged workspace's rules
    mirror = json.loads((proj / "docs" / "prof-opt" / "accuracy_rules.json")
                        .read_text(encoding="utf-8"))
    assert mirror == {"rules": [_rule(evidence_rounds=[4], vids=["r4-01"])]}


def test_rules_pool_merge_refute_two_models_quarantines(tmp_path):
    art, proj, home, mh = _ws_with_model(tmp_path)
    (art / "accuracy_rules.json").write_text(json.dumps(
        {"rules": [_rule(direction="harmful")]}), encoding="utf-8")
    env = _rules_env(art, proj, home)
    assert _run_rules(env, "merge", "--artifacts", str(art),
                      "--project-root", str(proj)).returncode == 0

    # the same PATTERN observed BENIGN by two OTHER models -> refuted twice
    for tag in ("b1", "b2"):
        ws = tmp_path / f"refute-{tag}"
        (ws / "shadow" / "pkg").mkdir(parents=True)
        (ws / "shadow" / "pkg" / "model.py").write_text(f"# {tag}\n",
                                                        encoding="utf-8")
        (ws / "accuracy_rules.json").write_text(json.dumps(
            {"rules": [_rule(direction="benign", metric_gap=0.01)]}),
            encoding="utf-8")
        proc = _run_rules(_rules_env(ws, proj, home), "merge",
                          "--artifacts", str(ws), "--project-root", str(proj))
        assert proc.returncode == 0, proc.stderr

    entries = json.loads((home / "prof-opt" / "accuracy_rules_pool.json")
                         .read_text(encoding="utf-8"))["entries"]
    by_key = {(e["change_pattern"], e["direction"]): e for e in entries}
    harmful = by_key[("reduce_layers>=2", "harmful")]
    benign = by_key[("reduce_layers>=2", "benign")]
    assert len(harmful["refute_models"]) == 2
    assert harmful["quarantined"] is True        # |refute_models| >= 2
    assert benign["confirm_models"] and not benign["quarantined"]


def test_rules_pool_merge_without_workspace_rules_skips(tmp_path):
    art, proj, home, _ = _ws_with_model(tmp_path)
    (proj / "docs" / "prof-opt").mkdir(parents=True)
    (proj / "docs" / "prof-opt" / "accuracy_rules.json").write_text(
        '{"sentinel": "do-not-touch"}', encoding="utf-8")
    proc = _run_rules(_rules_env(art, proj, home), "merge",
                      "--artifacts", str(art), "--project-root", str(proj))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["merged"] == 0
    # an early-failure run must not wipe the accumulated mirror
    assert (proj / "docs" / "prof-opt" / "accuracy_rules.json").read_text(
        encoding="utf-8") == '{"sentinel": "do-not-touch"}'
    assert "workspace rules missing" in proc.stderr
