"""test_po_v6.py — mechanism tests (single-variant convergence loop).

Script-level unit tests kept current with the v7 surface: the
training-device resolver (env override -> probe chain first-match-wins,
write-if-absent, reuse-mismatch fail-loud, NO_REUSE overwrite), the
device allocation ledger (probe raw passthrough / O_EXCL claim --idx /
adopt with pid_lib liveness / idempotent release / locked {"ok": false}),
round_state working = current + 1, the gate decision order (success ->
report / round cap -> report / idle streak -> report / loop, with
in_flight), history append_terminal row semantics + the (vid, change_sig)
dedup key with probe_insufficient permanently consumed, the latency
  recheck boundary (strictly below incumbent passes), the ledger aggregator's
purity (same shard set -> same output, full rebuildability), and the
deployed-set stamp roundtrip.

The watchdog face (watch_variant.py): warmup is never judged, the
over-budget streak counts once per NEW epoch and fires at the
E-derived threshold max(2, ceil(0.3 x E)) (a terminal replay never
re-kills), the early-stop kill refuses a pid that fails the /proc
attribution check, natural completion runs the final-budget verdict
(success / accuracy_fail) with the last-known epoch/metric/gap preserved
while the baseline anchor is pending, a crash without rc is a terminal
failure, and re-entry is idempotent in all states. Every curve is a mock
log file — nothing trains, no GPU/NPU is ever required.

The closing section carries the scenario smokes: the single-variant
convergence loop (one vid repairing in place), the two-card
parallel/block/release ledger sequence, the streaming early-stop
end-to-end (curve growth -> kill -> terminal -> derived dashboard), and
the gate's report exits (success with an in-flight survivor + the round
cap awaiting in-flight terminals).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workflows" / "prof-opt" / "agents" / "_po_scripts"
sys.path.insert(0, str(_SCRIPTS))

import history_lib  # noqa: E402
from gate_decide import decide  # noqa: E402

_BASH = shutil.which("bash") or "/bin/bash"
_RECHECK_SH = (_REPO / "workflows" / "prof-opt" / "agents" / "po_propose"
               / "scripts" / "run_latency_recheck.sh")
_RESOLVE_SH = _SCRIPTS / "resolve_train_device.sh"
_ALLOC_PY = _SCRIPTS / "device_alloc.py"
_LEDGER_AGG = _SCRIPTS / "ledger_aggregate.py"
_LEDGER_PY = _SCRIPTS / "experiment_ledger.py"
_DEPLOY_SH = _SCRIPTS / "deploy_scripts.sh"
_MFU_REPORT = ("[subagent:mfu-analyzer v2 MBA7K2]\n"
               "## MFU latency bottleneck report\n"
               "### Source files\n- model/schedule_result.json\n")


def _run_cli(args: list[str], env: dict | None = None,
             timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=timeout, env=env)


def _write_anchor(artifacts: Path, *, target: int = 501,
                  budget: float = 0.1) -> Path:
    anchor = artifacts / "base" / "origin_anchor.json"
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text(json.dumps({
        "baseline_makespan_cycles": 1000,
        "latency_reduction_min": 0.5, "accuracy_budget": budget,
        "target_cycles": target, "frozen_at_round": 0}), encoding="utf-8")
    return anchor


def _write_raw_profile(profile_dir: Path, makespan: int) -> None:
    raw_dir = profile_dir / "model"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "schedule_result.json").write_text(json.dumps({
        "parallel_cycles": makespan,
    }), encoding="utf-8")
    (profile_dir / "mfu_bottleneck_report.md").write_text(
        _MFU_REPORT, encoding="utf-8")


def _write_train_device(artifacts: Path, backend: str = "cuda",
                        count: int = 2) -> Path:
    artifacts.mkdir(parents=True, exist_ok=True)
    path = artifacts / "train_device.json"
    path.write_text(json.dumps({"backend": backend, "device_count": count,
                                "resolved_by": "test"}), encoding="utf-8")
    return path


def _write_lock(artifacts: Path, idx: int, vid: str, pid: int) -> Path:
    path = artifacts / "devices" / f"{idx}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"vid": vid, "pid": pid,
                                "acquired_at": "2026-08-31T00:00:00+00:00",
                                "backend": "cuda"}) + "\n", encoding="utf-8")
    return path


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _alloc(artifacts: Path, *args: str,
           env: dict | None = None) -> subprocess.CompletedProcess:
    # the subcommand owns --artifacts, so it comes first
    return _run_cli([sys.executable, str(_ALLOC_PY), *args,
                     "--artifacts", str(artifacts)], env=env)



_NVIDIA_SMI_STUB = """#!{bash}
case "$1" in
  -L) printf 'GPU 0: stub\\nGPU 1: stub\\n' ;;
  --query-gpu=*) printf '0, GPU-aa\\n1, GPU-bb\\n' ;;
  --query-compute-apps=*) printf 'GPU-bb, 4242\\n' ;;   # GPU 1 busy-real
  *) exit 9 ;;
esac
"""

_NVIDIA_SMI_IDLE_STUB = """#!{bash}
case "$1" in
  -L) printf 'GPU 0: stub\\nGPU 1: stub\\n' ;;
  --query-gpu=*) printf '0, GPU-aa\\n1, GPU-bb\\n' ;;
  --query-compute-apps=*) : ;;
  *) exit 9 ;;
esac
"""


def _stub_env(tmp_path: Path, *, tools: dict[str, str],
              backend: str | None = None,
              real_tools: str = "") -> dict:
    """PATH holding ONLY the stub tools (plus the essentials bash needs)
    — a stub's presence/absence is then deterministic. `real_tools` names
    space-separated host tools to symlink in (their real binaries), for
    stubs that need to shell out. An optional backend env override rides
    along."""
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    for name, body in tools.items():
        (stub_dir / name).write_text(body, encoding="utf-8")
        (stub_dir / name).chmod(0o755)
    for name in real_tools.split():
        target = shutil.which(name)
        if target:
            link = stub_dir / name
            if not link.exists():
                link.symlink_to(target)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ORCA_PO_")}
    env["PATH"] = ":".join([str(stub_dir),
                            "/usr/bin", "/bin", "/usr/local/bin"])
    if backend:
        env["ORCA_PO_DEVICE_BACKEND"] = backend
    return env


# ── device_alloc v7: probe passthrough / claim --idx / adopt / release ───────
# (the fuller probe/fail-loud matrix lives in test_po_v7.py)

def test_device_alloc_claim_idx_is_exclusive(tmp_path):
    art = tmp_path / "ws"
    _write_train_device(art, count=2)
    first = json.loads(_alloc(art, "claim", "--vid", "r1-01", "--idx", "0").stdout)
    assert first["ok"] is True and first["idx"] == 0
    lock = json.loads((art / "devices" / "0.lock").read_text(encoding="utf-8"))
    assert lock["vid"] == "r1-01" and lock["backend"] == "cuda"
    assert isinstance(lock["pid"], int) and "acquired_at" in lock

    # the SAME idx again -> ok:false naming the holder (rc 0: park, re-probe)
    second = _alloc(art, "claim", "--vid", "r2-01", "--idx", "0")
    assert second.returncode == 0, second.stderr
    payload = json.loads(second.stdout)
    assert payload["ok"] is False
    assert "device 0 locked by vid=r1-01" in payload["reason"]

    # a DIFFERENT idx is fine
    third = json.loads(_alloc(art, "claim", "--vid", "r2-01", "--idx", "1").stdout)
    assert third["ok"] is True and third["idx"] == 1


def test_device_alloc_claim_out_of_range_fails_loud(tmp_path):
    art = tmp_path / "ws"
    _write_train_device(art, count=2)
    for bad in ("2", "-1"):
        proc = _alloc(art, "claim", "--vid", "r1-01", "--idx", bad)
        assert proc.returncode == 2, proc.stderr
        assert "outside" in proc.stderr


def test_device_alloc_claim_dead_owner_lock_still_blocks(tmp_path):
    """v7 division of labor: claim NEVER recycles — even a dead-pid lock
    blocks its idx (recycling is release/report-sweep business, by explicit
    decision, never a side effect of claiming)."""
    art = tmp_path / "ws"
    _write_train_device(art, count=2)
    _write_lock(art, 0, "r0-99", _dead_pid())
    out = json.loads(_alloc(art, "claim", "--vid", "r1-01", "--idx", "0").stdout)
    assert out["ok"] is False and "r0-99" in out["reason"]
    assert (art / "devices" / "0.lock").is_file()   # untouched by claim


def test_device_alloc_release_is_idempotent(tmp_path):
    art = tmp_path / "ws"
    _write_train_device(art, count=1)
    _write_lock(art, 0, "r1-01", os.getpid())
    first = _alloc(art, "release", "--idx", "0")
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout) == {"released": True, "idx": 0}
    assert not (art / "devices" / "0.lock").exists()

    again = _alloc(art, "release", "--idx", "0")
    assert again.returncode == 0, again.stderr       # no double release
    payload = json.loads(again.stdout)
    assert payload["released"] is False and payload["idx"] == 0


def test_device_alloc_missing_train_device_fails_loud(tmp_path):
    proc = _alloc(tmp_path / "ws", "claim", "--vid", "r1-01", "--idx", "0")
    assert proc.returncode == 2
    assert "train_device.json missing" in proc.stderr


# ── resolve_train_device: four-level resolution + write-if-absent + reuse ─────

def _resolve(env: dict, *args: str) -> subprocess.CompletedProcess:
    return _run_cli([_BASH, str(_RESOLVE_SH), *args], env=env)


def test_resolve_train_device_env_wins_and_bad_enum_fails(tmp_path):
    art = tmp_path / "ws"
    art.mkdir()
    env = _stub_env(tmp_path, tools={
        "nvidia-smi": _NVIDIA_SMI_STUB.format(bash=_BASH)})
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    env["ORCA_PO_DEVICE_BACKEND"] = "cuda"
    proc = _resolve(env)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"backend": "cuda", "device_count": 2,
                                       "resolved_by": "env"}
    assert json.loads((art / "train_device.json").read_text(
        encoding="utf-8")) == json.loads(proc.stdout)

    env["ORCA_PO_DEVICE_BACKEND"] = "tpu"
    bad = _resolve(env)
    assert bad.returncode == 2 and "ORCA_PO_DEVICE_BACKEND" in bad.stderr


def test_resolve_train_device_npu_smi_level_and_count_failure(tmp_path):
    env = _stub_env(tmp_path, tools={
        "npu-smi": f"#!{_BASH}\nprintf 'Total Count : 4\\n'\n"})
    proc = _resolve(env, "--stdout-only")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"backend": "npu", "device_count": 4,
                                       "resolved_by": "npu-smi"}

    # npu-smi present but -l yields nothing countable -> fail loud
    env_broken = _stub_env(tmp_path / "b", tools={
        "npu-smi": f"#!{_BASH}\necho 'no count here' >&2; exit 1\n"})
    broken = _resolve(env_broken, "--stdout-only")
    assert broken.returncode == 2 and "device_count" in broken.stderr


def test_resolve_train_device_env_backend_without_counter_fails(tmp_path):
    """An env-declared backend is trusted for the ENUM only — its count still
    comes from the backend's own counter, and a missing counter is a hard
    error (never a guessed device_count)."""
    env = _stub_env(tmp_path, tools={})      # ORCA_PO_DEVICE_BACKEND=npu, no npu-smi
    env["ORCA_PO_DEVICE_BACKEND"] = "npu"
    proc = _resolve(env, "--stdout-only")
    assert proc.returncode == 2
    assert "device_count" in proc.stderr and "npu-smi -l" in proc.stderr


def test_resolve_train_device_nvidia_smi_level(tmp_path):
    env = _stub_env(tmp_path, tools={
        "nvidia-smi": _NVIDIA_SMI_STUB.format(bash=_BASH)})
    proc = _resolve(env, "--stdout-only")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"backend": "cuda", "device_count": 2,
                                       "resolved_by": "nvidia-smi"}


def test_resolve_train_device_torch_cuda_level(tmp_path):
    # no smi tools; python3 is a stub whose rc 0 fakes
    # torch.cuda.is_available() and whose stdout fakes device_count() == 1
    env = _stub_env(tmp_path, tools={
        "python3": "#!/bin/bash\nprintf '1\\n'\nexit 0\n"},
        real_tools="dirname grep sed head")
    proc = _resolve(env, "--stdout-only")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"backend": "cuda", "device_count": 1,
                                       "resolved_by": "torch.cuda"}


def test_resolve_train_device_no_backend_is_a_hard_error(tmp_path):
    env = _stub_env(tmp_path, tools={
        "python3": "#!/bin/bash\nexit 1\n"},   # torch check fails
        real_tools="dirname grep sed head")
    proc = _resolve(env, "--stdout-only")
    assert proc.returncode == 2
    assert "no trainable device backend" in proc.stderr


def test_resolve_train_device_write_if_absent_and_reuse_mismatch(tmp_path):
    art = tmp_path / "ws"
    art.mkdir()
    env = _stub_env(tmp_path, tools={
        "nvidia-smi": _NVIDIA_SMI_STUB.format(bash=_BASH)})
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    first = _resolve(env)
    frozen = (art / "train_device.json").read_text(encoding="utf-8")

    # write-if-absent: a second plain run keeps the frozen file byte-for-byte
    second = _resolve(env)
    assert second.returncode == 0
    assert (art / "train_device.json").read_text(encoding="utf-8") == frozen

    # matching re-resolution passes read-only
    ok = _resolve(env, "--stdout-only")
    assert ok.returncode == 0 and json.loads(ok.stdout)["backend"] == "cuda"

    # drift (backend changed under the workspace) -> fail loud + fresh_start
    drifted = json.loads(frozen)
    drifted["backend"] = "npu"
    (art / "train_device.json").write_text(json.dumps(drifted),
                                           encoding="utf-8")
    mismatch = _resolve(env, "--stdout-only")
    assert mismatch.returncode == 2
    assert "fresh_start" in mismatch.stderr
    # and the read-only path never rewrote the frozen file
    assert json.loads((art / "train_device.json").read_text(
        encoding="utf-8"))["backend"] == "npu"


# ── round_state: working = current + 1 ────────────────────────────────────────

def _round_state(artifacts: Path, command: str) -> subprocess.CompletedProcess:
    return _run_cli([sys.executable, str(_SCRIPTS / "round_state.py"),
                     "--artifacts", str(artifacts), command])


def test_round_state_working_is_current_plus_one(tmp_path):
    art = tmp_path / "ws"
    assert json.loads(_round_state(art, "working").stdout) == \
        {"round": 1, "round_dir": "rounds/001"}   # empty workspace

    for name in ("001", "003", "junk"):
        (art / "rounds" / name).mkdir(parents=True)
    assert json.loads(_round_state(art, "current").stdout)["round"] == 3
    assert json.loads(_round_state(art, "working").stdout) == \
        {"round": 4, "round_dir": "rounds/004"}

    # a leftover v5 .round_advanced marker is ignored (linkage retired)
    (art / ".round_advanced").write_text(
        json.dumps({"round": 3, "mode": "latency"}), encoding="utf-8")
    assert json.loads(_round_state(art, "working").stdout)["round"] == 4


def test_round_state_mode_command_is_gone(tmp_path):
    proc = _round_state(tmp_path / "ws", "mode")
    assert proc.returncode != 0           # argparse choices fail loud


# ── gate_decide: the v6 three-branch decision order ───────────────────────────

def _gate_ws(tmp_path: Path, rounds: list[int],
             history_rows: list[dict]) -> Path:
    art = tmp_path / "ws"
    for rnd in rounds:
        (art / "rounds" / f"{rnd:03d}").mkdir(parents=True)
    _write_anchor(art)
    hist = art / "history.jsonl"
    hist.parent.mkdir(parents=True, exist_ok=True)
    hist.write_text("".join(json.dumps(r) + "\n" for r in history_rows),
                    encoding="utf-8")
    return art


def test_gate_success_row_routes_report_even_at_cap(tmp_path):
    rows = [
        {"vid": "r1-01", "round": 1, "outcome": "latency_improved",
         "makespan_cycles": 450},
        {"vid": "r1-01", "round": 1, "outcome": "success",
         "makespan_cycles": 450, "gap": 0.02, "stopped_at_epoch": 10,
         "final_acc": 0.91},
        {"vid": "r2-01", "round": 2, "outcome": "latency_improved",
         "makespan_cycles": 460},
    ]
    art = _gate_ws(tmp_path, rounds=[1, 2], history_rows=rows)
    out = decide(art, max_rounds=1)        # cap reached AND success present
    assert out["decision"] == "report"     # branch 1 wins over the cap
    assert out["success_vids"] == ["r1-01"]
    assert out["in_flight"] == ["r2-01"]   # passed but not terminal
    assert out["round"] == 2 and out["target_cycles"] == 501
    assert set(out) == {"decision", "round", "target_cycles",
                        "success_vids", "in_flight", "idle_rounds",
                        "incumbent_promoted", "reason"}


def test_gate_round_cap_routes_report_without_success(tmp_path):
    rows = [{"vid": "r1-01", "round": 1, "outcome": "latency_fail",
             "makespan_cycles": 800}]
    art = _gate_ws(tmp_path, rounds=[1, 2], history_rows=rows)
    out = decide(art, max_rounds=2)
    assert out["decision"] == "report"
    assert out["success_vids"] == [] and out["in_flight"] == []
    assert "hard cap" in out["reason"]

    # below the cap the very same workspace keeps looping
    assert decide(art, max_rounds=3)["decision"] == "loop"


def test_gate_loops_and_terminal_rows_clear_in_flight(tmp_path):
    rows = [
        {"vid": "r1-01", "round": 1, "outcome": "latency_improved",
         "makespan_cycles": 450},
        {"vid": "r1-02", "round": 1, "outcome": "latency_improved",
         "makespan_cycles": 460},
        {"vid": "r1-02", "round": 1, "outcome": "accuracy_fail",
         "gap": 0.5, "stopped_at_epoch": 6},
    ]
    art = _gate_ws(tmp_path, rounds=[1], history_rows=rows)
    out = decide(art, max_rounds=100)
    assert out["decision"] == "loop"
    assert out["in_flight"] == ["r1-01"]   # r1-02 reached a terminal row


def test_gate_terminal_rows_cover_all_four_v6_outcomes(tmp_path):
    outcomes = ["success", "accuracy_fail", "probe_insufficient", "latency_fail"]
    rows = [{"vid": f"r{i}-01", "round": 1, "outcome": "latency_improved"}
            for i in range(4)]
    rows += [{"vid": f"r{i}-01", "round": 1, "outcome": o,
              **({"makespan_cycles": 450} if o == "success" else {})}
             for i, o in enumerate(outcomes)]
    art = _gate_ws(tmp_path, rounds=[1], history_rows=rows)
    out = decide(art, max_rounds=100)
    assert out["in_flight"] == []
    assert out["decision"] == "report"     # the success row among them


def test_gate_missing_origin_anchor_still_fails_loud(tmp_path):
    art = tmp_path / "ws"
    (art / "rounds" / "001").mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="origin anchor"):
        decide(art, max_rounds=5)


# ── history_lib.append_terminal: row semantics + unchanged dedup key ─────────

def _impl(hist: Path, vid: str, sig: str, *, probe_epochs: int = 1) -> None:
    history_lib.append_implemented(
        hist, vid, round=1, seq=1, parent_vid=None, change_sig=sig,
        probe_epochs=probe_epochs, target_modules=["m"],
        predicted_delta_cycles=-100,
        base_at_proposal={"vid": None, "makespan_cycles": 1000})


def test_append_terminal_success_row_semantics(tmp_path):
    hist = tmp_path / "history.jsonl"
    _impl(hist, "r1-01", "activation:gelu->relu:m")
    history_lib.append_latency(hist, "r1-01", structural_check="pass",
                               makespan_cycles=450, latency_gate="pass",
                               pred_actual_ratio=1.0,
                               outcome="latency_improved")
    row = history_lib.append_terminal(
        hist, "r1-01", outcome="success", gap=0.02,
        stopped_at_epoch=10, final_acc=0.91)
    assert row["outcome"] == "success" and row["gap"] == 0.02
    assert row["stopped_at_epoch"] == 10 and row["final_acc"] == 0.91
    # full-snapshot merge: the terminal row still carries the vid's history
    assert row["makespan_cycles"] == 450 and row["change_sig"] is not None
    assert row["version"] == 3
    assert history_lib.read_latest(hist)["r1-01"]["outcome"] == "success"
    # the closed field set holds: only terminal/latency/impl/version/ts keys
    assert set(row) <= (set(history_lib.TERMINAL_FIELDS)
                        | set(history_lib.LATENCY_FIELDS)
                        | set(history_lib.IMPL_FIELDS) | {"version", "ts"})


def test_append_terminal_rejects_retired_outcomes_and_unknown_fields(tmp_path):
    hist = tmp_path / "history.jsonl"
    _impl(hist, "r1-01", "sig:m")
    with pytest.raises(history_lib.HistoryError, match="accuracy_pass"):
        history_lib.append_terminal(hist, "r1-01", outcome="accuracy_pass")
    with pytest.raises(history_lib.HistoryError, match="advanced"):
        history_lib.append_terminal(hist, "r1-01", outcome="advanced")
    with pytest.raises(TypeError):
        history_lib.append_terminal(hist, "r1-01", outcome="success",
                                    proxy_acc=0.9)   # retired field
    # nothing terminal was written: the vid's latest row is still the impl row
    assert "outcome" not in history_lib.read_latest(hist)["r1-01"]


def test_append_terminal_per_outcome_extras_written_only_when_passed(tmp_path):
    hist = tmp_path / "history.jsonl"
    _impl(hist, "r1-01", "a:m")
    row = history_lib.append_terminal(
        hist, "r1-01", outcome="probe_insufficient",
        stage="training", max_retries_hit=True)
    assert row["stage"] == "training" and row["max_retries_hit"] is True
    assert "final_acc" not in row and "stopped_at_epoch" not in row

    hist2 = tmp_path / "h2.jsonl"
    _impl(hist2, "r2-01", "b:m")
    row2 = history_lib.append_terminal(hist2, "r2-01", outcome="latency_fail",
                                       measured_makespan_cycles=800)
    assert row2["measured_makespan_cycles"] == 800 and "gap" not in row2


def test_append_terminal_keeps_the_vid_change_sig_dedup_key(tmp_path):
    """The dedup key is unchanged in v6: (vid, change_sig) full snapshots —
    a probe_insufficient sig stays same-config blocked, a config change
    reopens it, and latency_fail remains non-permanent."""
    hist = tmp_path / "history.jsonl"
    _impl(hist, "r1-01", "norm:relax:m", probe_epochs=1)
    history_lib.append_terminal(hist, "r1-01", outcome="probe_insufficient",
                                stage="liveness", max_retries_hit=True)
    assert history_lib.dedup_state(hist, "norm:relax:m", 1)["blocked"] is True
    assert history_lib.dedup_state(hist, "norm:relax:m", 2)["blocked"] is True

    hist2 = tmp_path / "h2.jsonl"
    _impl(hist2, "r1-01", "act:swap:m")
    history_lib.append_terminal(hist2, "r1-01", outcome="latency_fail",
                                measured_makespan_cycles=900)
    assert history_lib.dedup_state(hist2, "act:swap:m", 1)["blocked"] is False


# ── run_latency_recheck: strict current-incumbent improvement boundary ────────

def _recheck_ws(tmp_path: Path, *, target: int = 500) -> Path:
    """mfu-mode fixture: the recheck consumes each variant's four-piece
    makespan verbatim, so the two variants pin the gate boundary exactly."""
    pytest.importorskip("onnx")
    import onnx
    from onnx import TensorProto, helper
    art = tmp_path / "ws"
    (art / "scripts").mkdir(parents=True)
    for src in ("diff_check.py", "history_lib.py", "emit_result.py",
                "round_state.py", "check_verdict.py"):
        shutil.copy(_SCRIPTS / src, art / "scripts" / src)
    (art / "contracts.json").write_text(json.dumps(
        {"interpreter": {"sys_executable": sys.executable}}), encoding="utf-8")
    _write_raw_profile(art / "base" / "profile", 1000)
    _write_anchor(art, target=target)
    graph = helper.make_graph(
        [helper.make_node("MatMul", ["x", "w"], ["h"], name="mm"),
         helper.make_node("Add", ["h", "b"], ["y"], name="add")],
        "smoke",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 16])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, 16])],
        [helper.make_tensor("w", TensorProto.FLOAT, [16, 16],
                            vals=[0.0] * 256),
         helper.make_tensor("b", TensorProto.FLOAT, [16], vals=[0.0] * 16)])
    onnx.save(helper.make_model(graph,
                                opset_imports=[helper.make_opsetid("", 17)]),
              str(art / "base" / "model.onnx"))
    (art / "shadow" / "pkg").mkdir(parents=True)
    (art / "shadow" / "pkg" / "model.py").write_text("# base\n", encoding="utf-8")
    (art / "rounds" / "001").mkdir(parents=True)
    return art


def _recheck_variant(art: Path, vid: str, makespan: int) -> None:
    vd = art / "variants" / vid
    (vd / "onnx").mkdir(parents=True)
    shutil.copy(art / "base" / "model.onnx", vd / "onnx" / "model.onnx")
    _write_raw_profile(vd / "profile", makespan)
    (vd / "shadow" / "pkg").mkdir(parents=True)
    shutil.copy(art / "shadow" / "pkg" / "model.py",
                vd / "shadow" / "pkg" / "model.py")
    (vd / "declaration.json").write_text(json.dumps(
        {"vid": vid, "edited_files": [], "op_delta": {},
         "predicted_delta_cycles": -400}), encoding="utf-8")
    (vd / "DONE").write_text("", encoding="utf-8")
    _impl(art / "history.jsonl", vid, f"sig:{vid}")


def _run_recheck(art: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    return _run_cli([_BASH, str(_RECHECK_SH)], env=env, timeout=300)


def test_recheck_gate_uses_strict_incumbent_boundary(tmp_path):
    art = _recheck_ws(tmp_path, target=500)
    _recheck_variant(art, "r1-01", 999)   # one cycle faster than incumbent
    _recheck_variant(art, "r1-02", 1000)  # equality is not improvement
    proc = _run_recheck(art)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["status"] == "executed"
    assert out["target_cycles"] == 500
    assert out["latency_improved_count"] == 1
    assert out["summary"] == "2 verdicts [latency_improved=1 latency_fail=1]"

    improved = json.loads((art / "variants" / "r1-01" / "verdict.json")
                          .read_text(encoding="utf-8"))
    assert improved["outcome"] == "latency_improved"
    assert improved["latency_gate"] == "pass"
    assert improved["target_cycles"] == 500
    assert improved["makespan_cycles"] > improved["target_cycles"]
    equal = json.loads((art / "variants" / "r1-02" / "verdict.json")
                       .read_text(encoding="utf-8"))
    assert equal["outcome"] == "latency_fail" and equal["latency_gate"] == "fail"
    # best.json / mode no longer feed the gate: no mode field is emitted
    assert "gate_mode" not in out and "gate_mode" not in improved
    latest = history_lib.read_latest(art / "history.jsonl")
    assert latest["r1-01"]["outcome"] == "latency_improved"
    assert latest["r1-02"]["outcome"] == "latency_fail"


# ── ledger_aggregate: pure, deterministic, fully rebuildable ───────────────────

def _shard(artifacts: Path, rel: str, vid: str, **fields) -> Path:
    path = artifacts / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"vid": vid, "status": "training", "epoch": 3, "metric": 0.9,
               "gap": 0.02, "device": 0, "change_summary": f"change {vid}",
               "ts": "2026-08-31T00:00:00+00:00"}
    payload.update(fields)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def test_ledger_aggregate_is_pure_and_fully_rebuildable(tmp_path):
    art = tmp_path / "ws"
    _shard(art, "baseline/ledger_entry.json", "baseline", status="done",
           epoch=10)
    _shard(art, "variants/r2-01/ledger_entry.json", "r2-01")
    _shard(art, "variants/r1-01/ledger_entry.json", "r1-01")

    proc = _run_cli([sys.executable, str(_LEDGER_AGG),
                     "--artifacts", str(art)])
    assert proc.returncode == 0, proc.stderr
    first = (art / "experiment_ledger.json").read_text(encoding="utf-8")
    payload = json.loads(first)
    # deterministic order: baseline first, then variants by vid
    assert [r["vid"] for r in payload["rows"]] == ["baseline", "r1-01", "r2-01"]
    assert payload["variant_count"] == 2 and payload["schema_version"] == 2

    # full rebuild from the shards: delete the derived file, re-aggregate,
    # byte-identical output (same shard set -> same output, no wall clock)
    (art / "experiment_ledger.json").unlink()
    again = _run_cli([sys.executable, str(_LEDGER_AGG),
                      "--artifacts", str(art)])
    assert again.returncode == 0, again.stderr
    assert (art / "experiment_ledger.json").read_text(encoding="utf-8") == first

    # re-running over an existing file converges (idempotent replace)
    third = _run_cli([sys.executable, str(_LEDGER_AGG),
                      "--artifacts", str(art)])
    assert third.returncode == 0
    assert (art / "experiment_ledger.json").read_text(encoding="utf-8") == first


def test_ledger_aggregate_fails_loud_on_torn_shard(tmp_path):
    art = tmp_path / "ws"
    _shard(art, "variants/r1-01/ledger_entry.json", "r1-01")
    (art / "variants" / "r2-01").mkdir(parents=True)
    (art / "variants" / "r2-01" / "ledger_entry.json").write_text(
        "{torn", encoding="utf-8")
    proc = _run_cli([sys.executable, str(_LEDGER_AGG),
                     "--artifacts", str(art)])
    assert proc.returncode == 2
    assert "r2-01" in proc.stderr


def test_experiment_ledger_entry_aggregates_and_renders_summary(tmp_path):
    art = tmp_path / "ws"
    _shard(art, "variants/r1-01/ledger_entry.json", "r1-01")
    proc = _run_cli([sys.executable, str(_LEDGER_PY),
                     "--artifacts", str(art)])
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["variant_count"] == 1
    ledger = json.loads((art / "experiment_ledger.json")
                        .read_text(encoding="utf-8"))
    assert [r["vid"] for r in ledger["rows"]] == ["r1-01"]
    summary = (art / "experiment_summary.md").read_text(encoding="utf-8")
    assert "r1-01" in summary and "change r1-01" in summary


# ── verdict_decide final-budget: variants/<vid>/eval + anchor budget ──────────

def _final_ws(tmp_path: Path, *, final_acc: float, anchor_acc: float = 0.92,
              budget: float = 0.05,
              direction: str = "higher_better") -> Path:
    art = tmp_path / "ws"
    _write_anchor(art, budget=budget)
    record = {"vid": "r1-01", "final_acc": final_acc,
              "baseline_full_acc": anchor_acc, "metric_direction": direction,
              "within_budget": None}
    path = art / "variants" / "r1-01" / "eval" / "final_acc.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return art


def test_final_budget_computes_and_backfills_within_budget(tmp_path):
    art = _final_ws(tmp_path, final_acc=0.90)    # 0.90 >= 0.92 - 0.05
    proc = _run_cli([sys.executable, str(_SCRIPTS / "verdict_decide.py"),
                     "final-budget", "--artifacts", str(art),
                     "--vid", "r1-01"])
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"within_budget": True}
    record = json.loads((art / "variants" / "r1-01" / "eval" / "final_acc.json")
                        .read_text(encoding="utf-8"))
    assert record["within_budget"] is True      # §7.3: the null is backfilled

    # idempotent: a second call over the backfilled value is a clean no-op
    again = _run_cli([sys.executable, str(_SCRIPTS / "verdict_decide.py"),
                      "final-budget", "--artifacts", str(art),
                      "--vid", "r1-01"])
    assert again.returncode == 0 and json.loads(again.stdout)["within_budget"] is True

    # the budget boundary is inclusive: exactly ON the line passes
    on_line = _final_ws(tmp_path / "b", final_acc=0.87)   # 0.87 == 0.92 - 0.05
    proc_eq = _run_cli([sys.executable, str(_SCRIPTS / "verdict_decide.py"),
                        "final-budget", "--artifacts", str(on_line),
                        "--vid", "r1-01"])
    assert proc_eq.returncode == 0, proc_eq.stderr
    assert json.loads(proc_eq.stdout) == {"within_budget": True}


def test_final_budget_fail_side_and_disagreeing_record(tmp_path):
    art = _final_ws(tmp_path, final_acc=0.80)   # 0.80 < 0.92 - 0.05
    proc = _run_cli([sys.executable, str(_SCRIPTS / "verdict_decide.py"),
                     "final-budget", "--artifacts", str(art),
                     "--vid", "r1-01"])
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"within_budget": False}

    # a hand-edited non-null verdict that DISAGREES fails loud, never a
    # silent overwrite
    record_path = art / "variants" / "r1-01" / "eval" / "final_acc.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["within_budget"] = True
    record_path.write_text(json.dumps(record), encoding="utf-8")
    tampered = _run_cli([sys.executable, str(_SCRIPTS / "verdict_decide.py"),
                         "final-budget", "--artifacts", str(art),
                         "--vid", "r1-01"])
    assert tampered.returncode == 2
    assert "within_budget" in tampered.stderr


def test_final_budget_lower_better_direction(tmp_path):
    # lower_better passes at value <= anchor + budget (2.05): 2.10 fails,
    # 1.95 passes, and exactly 2.05 (ON the line) passes
    art = _final_ws(tmp_path, final_acc=2.10, anchor_acc=2.00,
                    direction="lower_better")
    proc = _run_cli([sys.executable, str(_SCRIPTS / "verdict_decide.py"),
                     "final-budget", "--artifacts", str(art),
                     "--vid", "r1-01"])
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"within_budget": False}

    on_line = _final_ws(tmp_path / "on-line", final_acc=2.05, anchor_acc=2.00,
                        direction="lower_better")
    proc_eq = _run_cli([sys.executable, str(_SCRIPTS / "verdict_decide.py"),
                        "final-budget", "--artifacts", str(on_line),
                        "--vid", "r1-01"])
    assert proc_eq.returncode == 0, proc_eq.stderr
    assert json.loads(proc_eq.stdout) == {"within_budget": True}

    art2 = _final_ws(tmp_path / "b", final_acc=1.95, anchor_acc=2.00,
                     direction="lower_better")
    proc2 = _run_cli([sys.executable, str(_SCRIPTS / "verdict_decide.py"),
                      "final-budget", "--artifacts", str(art2),
                      "--vid", "r1-01"])
    assert proc2.returncode == 0, proc2.stderr
    assert json.loads(proc2.stdout) == {"within_budget": True}


def test_final_budget_missing_record_and_retired_promote(tmp_path):
    art = tmp_path / "ws"
    _write_anchor(art)
    missing = _run_cli([sys.executable, str(_SCRIPTS / "verdict_decide.py"),
                        "final-budget", "--artifacts", str(art),
                        "--vid", "r1-01"])
    assert missing.returncode == 2
    assert "final_acc" in missing.stderr

    promote = _run_cli([sys.executable, str(_SCRIPTS / "verdict_decide.py"),
                        "promote", "--artifacts", str(art),
                        "--vid", "r1-01"])
    assert promote.returncode != 0              # retired subcommand


# ── deploy: the three new scripts ride the manifest + gate_node roundtrip ─────

def test_deploy_covers_new_scripts_and_gate_node_roundtrip(tmp_path):
    art = tmp_path / "art"
    art.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "ORCA_PYTHON"}
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    deploy = _run_cli([_BASH, str(_DEPLOY_SH)], env=env, timeout=180)
    assert deploy.returncode == 0, deploy.stderr
    for name in ("resolve_train_device.sh", "device_alloc.py",
                 "ledger_aggregate.py"):
        deployed = art / "scripts" / name
        assert deployed.is_file(), name
        assert deployed.read_bytes() == (_SCRIPTS / name).read_bytes()
    stamp = json.loads((art / "scripts" / ".VERSION").read_text(encoding="utf-8"))
    assert len(stamp["manifest"]) == 64          # the new set is stamped

    # gate_node: deploy --verify passes, then the v6 decision passthrough
    _write_anchor(art)
    (art / "rounds" / "001").mkdir(parents=True)
    (art / "history.jsonl").write_text(json.dumps(
        {"vid": "r1-01", "round": 1, "outcome": "latency_fail",
         "makespan_cycles": 800}) + "\n", encoding="utf-8")
    gate = _run_cli([_BASH, str(_SCRIPTS / "gate_node.sh"),
                     "--max-rounds", "5"], env=env)
    assert gate.returncode == 0, gate.stderr
    payload = json.loads(gate.stdout)
    assert payload["decision"] == "loop" and payload["error"] == ""
    assert payload["target_cycles"] == 501 and payload["success_vids"] == []
    assert payload["incumbent_promotion"]["promoted"] is False
    assert payload["incumbent_promotion_path"] == "incumbent_promotion.json"

    # Promotion runs before routing and fails loud instead of silently letting
    # gate_decide report a target-met row whose variant artifacts are missing.
    (art / "history.jsonl").write_text(json.dumps(
        {"vid": "r1-01", "round": 1, "outcome": "success",
         "makespan_cycles": 400}) + "\n", encoding="utf-8")
    broken = _run_cli([_BASH, str(_SCRIPTS / "gate_node.sh"),
                       "--max-rounds", "5"], env=env)
    assert broken.returncode == 0, broken.stderr
    broken_payload = json.loads(broken.stdout)
    assert broken_payload["decision"] == "finish-failed"
    assert broken_payload["reason"] == "incumbent promotion failed"
    assert "cannot promote r1-01" in broken_payload["error"]

    # A completed non-target success promotes first; the three empty rounds
    # were searched against the old base and therefore cannot trigger idle exit.
    variant = art / "variants" / "r1-01"
    (variant / "shadow").mkdir(parents=True)
    (variant / "onnx").mkdir()
    (variant / "profile").mkdir()
    (variant / "shadow" / "model.py").write_text("new", encoding="utf-8")
    (variant / "onnx" / "model.onnx").write_bytes(b"onnx")
    (variant / "profile" / "schedule_result.json").write_text("{}", encoding="utf-8")
    (art / "history.jsonl").write_text(json.dumps(
        {"vid": "r1-01", "round": 1, "outcome": "success",
         "makespan_cycles": 700, "change_sig": "sig-1",
         "parent_vid": None}) + "\n", encoding="utf-8")
    for round_no in (1, 2, 3):
        round_dir = art / "rounds" / f"{round_no:03d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        (round_dir / "proposals.json").write_text(json.dumps({
            "round": round_no, "proposals": [],
            "exhausted_rationale": ["old base exhausted"],
        }), encoding="utf-8")
    reset = _run_cli([_BASH, str(_SCRIPTS / "gate_node.sh"),
                      "--max-rounds", "5", "--idle-round-cap", "3"], env=env)
    assert reset.returncode == 0, reset.stderr
    reset_payload = json.loads(reset.stdout)
    assert reset_payload["decision"] == "loop"
    assert reset_payload["incumbent_promoted"] is True
    assert reset_payload["incumbent_promotion"]["promoted"] is True
    assert "prior zero-proposal rounds describe the old base" in reset_payload["reason"]


# ── P1: repair_trace budget boundary (4 admissible / 5 blocked / 6 rejected) ───

def _seed_repair_trace(art: Path, vid: str, count: int) -> None:
    """Hand-write a repair ledger as the recheck would have (failed
    measurements with a distinct makespan so the fresh attempt appends)."""
    trace = art / "variants" / vid / "repair_trace.json"
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text(json.dumps({
        "vid": vid, "repair_count": count,
        "attempts": [{"round": 1, "measured_makespan_cycles": 900 + i,
                      "target_cycles": 500, "gap_cycles": 400 + i,
                      "reason": "makespan > target_cycles (unified v6 gate)"}
                     for i in range(count)]}, ensure_ascii=False), encoding="utf-8")


def test_repair_trace_four_attempts_still_admits_fifth_measurement(tmp_path):
    art = _recheck_ws(tmp_path, target=500)
    _recheck_variant(art, "r1-01", 1100)         # slower than incumbent
    _seed_repair_trace(art, "r1-01", 4)
    (art / "variants" / "r1-01" / "verdict.json").unlink(missing_ok=True)
    proc = _run_recheck(art)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["latency_improved_count"] == 0
    trace = json.loads((art / "variants" / "r1-01" / "repair_trace.json")
                       .read_text(encoding="utf-8"))
    assert trace["repair_count"] == 5 and len(trace["attempts"]) == 5
    assert trace["attempts"][-1]["measured_makespan_cycles"] == 1100
    assert trace["attempts"][-1]["gap_cycles"] == 100
    assert trace["attempts"][-1]["target_cycles"] == 500


def test_repair_trace_fifth_failure_is_terminal_sixth_fails_loud(tmp_path):
    art = _recheck_ws(tmp_path, target=500)
    _recheck_variant(art, "r1-01", 1100)
    _seed_repair_trace(art, "r1-01", 5)          # the budget is already spent
    proc = _run_recheck(art)
    assert proc.returncode == 2                  # §5.2/§14: script backstop
    assert "repair budget exhausted" in proc.stderr
    # nothing was measured on the forbidden 6th attempt
    assert not (art / "variants" / "r1-01" / "verdict.json").exists()
    trace = json.loads((art / "variants" / "r1-01" / "repair_trace.json")
                       .read_text(encoding="utf-8"))
    assert trace["repair_count"] == 5            # ledger untouched by the guard

    # a REPEATED measured value still consumes budget: reaching the
    # measurement step means a fresh repair pass (the verdict file was
    # deleted), so a no-op repair must never freeze the counter — an
    # unbounded repair loop is exactly what the budget exists to stop
    art2 = _recheck_ws(tmp_path / "b", target=500)
    _recheck_variant(art2, "r1-01", 1100)
    first = _run_recheck(art2)
    assert first.returncode == 0, first.stderr
    trace_path = art2 / "variants" / "r1-01" / "repair_trace.json"
    assert json.loads(trace_path.read_text(encoding="utf-8"))["repair_count"] == 1
    (art2 / "variants" / "r1-01" / "verdict.json").unlink()
    second = _run_recheck(art2)                  # same makespan, new pass
    assert second.returncode == 0, second.stderr
    replayed = json.loads(trace_path.read_text(encoding="utf-8"))
    assert replayed["repair_count"] == 2         # no value-dedup: fail-safe


def test_repair_trace_unparseable_and_nonlatency_failures_fail_loud(tmp_path):
    # a hand-corrupted ledger is a hard error at BOTH the guard and the
    # recorder — never a silently reset budget
    art = _recheck_ws(tmp_path, target=500)
    _recheck_variant(art, "r1-01", 1100)
    (art / "variants" / "r1-01" / "repair_trace.json").write_text(
        "{torn", encoding="utf-8")
    proc = _run_recheck(art)
    assert proc.returncode == 2
    assert "repair_trace.json unparseable" in proc.stderr

    # structural failures are NOT latency repairs: no attempt is recorded
    art2 = _recheck_ws(tmp_path / "b", target=500)
    _recheck_variant(art2, "r1-01", 400)          # would pass the line...
    decl = art2 / "variants" / "r1-01" / "declaration.json"
    doc = json.loads(decl.read_text(encoding="utf-8"))
    doc["edited_files"] = ["pkg/never_touched.py"]   # ...but the file layer
    decl.write_text(json.dumps(doc), encoding="utf-8")  # disagrees
    proc2 = _run_recheck(art2)
    assert proc2.returncode == 0, proc2.stderr
    verdict = json.loads((art2 / "variants" / "r1-01" / "verdict.json")
                         .read_text(encoding="utf-8"))
    assert verdict["outcome"] == "structural_mismatch"
    assert not (art2 / "variants" / "r1-01" / "repair_trace.json").exists()


# ── P1: check_propose_emit v7 — §5 gate over both ending paths ───────────────

_CHECK_EMIT = _SCRIPTS / "check_propose_emit.py"
_VAS_SENTINEL = "[subagent:variant-assessor v1 VAS4K9]"
_ASSESS_SECTIONS = ("## 任务语义", "## 输入输出", "## 架构动机",
                    "## 逐模块职责与物理意义", "## 训练目标与指标方向",
                    "## 与基线差异")
_ASSESS_SUB = "### 被牺牲信息与预期精度代价"


def _assessment_doc(sections: tuple[str, ...] = _ASSESS_SECTIONS,
                    drop: str = "", bare: bool = False,
                    sentinel: str = _VAS_SENTINEL) -> str:
    lines = [sentinel]
    for heading in sections:
        if heading == drop:
            continue
        lines.append(heading)
        if not bare:
            lines.append(f"content for {heading}")
        if heading == "## 与基线差异":
            lines.append(_ASSESS_SUB)
            lines.append("sacrificed information and expected cost")
    return "\n".join(lines) + "\n"


def _stamp_for(art: Path, vid: str, sig: str) -> str:
    import hashlib
    decl = art / "variants" / vid / "declaration.json"
    return f"{vid}|{sig}|{hashlib.sha256(decl.read_bytes()).hexdigest()}"


def _emit_ws(tmp_path: Path, *, outcome: str = "latency_improved",
             delta: int = -600) -> Path:
    """A green single-variant round: one admitted proposal, the variant's
    assessment.md + the v7 analysis stamp, the history rows, and the round
    analysis."""
    art = tmp_path / "ws"
    (art / "scripts").mkdir(parents=True)
    for src in ("history_lib.py", "round_state.py"):
        shutil.copy(_SCRIPTS / src, art / "scripts" / src)
    _write_raw_profile(art / "base" / "profile", 1000)
    _write_anchor(art, target=500)

    rd = art / "rounds" / "001"
    rd.mkdir(parents=True)
    (art / "shadow" / "pkg").mkdir(parents=True)
    (art / "shadow" / "pkg" / "model.py").write_text("model", encoding="utf-8")
    candidates = rd / "candidates"
    candidates.mkdir()
    for name, sentinel in {
        "semantic.md": "[subagent:semantic-architecture-proposer v1 SAP1A1]",
        "hardware.md": "[subagent:hardware-architecture-proposer v1 HAP1B1]",
        "sota.md": "[subagent:sota-architecture-proposer v1 SOTA1C1]",
    }.items():
        (candidates / name).write_text(sentinel + "\ncontent\n", encoding="utf-8")
    (rd / "architecture_decision.md").write_text(
        "[subagent:architecture-selector v1 ASC1D1]\ncontent\n", encoding="utf-8")
    (rd / "proposals.json").write_text(json.dumps({
        "round": 1, "filtered_count": 0,
        "exhausted_rationale": [],
        "proposals": [{"vid": "r1-01", "lever": "activation",
                       "change_sig": "sig:r1-01", "target_modules": ["m"],
                       "target_pattern_id": "low-mfu-matmul",
                       "rationale": "why", "change_spec": "edit",
                       "parent_vid": None,
                       "base_at_proposal": {"vid": None, "makespan_cycles": 1000},
                       "predicted_delta_cycles": delta,
                       "prediction_basis": "predictor",
                       "edited_files": ["pkg/model.py"],
                       "predicted_acc_impact": "low",
                       "accuracy_evidence": "rule-0001",
                       "sota_reference": "ref"}]}), encoding="utf-8")
    (rd / "verdicts.jsonl").write_text(json.dumps(
        {"vid": "r1-01", "round": 1, "outcome": outcome}) + "\n",
        encoding="utf-8")
    (rd / "analysis.md").write_text(
        "## latency\nreached the line; predicted-vs-line margin disclosed; "
        "soft-alignment: aligned; next direction\n",
        encoding="utf-8")

    vd = art / "variants" / "r1-01"
    vd.mkdir(parents=True)
    (vd / "declaration.json").write_text(json.dumps(
        {"vid": "r1-01", "change_sig": "sig:r1-01",
         "predicted_delta_cycles": delta}), encoding="utf-8")
    (vd / "assessment.md").write_text(_assessment_doc(), encoding="utf-8")
    (vd / ".analysis_stamp.json").write_text(json.dumps(
        {"key": _stamp_for(art, "r1-01", "sig:r1-01")}), encoding="utf-8")

    hist = art / "history.jsonl"
    _impl(hist, "r1-01", "sig:r1-01")
    if outcome == "latency_improved":
        history_lib.append_latency(hist, "r1-01", structural_check="pass",
                                   makespan_cycles=400, latency_gate="pass",
                                   pred_actual_ratio=None,
                                   outcome="latency_improved")
    else:
        history_lib.append_latency(hist, "r1-01", structural_check="pass",
                                   makespan_cycles=800, latency_gate="fail",
                                   pred_actual_ratio=None,
                                   outcome="latency_fail")
        (rd / "direction.json").write_text(json.dumps(
            {"round": 1, "failed_sigs": ["sig:r1-01"]}), encoding="utf-8")
    return art


def _check_emit(art: Path) -> subprocess.CompletedProcess:
    return _run_cli([sys.executable, str(_CHECK_EMIT),
                     "--artifacts", str(art)])


def test_emit_gate_green_on_both_ending_paths(tmp_path):
    """The improvement path and the honest elimination path
    (latency_fail after repair exhaustion) both pass the v7 gate."""
    for outcome in ("latency_improved", "latency_fail"):
        art = _emit_ws(tmp_path / outcome, outcome=outcome)
        proc = _check_emit(art)
        assert proc.returncode == 0, (outcome, proc.stderr)
        assert json.loads(proc.stdout)["ok"] is True


def test_emit_gate_requires_all_architecture_documents(tmp_path):
    for relative in (
            "rounds/001/candidates/semantic.md",
            "rounds/001/candidates/hardware.md",
            "rounds/001/candidates/sota.md",
            "rounds/001/architecture_decision.md"):
        art = _emit_ws(tmp_path / relative.replace("/", "-"))
        (art / relative).unlink()
        proc = _check_emit(art)
        assert proc.returncode == 1, relative
        assert Path(relative).name in proc.stderr


def test_emit_gate_rejects_multi_proposal_and_zero_delta(tmp_path):
    art = _emit_ws(tmp_path / "many")
    proposals_path = art / "rounds" / "001" / "proposals.json"
    doc = json.loads(proposals_path.read_text(encoding="utf-8"))
    doc["proposals"].append(dict(doc["proposals"][0], vid="r1-02",
                                 change_sig="sig:r1-02"))
    proposals_path.write_text(json.dumps(doc), encoding="utf-8")
    proc = _check_emit(art)
    assert proc.returncode == 1
    assert "exactly ONE" in proc.stderr

    # Prediction is calibration evidence only.
    art2 = _emit_ws(tmp_path / "above", delta=-100)
    assert _check_emit(art2).returncode == 0

    # A non-negative estimate is still admitted; MFU measurement decides.
    art3 = _emit_ws(tmp_path / "zero", delta=0)
    proc3 = _check_emit(art3)
    assert proc3.returncode == 0
    # sota_reference may be null (why-no-precedent lives in the rationale)
    art4 = _emit_ws(tmp_path / "noref")
    proposals = json.loads((art4 / "rounds" / "001" / "proposals.json")
                           .read_text(encoding="utf-8"))
    proposals["proposals"][0]["sota_reference"] = None
    (art4 / "rounds" / "001" / "proposals.json").write_text(
        json.dumps(proposals), encoding="utf-8")
    assert _check_emit(art4).returncode == 0


def test_emit_gate_assessment_matrix(tmp_path):
    """v7 §5.3: the assessment.md gate — sentinel / non-empty body / six
    sections / the conclusion sub-section; plus the v7 stamp key."""
    def break_doc(art: Path, content: str) -> None:
        (art / "variants" / "r1-01" / "assessment.md").write_text(
            content, encoding="utf-8")

    # sentinel broken
    art = _emit_ws(tmp_path / "s")
    break_doc(art, _assessment_doc(sentinel="wrong sentinel"))
    proc = _check_emit(art)
    assert proc.returncode == 1 and "sentinel" in proc.stderr

    # body empty (sentinel only)
    art = _emit_ws(tmp_path / "e")
    break_doc(art, _VAS_SENTINEL + "\n")
    proc = _check_emit(art)
    assert proc.returncode == 1 and "empty" in proc.stderr

    # a section missing
    art = _emit_ws(tmp_path / "c1")
    break_doc(art, _assessment_doc(drop="## 与基线差异"))
    proc = _check_emit(art)
    assert proc.returncode == 1 and "与基线差异" in proc.stderr

    # the conclusion SUB-section missing
    art = _emit_ws(tmp_path / "c2")
    text = _assessment_doc()
    text = text.replace(_ASSESS_SUB + "\nsacrificed information and expected cost\n", "")
    break_doc(art, text)
    proc = _check_emit(art)
    assert proc.returncode == 1 and "被牺牲信息与预期精度代价" in proc.stderr

    # the whole document missing
    art = _emit_ws(tmp_path / "c3")
    (art / "variants" / "r1-01" / "assessment.md").unlink()
    proc = _check_emit(art)
    assert proc.returncode == 1 and "assessment.md" in proc.stderr


def test_emit_gate_stamp_key_matrix(tmp_path):
    """v7 §5.3 stamp fix: the key is vid|sig|sha256(declaration.json) — a
    repair that rewrites declaration.json changes the key, so a stale stamp
    can never green-light a skipped re-assessment."""
    # a stale stamp (declaration rewritten after stamping) is rejected
    art = _emit_ws(tmp_path / "stale")
    decl = art / "variants" / "r1-01" / "declaration.json"
    doc = json.loads(decl.read_text(encoding="utf-8"))
    doc["predicted_delta_cycles"] = -601          # the repair rewrite
    decl.write_text(json.dumps(doc), encoding="utf-8")
    proc = _check_emit(art)
    assert proc.returncode == 1
    assert "v7 key" in proc.stderr

    # re-stamping against the CURRENT declaration re-admits
    (art / "variants" / "r1-01" / ".analysis_stamp.json").write_text(json.dumps(
        {"key": _stamp_for(art, "r1-01", "sig:r1-01")}), encoding="utf-8")
    assert _check_emit(art).returncode == 0

    # a wrong sig in the key is rejected too
    art2 = _emit_ws(tmp_path / "sig")
    (art2 / "variants" / "r1-01" / ".analysis_stamp.json").write_text(
        json.dumps({"key": _stamp_for(art2, "r1-01", "sig:other")}),
        encoding="utf-8")
    assert _check_emit(art2).returncode == 1


def test_emit_gate_repair_trace_and_analysis_and_direction_rules(tmp_path):
    # a hand-inflated 6th attempt is rejected at the gate even though the
    # recheck guard was bypassed
    art = _emit_ws(tmp_path / "r6", outcome="latency_fail")
    _seed_repair_trace(art, "r1-01", 6)
    proc = _check_emit(art)
    assert proc.returncode == 1 and "repair budget" in proc.stderr

    # count/attempts disagreement (hand edit) fails loud
    art = _emit_ws(tmp_path / "rk", outcome="latency_fail")
    _seed_repair_trace(art, "r1-01", 2)
    trace_path = art / "variants" / "r1-01" / "repair_trace.json"
    doc = json.loads(trace_path.read_text(encoding="utf-8"))
    doc["repair_count"] = 3
    trace_path.write_text(json.dumps(doc), encoding="utf-8")
    proc = _check_emit(art)
    assert proc.returncode == 1 and "len(attempts)" in proc.stderr

    # the legal exhaustion (5 attempts) is admissible
    art = _emit_ws(tmp_path / "r5", outcome="latency_fail")
    _seed_repair_trace(art, "r1-01", 5)
    assert _check_emit(art).returncode == 0

    # analysis.md without the latency section is rejected on BOTH paths
    for outcome in ("latency_improved", "latency_fail"):
        art = _emit_ws(tmp_path / f"a-{outcome}", outcome=outcome)
        (art / "rounds" / "001" / "analysis.md").write_text(
            "## summary\nno latency section here\n", encoding="utf-8")
        proc = _check_emit(art)
        assert proc.returncode == 1 and "## latency" in proc.stderr

    # the latency_fail path must land failed_sigs in direction.json
    art = _emit_ws(tmp_path / "d", outcome="latency_fail")
    (art / "rounds" / "001" / "direction.json").unlink()
    proc = _check_emit(art)
    assert proc.returncode == 1 and "direction.json" in proc.stderr


# ── P2: flatten→probe wiring smoke + check_probe_emit v6 (§6.2) ───────────────

_PROBE_EMIT = _SCRIPTS / "check_probe_emit.py"
_WATCH_PY = _SCRIPTS / "watch_variant.py"


def test_flatten_to_probe_resource_chain_smoke(tmp_path):
    """The entry→probe resource wiring over mocked backend CLIs (v7): the
    entry resolver freezes train_device.json; the probe side observes the
    backend's raw output through `probe`, claims the agent-chosen idx, and
    adopts a long-lived owner."""
    idle_table = ("| NPU | Name  | Health | Process |\n"
                  "| 0   | 910B3 | OK     | -       |\n"
                  "| 1   | 910B3 | OK     | -       |\n")
    art = tmp_path / "ws"
    art.mkdir()
    env = _stub_env(tmp_path, tools={
        "npu-smi": f"#!{_BASH}\n"
                   f"if [ \"${{1:-}}\" = \"-l\" ]; then printf 'Total Count : 2\\n'; "
                   f"else printf '%s' '{idle_table}'; fi\n"})
    env["ORCA_ARTIFACTS_DIR"] = str(art)

    # flatten side: resolve once, freeze the file
    resolved = _resolve(env)
    assert resolved.returncode == 0, resolved.stderr
    assert json.loads(resolved.stdout) == {"backend": "npu", "device_count": 2,
                                           "resolved_by": "npu-smi"}
    assert json.loads((art / "train_device.json").read_text(
        encoding="utf-8"))["backend"] == "npu"

    # probe side: observe (raw passthrough) -> choose idx 0 -> claim -> adopt
    probe = _alloc(art, "probe", "--backend", "npu", env=env)
    assert probe.returncode == 0, probe.stderr
    doc = json.loads(probe.stdout)
    assert doc["backend"] == "npu" and doc["device_count"] == 2
    assert doc["locks"] == []
    assert idle_table in doc["raw"]        # the occupancy text passes through VERBATIM

    claimed = _alloc(art, "claim", "--vid", "r1-01", "--idx", "0", env=env)
    assert claimed.returncode == 0, claimed.stderr
    assert json.loads(claimed.stdout)["ok"] is True
    adopted = _alloc(art, "adopt", "--vid", "r1-01",
                     "--pid", str(os.getpid()), env=env)
    assert adopted.returncode == 0, adopted.stderr
    assert json.loads(adopted.stdout) == {"adopted": True, "idx": 0,
                                          "vid": "r1-01", "pid": os.getpid()}
    # the live lock shows up in the NEXT probe's ledger view
    doc2 = json.loads(_alloc(art, "probe", "--backend", "npu", env=env).stdout)
    assert doc2["locks"] == [{"idx": 0, "vid": "r1-01", "pid": os.getpid(),
                              "acquired_at": doc2["locks"][0]["acquired_at"]}]

    # release reopens the card for the next variant
    rel = _alloc(art, "release", "--idx", "0", env=env)
    assert rel.returncode == 0, rel.stderr
    assert json.loads(_alloc(art, "probe", "--backend", "npu",
                             env=env).stdout)["locks"] == []


def test_device_alloc_adopt_rebinds_ownership_fail_loud(tmp_path):
    art = tmp_path / "ws"
    _write_train_device(art, count=2)
    dead = _dead_pid()
    _write_lock(art, 0, "r1-01", dead)
    holder = subprocess.Popen(["sleep", "60"],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    try:
        # a dead target pid is refused: adopting it would pin the card
        # behind a phantom owner once the pid is recycled
        ghost = _alloc(art, "adopt", "--vid", "r1-01", "--pid", str(dead))
        assert ghost.returncode == 2 and "not alive" in ghost.stderr

        adopted = _alloc(art, "adopt", "--vid", "r1-01", "--pid", str(holder.pid))
        assert adopted.returncode == 0, adopted.stderr
        lock = json.loads((art / "devices" / "0.lock").read_text(encoding="utf-8"))
        assert lock["pid"] == holder.pid and lock["vid"] == "r1-01"

        # ambiguity (two locks naming the vid) is a torn ledger, never a guess
        _write_lock(art, 1, "r1-01", os.getpid())
        torn = _alloc(art, "adopt", "--vid", "r1-01", "--pid", str(os.getpid()))
        assert torn.returncode == 2 and "torn" in torn.stderr
    finally:
        holder.terminate()
        holder.wait()

    # nothing names the vid -> fail loud
    art2 = tmp_path / "ws2"
    _write_train_device(art2, count=1)
    missing = _alloc(art2, "adopt", "--vid", "zz", "--pid", "1")
    assert missing.returncode == 2 and "no lock" in missing.stderr


def test_watch_variant_pins_signature_and_fails_loud_on_torn_ws(tmp_path):
    """The P2 signature stays; the real body now fails loud on a torn
    workspace instead of exiting 0 unsupervised (the stub's old behavior)."""
    art = tmp_path / "ws"
    art.mkdir()
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)

    helped = _run_cli([sys.executable, str(_WATCH_PY), "--help"], env=env)
    assert helped.returncode == 0
    assert "--vid" in helped.stdout and "--device" in helped.stdout

    missing_vid = _run_cli([sys.executable, str(_WATCH_PY), "--device", "0"], env=env)
    assert missing_vid.returncode == 2
    bad_device = _run_cli([sys.executable, str(_WATCH_PY), "--vid", "r1-01",
                           "--device", "-3"], env=env)
    assert bad_device.returncode == 2
    unknown = _run_cli([sys.executable, str(_WATCH_PY), "--vid", "r1-01",
                        "--device", "0", "--bogus"], env=env)
    assert unknown.returncode == 2

    # well-formed invocation over a torn workspace (no contracts) -> FATAL
    torn = _run_cli([sys.executable, str(_WATCH_PY), "--vid", "r1-01",
                     "--device", "0"], env=env)
    assert torn.returncode == 2
    assert "contracts.json" in torn.stderr
    vdir = art / "variants" / "r1-01"
    # the pid file lands before any supervision decision (the probe node's
    # launch confirmation keys on it)
    assert (vdir / "watchdog.pid").read_text(encoding="utf-8").strip().isdigit()


def _probe_ws(tmp_path: Path, *, makespan: int = 400,
              verdict: bool = True, lock_vid: str = "r1-01",
              watchdog: bool = True,
              liveness: dict | None = None) -> Path:
    """A launched-variant workspace for the §6.2 gate: one improved vid
    with a verdict below the incumbent, a ledger lock naming it, the
    watchdog pid file, and the liveness record."""
    art = tmp_path / "ws"
    (art / "scripts").mkdir(parents=True)
    for src in ("history_lib.py", "round_state.py"):
        shutil.copy(_SCRIPTS / src, art / "scripts" / src)
    _write_anchor(art, target=500)
    (art / "rounds" / "001").mkdir(parents=True)
    hist = art / "history.jsonl"
    _impl(hist, "r1-01", "sig:r1-01")
    history_lib.append_latency(hist, "r1-01", structural_check="pass",
                               makespan_cycles=makespan, latency_gate="pass",
                               pred_actual_ratio=None, outcome="latency_improved")
    vd = art / "variants" / "r1-01"
    (vd / "train").mkdir(parents=True)
    # the implementation completion proof: DONE pins the declaration it was
    # written against (write_done_marker's hash discipline)
    decl_text = json.dumps({"vid": "r1-01", "edited_files": [],
                            "predicted_delta_cycles": -400})
    (vd / "declaration.json").write_text(decl_text, encoding="utf-8")
    (vd / "DONE").write_text(json.dumps({
        "vid": "r1-01",
        "declaration_sha256": hashlib.sha256(
            decl_text.encode()).hexdigest(),
        "ts": "2026-09-01T00:00:00+00:00"}), encoding="utf-8")
    if verdict:
        (vd / "verdict.json").write_text(json.dumps(
            {"vid": "r1-01", "round": 1, "outcome": "latency_improved",
             "makespan_cycles": makespan, "target_cycles": 500}),
            encoding="utf-8")
    _write_lock(art, 0, lock_vid, os.getpid())
    if watchdog:
        # the real guardian loops until terminal — the emit gate probes its
        # pid for liveness + attribution, so the fake one is session-leading
        # and cmdline-attributed (auto-reaped after the test)
        (vd / "watchdog.pid").write_text(
            f"{_attributed_session_sleeper('watch_variant.py').pid}\n",
            encoding="utf-8")
    if liveness is not None:
        record = {"vid": "r1-01", "epoch1_ok": True, "device": 0,
                  "train_pid": 111, "ts": "2026-08-31T00:00:00+00:00"}
        record.update(liveness)
        (vd / "train" / "liveness.json").write_text(json.dumps(record),
                                                    encoding="utf-8")
    return art


def _check_probe(art: Path) -> subprocess.CompletedProcess:
    return _run_cli([sys.executable, str(_PROBE_EMIT),
                     "--artifacts", str(art)])


def _attributed_sleeper() -> subprocess.Popen:
    """A live process whose /proc cmdline references train.rendered.sh (the
    wrapper attribution token) — it never actually trains."""
    return subprocess.Popen(
        [_BASH, "-c", "while :; do sleep 30; done # train.rendered.sh"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# session-leading sleepers (own process group): the watchdog's group kill
# and the emit gate's group-liveness probe must never reach the test runner's
# own process group, so every fake worker/guardian below is setsid'd
_GUARDIANS: list[subprocess.Popen] = []


@pytest.fixture(autouse=True)
def _reap_session_sleepers():
    yield
    for proc in _GUARDIANS:
        if proc.poll() is None:
            try:                       # take the whole group: the leader's
                os.killpg(proc.pid, signal.SIGTERM)  # `sleep` child too
            except (ProcessLookupError, PermissionError):
                proc.terminate()
            proc.wait()
    _GUARDIANS.clear()


def _attributed_session_sleeper(token: str) -> subprocess.Popen:
    """A live SESSION-LEADING process whose /proc cmdline references `token`
    (group kill safe). Registered for automatic reaping."""
    proc = subprocess.Popen(
        [_BASH, "-c", f"while :; do sleep 30; done # {token}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    _GUARDIANS.append(proc)
    return proc


def test_probe_emit_green_launch_state(tmp_path):
    art = _probe_ws(tmp_path, liveness={})
    sleeper = _attributed_sleeper()
    try:
        (art / "variants" / "r1-01" / "train" / "train.pid").write_text(
            f"{sleeper.pid}\n", encoding="utf-8")
        proc = _check_probe(art)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == {"ok": True, "round": 1,
                                           "probed": ["r1-01"]}
    finally:
        sleeper.terminate()
        sleeper.wait()


def test_probe_emit_rejects_declaration_drift_after_done(tmp_path):
    """The DONE marker pins the measured declaration: editing
    declaration.json after the marker was written (a repair pass that
    finished the paperwork but not the re-measure) is a torn workspace —
    the gate fails loud instead of launching the drifted structure."""
    art = _probe_ws(tmp_path, liveness={})
    (art / "variants" / "r1-01" / "train" / "train.pid").write_text(
        f"{os.getpid()}\n", encoding="utf-8")
    drifted = json.loads(
        (art / "variants" / "r1-01" / "declaration.json").read_text(
            encoding="utf-8"))
    drifted["predicted_delta_cycles"] = -1        # edited after DONE
    (art / "variants" / "r1-01" / "declaration.json").write_text(
        json.dumps(drifted), encoding="utf-8")
    proc = _check_probe(art)
    assert proc.returncode == 1
    assert "declaration_sha256" in proc.stderr
    assert "torn workspace" in proc.stderr


def test_probe_emit_rejects_torn_verdict(tmp_path):
    # equal to the incumbent -> torn workspace, never admissible
    art = _probe_ws(tmp_path, makespan=1000, liveness={})
    (art / "variants" / "r1-01" / "train" / "train.pid").write_text(
        f"{os.getpid()}\n", encoding="utf-8")
    proc = _check_probe(art)
    assert proc.returncode == 1
    assert "torn workspace" in proc.stderr and "not below incumbent" in proc.stderr

    # verdict file gone entirely -> same failure class
    art2 = _probe_ws(tmp_path / "b", verdict=False, liveness={})
    (art2 / "variants" / "r1-01" / "train" / "train.pid").write_text(
        f"{os.getpid()}\n", encoding="utf-8")
    proc2 = _check_probe(art2)
    assert proc2.returncode == 1
    assert "invalid or missing" in proc2.stderr and "verdict.json" in proc2.stderr


def test_probe_emit_requires_lock_watchdog_and_liveness(tmp_path):
    def with_pid(art: Path, pid_text: str) -> None:
        (art / "variants" / "r1-01" / "train" / "train.pid").write_text(
            pid_text, encoding="utf-8")

    # the lock names a different vid -> unclaimed card
    art = _probe_ws(tmp_path, lock_vid="r0-99", liveness={})
    with_pid(art, f"{os.getpid()}\n")
    proc = _check_probe(art)
    assert proc.returncode == 1 and "no devices/<idx>.lock" in proc.stderr

    # watchdog guardian missing
    art2 = _probe_ws(tmp_path / "b", watchdog=False, liveness={})
    with_pid(art2, f"{os.getpid()}\n")
    proc2 = _check_probe(art2)
    assert proc2.returncode == 1 and "watchdog.pid missing" in proc2.stderr

    # liveness record missing (the epoch-1 proof is a hard precondition)
    art3 = _probe_ws(tmp_path / "c")
    with_pid(art3, f"{os.getpid()}\n")
    proc3 = _check_probe(art3)
    assert proc3.returncode == 1 and "liveness.json missing" in proc3.stderr

    # epoch1_ok false -> the record exists but proves the wrong thing
    art4 = _probe_ws(tmp_path / "d", liveness={"epoch1_ok": False})
    with_pid(art4, f"{os.getpid()}\n")
    proc4 = _check_probe(art4)
    assert proc4.returncode == 1 and "epoch1_ok" in proc4.stderr


def test_probe_emit_dead_pid_needs_terminal_state(tmp_path):
    dead = _dead_pid()
    art = _probe_ws(tmp_path, liveness={})
    (art / "variants" / "r1-01" / "train" / "train.pid").write_text(
        f"{dead}\n", encoding="utf-8")
    proc = _check_probe(art)
    assert proc.returncode == 1
    assert "dead with no terminal" in proc.stderr

    # the watchdog's terminal file (train_status.json) redeems the dead pid
    (art / "variants" / "r1-01" / "train_status.json").write_text(
        json.dumps({"vid": "r1-01", "stage": "done", "epoch": 10,
                    "ts": "2026-08-31T00:00:00+00:00"}), encoding="utf-8")
    proc2 = _check_probe(art)
    assert proc2.returncode == 0, proc2.stderr
    assert json.loads(proc2.stdout)["probed"] == ["r1-01"]


def test_probe_emit_empty_training_set_and_terminal_vids_pass(tmp_path):    # a vid that already reached a terminal row is out of scope
    art = _probe_ws(tmp_path, liveness={})
    history_lib.append_terminal(art / "history.jsonl", "r1-01",
                                outcome="probe_insufficient", stage="liveness",
                                max_retries_hit=True)
    proc = _check_probe(art)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["probed"] == []

    # a workspace with no latency_improved vid at all: nothing to verify
    art2 = _probe_ws(tmp_path / "b", liveness={})
    (art2 / "history.jsonl").unlink()
    proc2 = _check_probe(art2)
    assert proc2.returncode == 0, proc2.stderr
    assert json.loads(proc2.stdout)["probed"] == []


def test_probe_emit_pid_attribution_and_input_failures(tmp_path):
    # a LIVE pid whose cmdline has nothing to do with our training (pid
    # reuse) must be rejected — never counted as our liveness
    art = _probe_ws(tmp_path, liveness={})
    stranger = subprocess.Popen(["sleep", "300"],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    try:
        (art / "variants" / "r1-01" / "train" / "train.pid").write_text(
            f"{stranger.pid}\n", encoding="utf-8")
        proc = _check_probe(art)
        assert proc.returncode == 1
        assert "pid reuse" in proc.stderr
    finally:
        stranger.terminate()
        stranger.wait()

    # a corrupt pid file fails loud, never guessed around
    art2 = _probe_ws(tmp_path / "b", liveness={})
    (art2 / "variants" / "r1-01" / "train" / "train.pid").write_text(
        "not-a-pid\n", encoding="utf-8")
    proc2 = _check_probe(art2)
    assert proc2.returncode == 1 and "not an int" in proc2.stderr

    # the admission line itself is unavailable (baseline stage incomplete)
    art3 = _probe_ws(tmp_path / "c", liveness={})
    (art3 / "base" / "origin_anchor.json").unlink()
    (art3 / "variants" / "r1-01" / "train" / "train.pid").write_text(
        f"{os.getpid()}\n", encoding="utf-8")
    proc3 = _check_probe(art3)
    assert proc3.returncode == 1
    assert "origin_anchor" in proc3.stderr       # via check_verdict per-vid


# ── P3: watch_variant.py — warmup / streak boundaries / early-stop kill ────

_EPOCH_LINE = "epoch {e} metric {m}"


def _watch_ws(tmp_path: Path, *, epochs: int = 20, log_epochs=None,
              candidate: str = "0.5", baseline: str = "0.9",
              direction: str = "higher_better",
              rc: int | None = None, with_lock: bool = True) -> Path:
    """A fully-launched variant workspace for the watchdog: deployed scripts,
    contracts + anchor + baseline curve/full-acc, the seeded ledger shard,
    the history rows, a mock train log, and (optionally) the device lock.

    The eval "run" is a stub template that cats a metric file sitting next
    to the resolved ckpt — the eval CHAIN (render -> run -> extract) is the
    real one, only the training/eval bodies are fake."""
    art = tmp_path / "ws"
    (art / "scripts").mkdir(parents=True)
    for src in ("metric_curve.py", "verdict_decide.py", "history_lib.py",
                "ledger_aggregate.py", "device_alloc.py", "render_run.sh",
                "assert_shadow.py", "pid_lib.py"):
        shutil.copy(_SCRIPTS / src, art / "scripts" / src)
    (art / "orca_inject").mkdir(parents=True)
    for src in ("header.env", "sitecustomize.py"):
        shutil.copy(_SCRIPTS / "orca_inject" / src, art / "orca_inject" / src)
    (art / "templates").mkdir(parents=True)
    (art / "templates" / "run_eval.template.sh").write_text(
        'cat "<<ckpt>>.metric"\n', encoding="utf-8")
    (art / "contracts.json").write_text(json.dumps({
        "interpreter": {"sys_executable": sys.executable},
        "full_train_budget": {"epochs": epochs, "seed": 7},
        "proxy_budget": {"epochs": 1, "seed": 7},
        "early_stop": {"warmup_frac": 0.1, "streak_frac": 0.3},
        "eval": {"metric_extraction": {
                     "kind": "stdout_regex",
                     "pattern": r"final metric: ([0-9]*\.?[0-9]+)"},
                 "metric_direction": direction, "tier": "A"},
        "train": {"ckpt_output_rule": "{out_dir}/ckpt_*.pt",
                  "epoch_metric_extraction":
                      r"epoch (?P<epoch>[0-9]+) metric (?P<metric>[0-9]*\.?[0-9]+)",
                  "ckpt_per_epoch": False},
        "shadow": {"shadow_pkgs": ["pkg"]}}), encoding="utf-8")
    (art / "readiness").mkdir(parents=True)
    (art / "readiness" / "readiness.json").write_text(
        json.dumps({"project_root": str(tmp_path)}), encoding="utf-8")
    _write_anchor(art, budget=0.05)
    (art / "baseline").mkdir(parents=True)
    # the baseline curve is metric_curve extract's JSONL output (the raw
    # text form is only the train LOG's format)
    (art / "baseline" / "baseline_metrics.jsonl").write_text(
        "".join(json.dumps({"epoch": e, "metric": float(baseline)}) + "\n"
                for e in range(1, epochs + 1)), encoding="utf-8")
    (art / "baseline" / "baseline_full_acc.json").write_text(json.dumps(
        {"baseline_full_acc": float(baseline), "ckpt": "baseline/last.pt",
         "full_train_budget": {"epochs": epochs, "seed": 7}}), encoding="utf-8")

    vd = art / "variants" / "r1-01"
    (vd / "metrics").mkdir(parents=True)
    for shadow in (art / "shadow", vd / "shadow"):
        (shadow / "pkg").mkdir(parents=True)
        (shadow / "pkg" / "__init__.py").write_text("#\n", encoding="utf-8")
    (vd / "train").mkdir(parents=True)
    # the relaunch path really executes this stub: it "trains" epochs fast
    # and touches the ckpts the contract's rule predicts
    (vd / "train" / "train.rendered.sh").write_text(
        "#!/usr/bin/env bash\n"
        'cd "$(dirname "$0")"\n'
        f"for i in $(seq 1 {epochs}); do echo \"epoch $i metric 0.85\"; "
        "sleep 0.05; touch \"ckpt_$i.pt\"; done\n", encoding="utf-8")
    if log_epochs is not None:
        (vd / "train" / "train.log").write_text(
            "".join(_EPOCH_LINE.format(e=e, m=candidate) + "\n"
                    for e in log_epochs), encoding="utf-8")
    if rc is not None:
        (vd / "train" / "rc").write_text(f"{rc}\n", encoding="utf-8")
    if with_lock:
        _write_lock(art, 0, "r1-01", os.getpid())
    # the shard exactly as the proposal node seeds it (§5.1 Step 6)
    (vd / "ledger_entry.json").write_text(json.dumps(
        {"vid": "r1-01", "status": "latency_improved", "epoch": None,
         "metric": None, "gap": None, "device": None,
         "change_summary": "swap erf activation for relu in block m",
         "ts": "2026-08-31T00:00:00+00:00"}), encoding="utf-8")
    hist = art / "history.jsonl"
    _impl(hist, "r1-01", "sig:r1-01")
    history_lib.append_latency(hist, "r1-01", structural_check="pass",
                               makespan_cycles=400, latency_gate="pass",
                               pred_actual_ratio=None, outcome="latency_improved")
    return art


def _watch_run(art: Path, *extra: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    return _run_cli([sys.executable, str(_WATCH_PY), "--vid", "r1-01",
                     "--device", "0", *extra], env=env, timeout=180)


def _train_status(art: Path) -> dict:
    return json.loads((art / "variants" / "r1-01" / "train_status.json")
                      .read_text(encoding="utf-8"))


def _latest_row(art: Path) -> dict:
    return history_lib.read_latest(art / "history.jsonl")["r1-01"]


def _watch_shard(art: Path) -> dict:
    return json.loads((art / "variants" / "r1-01" / "ledger_entry.json")
                      .read_text(encoding="utf-8"))


def _write_final_metric_ckpts(art: Path, epochs: int,
                              metric_line: str) -> None:
    """The fake eval products: one ckpt + one .metric file per epoch, staged
    mtimes so the LAST epoch's ckpt resolves as the final one."""
    td = art / "variants" / "r1-01" / "train"
    for i in range(1, epochs + 1):
        (td / f"ckpt_{i}.pt").write_text("", encoding="utf-8")
        (td / f"ckpt_{i}.pt.metric").write_text(metric_line, encoding="utf-8")
        os.utime(td / f"ckpt_{i}.pt", (1700_000_000 + i, 1700_000_000 + i))
        os.utime(td / f"ckpt_{i}.pt.metric",
                 (1700_000_000 + i, 1700_000_000 + i))


def test_watch_warmup_epochs_are_never_judged(tmp_path):
    """§7.2 warmup: epochs <= ceil(0.1 x E) never judge and never count — a
    wildly over-budget curve inside warmup leaves the streak at zero."""
    art = _watch_ws(tmp_path, epochs=20, log_epochs=range(1, 3),
                    candidate="0.1")        # warmup = ceil(0.1 x 20) = 2
    sleeper = _attributed_session_sleeper("train.rendered.sh")
    (art / "variants" / "r1-01" / "train" / "train.pid").write_text(
        f"{sleeper.pid}\n", encoding="utf-8")
    proc = _watch_run(art, "--once")
    assert proc.returncode == 0, proc.stderr
    status = _train_status(art)
    assert status["stage"] == "training"
    assert status["epoch"] == 2                       # seen, never judged
    assert status["over_budget_streak"] == 0
    assert status["gap"] is None
    assert _latest_row(art).get("outcome") == "latency_improved"   # no terminal
    assert sleeper.poll() is None                     # nobody was killed
    assert (art / "devices" / "0.lock").is_file()


def test_watch_five_over_budget_epochs_do_not_kill(tmp_path):
    """Judged epochs 3..7 (five consecutive over-budget) leave the training
    alive — v7's threshold for E=20 is max(2, ceil(0.3 x 20)) = 6, and a poll
    cycle over an ALREADY-counted epoch must not inflate the streak."""
    art = _watch_ws(tmp_path, epochs=20, log_epochs=range(1, 8))
    sleeper = _attributed_session_sleeper("train.rendered.sh")
    (art / "variants" / "r1-01" / "train" / "train.pid").write_text(
        f"{sleeper.pid}\n", encoding="utf-8")
    first = _watch_run(art, "--once")
    assert first.returncode == 0, first.stderr
    status = _train_status(art)
    assert status["stage"] == "training"
    assert status["epoch"] == 7 and status["over_budget_streak"] == 5
    assert status["gap"] == pytest.approx(0.4)        # 0.9 - 0.5, normalized

    # a re-poll over the SAME curve: per-epoch counting, never per-cycle
    second = _watch_run(art, "--once")
    assert second.returncode == 0, second.stderr
    assert _train_status(art)["over_budget_streak"] == 5
    assert sleeper.poll() is None
    assert _latest_row(art).get("outcome") == "latency_improved"


def test_watch_six_over_budget_epochs_kill_then_replay_never_rekills(tmp_path):
    """A streak reaching the E-derived threshold (max(2, ceil(0.3 x 20)) = 6)
    kills the attributed process group, records the terminal
    accuracy_fail row with the FROZEN re-parsed depth, does the full
    terminal tail — and a re-entry replays the terminal without re-killing
    or appending a second history row."""
    art = _watch_ws(tmp_path, epochs=20, log_epochs=range(1, 9))
    # judged epochs 3..8 -> the streak reaches 6 exactly at epoch 8
    sleeper = _attributed_session_sleeper("train.rendered.sh")
    (art / "variants" / "r1-01" / "train" / "train.pid").write_text(
        f"{sleeper.pid}\n", encoding="utf-8")
    proc = _watch_run(art, "--once")
    assert proc.returncode == 0, proc.stderr
    sleeper.wait(timeout=20)                          # the group died

    status = _train_status(art)
    assert status["stage"] == "killed"
    assert status["stopped_at_epoch"] == 8            # frozen re-parse
    assert status["over_budget_streak"] == 6
    assert status["gap"] == pytest.approx(0.4)

    row = _latest_row(art)
    assert row["outcome"] == "accuracy_fail"
    assert row["stopped_at_epoch"] == 8
    assert row["over_budget_streak"] == 6
    assert row["gap"] == pytest.approx(0.4)

    # the terminal tail: lock released, rules marker, shard (with the
    # proposal-seeded change_summary preserved), derived ledger aggregated
    assert not (art / "devices" / "0.lock").exists()
    assert (art / "variants" / "r1-01" / ".rules_pending").is_file()
    shard = _watch_shard(art)
    assert shard["status"] == "accuracy_fail"
    assert shard["change_summary"] == "swap erf activation for relu in block m"
    ledger = json.loads((art / "experiment_ledger.json").read_text(
        encoding="utf-8"))
    assert [r for r in ledger["rows"] if r["vid"] == "r1-01"][0]["status"] \
        == "accuracy_fail"

    # §7.6 replay: same terminal, no second kill, no second history row
    rows_before = len(history_lib.read_rows(art / "history.jsonl"))
    replay = _watch_run(art, "--once")
    assert replay.returncode == 0, replay.stderr
    assert _train_status(art)["stage"] == "killed"
    assert len(history_lib.read_rows(art / "history.jsonl")) == rows_before


def test_watch_kill_attribution_refusal_is_fatal_no_terminal(tmp_path):
    """§14: the kill attribution check failing refuses the kill and FATALs —
    the stranger survives, no terminal is written, the lock stays."""
    art = _watch_ws(tmp_path, epochs=20, log_epochs=range(1, 9))
    stranger = subprocess.Popen(["sleep", "300"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)
    _GUARDIANS.append(stranger)
    (art / "variants" / "r1-01" / "train" / "train.pid").write_text(
        f"{stranger.pid}\n", encoding="utf-8")
    proc = _watch_run(art, "--once")
    assert proc.returncode == 2
    assert "refusing to kill" in proc.stderr
    assert stranger.poll() is None                     # never touched
    # no terminal written: either no train_status.json at all yet or a
    # non-terminal stage — never killed|done|failed
    status_path = art / "variants" / "r1-01" / "train_status.json"
    assert (not status_path.exists()
            or _train_status(art)["stage"] not in ("killed", "done", "failed"))
    assert (art / "devices" / "0.lock").is_file()
    assert _latest_row(art).get("outcome") == "latency_improved"


def test_watch_natural_completion_success(tmp_path):
    """rc == 0 -> the finalizer eval chain -> final_acc.json (null first) ->
    final-budget backfill -> success with the direction-normalized gap."""
    art = _watch_ws(tmp_path, epochs=5, log_epochs=range(1, 6),
                    candidate="0.85", rc=0)
    _write_final_metric_ckpts(art, 5, "final metric: 0.88\n")
    proc = _watch_run(art, "--once")
    assert proc.returncode == 0, proc.stderr
    status = _train_status(art)
    assert status["stage"] == "done"
    assert status["stopped_at_epoch"] == 5             # = E on success

    record = json.loads((art / "variants" / "r1-01" / "eval" / "final_acc.json")
                        .read_text(encoding="utf-8"))
    assert record["within_budget"] is True             # backfilled (§7.3)
    assert record["final_acc"] == 0.88
    assert record["baseline_full_acc"] == 0.9
    assert record["metric_direction"] == "higher_better"
    assert record["full_train_budget"] == {"epochs": 5, "seed": 7}

    row = _latest_row(art)
    assert row["outcome"] == "success"
    assert row["final_acc"] == 0.88
    assert row["gap"] == pytest.approx(0.02)           # 0.9 - 0.88
    assert row["stopped_at_epoch"] == 5
    assert _watch_shard(art)["status"] == "success"
    assert not (art / "devices" / "0.lock").exists()
    assert (art / "variants" / "r1-01" / ".rules_pending").is_file()


def test_watch_natural_completion_accuracy_fail(tmp_path):
    """A full-budget run whose final eval lands outside the anchor budget
    terminalizes accuracy_fail (stage done — the run itself completed)."""
    art = _watch_ws(tmp_path, epochs=5, log_epochs=range(1, 6),
                    candidate="0.85", rc=0)
    _write_final_metric_ckpts(art, 5, "final metric: 0.70\n")
    proc = _watch_run(art, "--once")
    assert proc.returncode == 0, proc.stderr
    assert _train_status(art)["stage"] == "done"
    record = json.loads((art / "variants" / "r1-01" / "eval" / "final_acc.json")
                        .read_text(encoding="utf-8"))
    assert record["within_budget"] is False
    row = _latest_row(art)
    assert row["outcome"] == "accuracy_fail"
    assert row["gap"] == pytest.approx(0.2)
    assert "final_acc" not in row                      # accuracy_fail extra
    assert not (art / "devices" / "0.lock").exists()


def test_watch_natural_completion_waits_for_baseline_anchor(tmp_path):
    """The baseline's full-acc anchor may still be pending (both trainings
    are asynchronous): the guardian WAITS, never judges against a guess."""
    art = _watch_ws(tmp_path, epochs=5, log_epochs=range(1, 6),
                    candidate="0.85", rc=0)
    _write_final_metric_ckpts(art, 5, "final metric: 0.88\n")
    (art / "baseline" / "baseline_full_acc.json").unlink()
    proc = _watch_run(art, "--once")
    assert proc.returncode == 0, proc.stderr
    assert _train_status(art)["stage"] == "waiting"    # not terminal
    assert _latest_row(art).get("outcome") == "latency_improved"
    assert (art / "devices" / "0.lock").is_file()      # card still held


def test_watch_nonzero_rc_is_probe_insufficient_train(tmp_path):
    """rc != 0 is an honest failure exit (baseline-finalizer policy): terminal
    probe_insufficient, no re-launch."""
    art = _watch_ws(tmp_path, epochs=5, log_epochs=range(1, 4), rc=1)
    proc = _watch_run(art, "--once")
    assert proc.returncode == 0, proc.stderr
    status = _train_status(art)
    assert status["stage"] == "failed"
    row = _latest_row(art)
    assert row["outcome"] == "probe_insufficient"
    assert row["stage"] == "train" and row["max_retries_hit"] is False
    assert not (art / "devices" / "0.lock").exists()
    attempts = art / "variants" / "r1-01" / "train" / ".train_attempts"
    assert not attempts.exists()


def test_watch_crash_without_rc_is_terminal_probe_insufficient(tmp_path):
    """v7 crash row: a group dying WITHOUT an rc file is TERMINAL — stage
    failed, outcome probe_insufficient (stage "crash"), the crash
    attribution + log paths disclosed in watchdog.log, the card released.
    (The v6 relaunch machinery is deleted: a crash closes the variant
    honestly instead of silently retraining from scratch.)"""
    art = _watch_ws(tmp_path, epochs=5)                # no log, no rc yet
    vd = art / "variants" / "r1-01"
    dead = _dead_pid()
    (vd / "train" / "train.pid").write_text(f"{dead}\n", encoding="utf-8")
    (vd / "train" / "train.log").write_text(
        _EPOCH_LINE.format(e=1, m="0.85") + "\n", encoding="utf-8")

    proc = _watch_run(art, "--once")
    assert proc.returncode == 0, proc.stderr
    status = _train_status(art)
    assert status["stage"] == "failed"
    row = _latest_row(art)
    assert row["outcome"] == "probe_insufficient"
    assert row["stage"] == "crash"
    assert row["max_retries_hit"] is False
    assert not (art / "devices" / "0.lock").exists()    # the card is freed
    # the crash attribution + log paths are DISCLOSED in watchdog.log
    log = (vd / "watchdog.log").read_text(encoding="utf-8")
    assert "stage=crash" in log and "train.log" in log
    assert _watch_shard(art)["status"] == "probe_insufficient"


def test_watch_sigterm_kills_group_writes_terminal_releases_card(tmp_path):
    """v7 SIGTERM row: the platform tearing the run down stops the training
    honestly — attribution-checked kill of the training group, terminal
    probe_insufficient (stage sigterm), card released, never an orphan."""
    art = _watch_ws(tmp_path, epochs=20, log_epochs=range(1, 3),
                    candidate="0.5")
    vd = art / "variants" / "r1-01"
    sleeper = _attributed_session_sleeper("train.rendered.sh")
    (vd / "train" / "train.pid").write_text(f"{sleeper.pid}\n",
                                            encoding="utf-8")
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    guardian = subprocess.Popen(
        [sys.executable, str(_WATCH_PY), "--vid", "r1-01", "--device", "0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        # wait for the guardian's startup + first cycle (watchdog.log)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            log = vd / "watchdog.log"
            if log.is_file() and "watchdog alive" in log.read_text(
                    encoding="utf-8"):
                break
            time.sleep(0.2)
        guardian.send_signal(signal.SIGTERM)
        sleeper.wait(timeout=20)                       # the group was killed
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if guardian.poll() is not None:
                break
            time.sleep(0.2)
        assert guardian.poll() is not None
        status = _train_status(art)
        assert status["stage"] == "killed"
        row = _latest_row(art)
        assert row["outcome"] == "probe_insufficient"
        assert row["stage"] == "sigterm"
        assert not (art / "devices" / "0.lock").exists()
        log = (vd / "watchdog.log").read_text(encoding="utf-8")
        assert "stage=sigterm" in log
    finally:
        if guardian.poll() is None:
            guardian.kill()


def test_watch_reentry_terminal_with_no_lock_is_a_clean_replay(tmp_path):
    """§7.6 third state: the lock is already gone — the replay must not try
    to release again (and must not error on the absent lock)."""
    art = _watch_ws(tmp_path, epochs=5, log_epochs=range(1, 6), rc=0,
                    with_lock=False)
    (art / "variants" / "r1-01" / "train_status.json").write_text(json.dumps(
        {"vid": "r1-01", "stage": "done", "epoch": 5, "metric": 0.85,
         "gap": 0.05, "over_budget_streak": 0, "stopped_at_epoch": 5,
         "device": 0, "ts": "2026-08-31T00:00:00+00:00"}), encoding="utf-8")
    proc = _watch_run(art, "--once")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["stage"] == "done"
    rows = history_lib.read_rows(art / "history.jsonl")
    assert all(r.get("outcome") != "success" for r in rows)  # nothing appended


def _epoch_log_lines(pairs) -> str:
    return "".join(_EPOCH_LINE.format(e=e, m=m) + "\n" for e, m in pairs)


def test_watch_within_budget_epoch_resets_the_streak(tmp_path):
    """The counting rule's other half: gap <= budget zeroes the streak —
    nine over, one within, nine over leaves streak 9 and NO kill (a
    regression to a monotone counter would pass the 9/10 tests and still
    break the fairness invariant this guard exists to enforce)."""
    art = _watch_ws(tmp_path, epochs=30)               # warmup = ceil(3) = 3
    td = art / "variants" / "r1-01" / "train"
    over, within = "0.5", "0.89"                       # baseline 0.9 budget 0.05
    curve = [(e, over) for e in range(4, 12)]          # eight over budget
    curve.append((12, within))                         # gap 0.01 -> RESET
    curve += [(e, over) for e in range(13, 21)]        # eight more over
    (td / "train.log").write_text(
        _epoch_log_lines([(e, "0.9") for e in (1, 2, 3)])
        + _epoch_log_lines(curve), encoding="utf-8")
    sleeper = _attributed_session_sleeper("train.rendered.sh")
    (td / "train.pid").write_text(f"{sleeper.pid}\n", encoding="utf-8")
    proc = _watch_run(art, "--once")
    assert proc.returncode == 0, proc.stderr
    status = _train_status(art)
    assert status["stage"] == "training"
    assert status["epoch"] == 20
    assert status["over_budget_streak"] == 8           # reset at epoch 12
    assert status["gap"] == pytest.approx(0.4)
    assert sleeper.poll() is None
    assert _latest_row(art).get("outcome") == "latency_improved"


def test_watch_lower_better_direction_early_stops(tmp_path):
    """lower_better is a user-configurable contract: the normalized loss
    flips (candidate ABOVE the baseline is the bad side) and the same
    streak/kill machinery applies."""
    art = _watch_ws(tmp_path, epochs=20, log_epochs=range(1, 9),
                    candidate="0.9", baseline="0.5",
                    direction="lower_better")
    sleeper = _attributed_session_sleeper("train.rendered.sh")
    (art / "variants" / "r1-01" / "train" / "train.pid").write_text(
        f"{sleeper.pid}\n", encoding="utf-8")
    proc = _watch_run(art, "--once")
    assert proc.returncode == 0, proc.stderr
    sleeper.wait(timeout=20)
    status = _train_status(art)
    assert status["stage"] == "killed"
    assert status["gap"] == pytest.approx(0.4)         # candidate - baseline
    row = _latest_row(art)
    assert row["outcome"] == "accuracy_fail"
    assert row["gap"] == pytest.approx(0.4)


def test_watch_extract_failure_cycle_keeps_recorded_progress(tmp_path):
    """A transient curve-parse failure must not silently reset the streak
    (that would postpone the early stop by whole budget segments): the
    failed cycle rewrites the status with the recorded progress kept."""
    art = _watch_ws(tmp_path, epochs=20, log_epochs=range(1, 8))   # streak 5
    td = art / "variants" / "r1-01" / "train"
    sleeper = _attributed_session_sleeper("train.rendered.sh")
    (td / "train.pid").write_text(f"{sleeper.pid}\n", encoding="utf-8")
    first = _watch_run(art, "--once")
    assert first.returncode == 0, first.stderr
    assert _train_status(art)["over_budget_streak"] == 5

    # the log turns unparseable mid-run (torn tail, wrong pattern, ...)
    (td / "train.log").write_text("torn mid-run garbage\n", encoding="utf-8")
    (art / "variants" / "r1-01" / "metrics" / "metrics.jsonl").unlink()
    second = _watch_run(art, "--once")
    assert second.returncode == 0, second.stderr
    status = _train_status(art)
    assert status["stage"] == "training"
    assert status["epoch"] == 7 and status["over_budget_streak"] == 5  # kept
    assert sleeper.poll() is None


def test_watch_waiting_resumes_when_anchor_lands_and_bounds_on_dead_baseline(tmp_path):
    """The waiting state is bounded twice over: the anchor landing resumes
    the normal terminal chain, and a baseline that reached ITS terminal
    state without ever producing the anchor closes the variant instead of
    holding the card forever."""
    art = _watch_ws(tmp_path, epochs=5, log_epochs=range(1, 6),
                    candidate="0.85", rc=0)
    _write_final_metric_ckpts(art, 5, "final metric: 0.88\n")
    anchor = art / "baseline" / "baseline_full_acc.json"
    anchor.unlink()
    # a recorded progress row exists (the B12 regression guard): the WAITING
    # rewrite must PRESERVE the last known epoch/metric/gap, not null them
    (art / "variants" / "r1-01" / "train_status.json").write_text(json.dumps(
        {"vid": "r1-01", "stage": "training", "epoch": 5, "metric": 0.85,
         "gap": 0.05, "over_budget_streak": 0, "stopped_at_epoch": None,
         "device": 0, "ts": "2026-09-01T00:00:00+00:00"}), encoding="utf-8")
    waiting = _watch_run(art, "--once")
    assert waiting.returncode == 0, waiting.stderr
    wstatus = _train_status(art)
    assert wstatus["stage"] == "waiting"
    assert wstatus["epoch"] == 5 and wstatus["metric"] == 0.85   # kept (B12)
    assert wstatus["gap"] == pytest.approx(0.05)

    # the anchor lands -> the next cycle runs the full final chain
    anchor.write_text(json.dumps(
        {"baseline_full_acc": 0.9, "ckpt": "baseline/last.pt",
         "full_train_budget": {"epochs": 5, "seed": 7}}), encoding="utf-8")
    resumed = _watch_run(art, "--once")
    assert resumed.returncode == 0, resumed.stderr
    assert _train_status(art)["stage"] == "done"
    assert _latest_row(art)["outcome"] == "success"

    # the dead-baseline bound: terminal train_final, anchor never coming
    art2 = _watch_ws(tmp_path / "b", epochs=5, log_epochs=range(1, 6),
                     candidate="0.85", rc=0)
    _write_final_metric_ckpts(art2, 5, "final metric: 0.88\n")
    (art2 / "baseline" / "baseline_full_acc.json").unlink()
    (art2 / "baseline" / "train_final.json").write_text(json.dumps(
        {"status": "failed", "rc": 1, "stage": "train"}), encoding="utf-8")
    dead = _watch_run(art2, "--once")
    assert dead.returncode == 0, dead.stderr
    status = _train_status(art2)
    assert status["stage"] == "failed"
    row = _latest_row(art2)
    assert row["outcome"] == "probe_insufficient"
    assert row["stage"] == "baseline_anchor_unavailable"
    assert not (art2 / "devices" / "0.lock").exists()   # the card is freed


def test_watch_final_check_epoch_mismatch_is_probe_insufficient(tmp_path):
    """rc == 0 but the log proves fewer epochs than the rendered budget: the
    fairness precondition of the final judgment is broken — close as
    probe_insufficient (final_check), never judge a short curve."""
    art = _watch_ws(tmp_path, epochs=5, log_epochs=range(1, 4),
                    candidate="0.85", rc=0)          # 3 epochs vs E=5
    _write_final_metric_ckpts(art, 5, "final metric: 0.88\n")
    proc = _watch_run(art, "--once")
    assert proc.returncode == 0, proc.stderr
    status = _train_status(art)
    assert status["stage"] == "failed"
    assert status["epoch"] == 3                       # the parsed shortfall
    row = _latest_row(art)
    assert row["outcome"] == "probe_insufficient"
    assert row["stage"] == "final_check"
    # no verdict was ever written over the short curve
    assert not (art / "variants" / "r1-01" / "eval" / "final_acc.json").exists()
    assert not (art / "devices" / "0.lock").exists()


def test_probe_emit_watchdog_liveness_negative_branches(tmp_path):
    """The restored watchdog check's two failure modes: a dead guardian with
    no terminal state (unsupervised training) and a recycled pid whose
    cmdline is not our guardian (pid reuse)."""
    dead = _dead_pid()
    art = _probe_ws(tmp_path, watchdog=False, liveness={})
    vd = art / "variants" / "r1-01"
    train_sleeper = _attributed_session_sleeper("train.rendered.sh")
    (vd / "train" / "train.pid").write_text(f"{train_sleeper.pid}\n",
                                            encoding="utf-8")
    (vd / "watchdog.pid").write_text(f"{dead}\n", encoding="utf-8")
    proc = _check_probe(art)
    assert proc.returncode == 1
    assert "unsupervised" in proc.stderr

    # a LIVE pid that is not our guardian (pid reuse) — never counted
    stranger = subprocess.Popen(["sleep", "300"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)
    _GUARDIANS.append(stranger)
    (vd / "watchdog.pid").write_text(f"{stranger.pid}\n", encoding="utf-8")
    reused = _check_probe(art)
    assert reused.returncode == 1
    assert "pid reuse" in reused.stderr

    # the terminal state redeems a dead guardian (it finished its job)
    (vd / "train_status.json").write_text(json.dumps(
        {"vid": "r1-01", "stage": "done", "epoch": 5, "metric": 0.85,
         "gap": 0.05, "over_budget_streak": 0, "stopped_at_epoch": 5,
         "device": 0, "ts": "2026-08-31T00:00:00+00:00"}), encoding="utf-8")
    redeemed = _check_probe(art)
    assert redeemed.returncode == 0, redeemed.stderr


# ── §13.2 scenario smokes: script-level sequences over the real scripts ──────

def test_scenario_single_variant_convergence_loop(tmp_path):
    """§13.2-1: ONE vid iterates until it improves the current incumbent.
    The recheck's repair ledger counts each miss, no second vid ever
    appears, and the rerouting signal (direction.json) stays absent — the
    elimination path belongs to the >= 5 budget, not this round."""
    art = _recheck_ws(tmp_path, target=500)
    _recheck_variant(art, "r1-01", 1100)
    vd = art / "variants" / "r1-01"

    def _remeasure(makespan: int) -> dict:
        _write_raw_profile(vd / "profile", makespan)
        verdict = vd / "verdict.json"
        if verdict.is_file():
            verdict.unlink()
        proc = _run_recheck(art)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    out = _remeasure(1100)
    assert out["latency_improved_count"] == 0
    trace = json.loads((vd / "repair_trace.json").read_text(encoding="utf-8"))
    assert trace["repair_count"] == 1 and len(trace["attempts"]) == 1

    out = _remeasure(1000)                      # equal incumbent still fails
    assert out["latency_improved_count"] == 0
    trace = json.loads((vd / "repair_trace.json").read_text(encoding="utf-8"))
    assert trace["repair_count"] == 2

    out = _remeasure(999)                       # first strict improvement
    assert out["latency_improved_count"] == 1
    trace = json.loads((vd / "repair_trace.json").read_text(encoding="utf-8"))
    assert trace["repair_count"] == 2           # a pass appends no attempt
    verdict = json.loads((vd / "verdict.json").read_text(encoding="utf-8"))
    assert verdict["outcome"] == "latency_improved"

    # the SAME vid carries the whole loop; no elimination artifacts
    latest = history_lib.read_latest(art / "history.jsonl")
    assert set(latest) == {"r1-01"}
    assert latest["r1-01"]["outcome"] == "latency_improved"
    assert not (art / "rounds" / "001" / "direction.json").exists()


def test_scenario_two_cards_two_variants_parallel_block_release(tmp_path):
    """§13.2-2: on a synthetic two-card backend two variants hold the two
    cards in PARALLEL; a third entry meets the full house ({"ok": false} —
    exactly the condition the probe parks on); one terminal release frees
    its card and the next claim takes THAT card."""
    art = tmp_path / "ws"
    _write_train_device(art, count=2)
    v1 = json.loads(_alloc(art, "claim", "--vid", "r1-01", "--idx", "0").stdout)
    v2 = json.loads(_alloc(art, "claim", "--vid", "r2-01", "--idx", "1").stdout)
    assert v1["ok"] is True and v1["idx"] == 0
    assert v2["ok"] is True and v2["idx"] == 1     # parallel, both held

    # the park condition: the agent-chosen idx already locked (holder named)
    busy = json.loads(_alloc(art, "claim", "--vid", "r3-01", "--idx", "0").stdout)
    assert busy["ok"] is False and "r1-01" in busy["reason"]

    rel = _alloc(art, "release", "--idx", str(v1["idx"]))
    assert rel.returncode == 0, rel.stderr         # r1-01 reached a terminal
    v3 = json.loads(_alloc(art, "claim", "--vid", "r3-01", "--idx", "0").stdout)
    assert v3["ok"] is True and v3["idx"] == 0     # the freed card
    held = sorted(json.loads((art / "devices" / f"{i}.lock").read_text(
        encoding="utf-8"))["vid"] for i in (0, 1))
    assert held == ["r2-01", "r3-01"]


def test_scenario_streaming_early_stop_end_to_end(tmp_path):
    """§13.2-3: curve growth through the REAL watchdog — warmup epochs are
    seen but never judged, the streak builds to 10, the kill lands on the
    attributed group, the terminal (train_status / history / shard / lock /
    rules marker) lands on disk, and the derived dashboard reflects it."""
    art = _watch_ws(tmp_path, epochs=20, log_epochs=range(1, 3),
                    candidate="0.5")
    vd = art / "variants" / "r1-01"
    sleeper = _attributed_session_sleeper("train.rendered.sh")
    (vd / "train" / "train.pid").write_text(f"{sleeper.pid}\n",
                                            encoding="utf-8")

    warm = _watch_run(art, "--once")               # warmup = epochs <= 2
    assert warm.returncode == 0, warm.stderr
    status = _train_status(art)
    assert status["stage"] == "training" and status["over_budget_streak"] == 0
    assert sleeper.poll() is None

    with open(vd / "train" / "train.log", "a", encoding="utf-8") as fh:
        for e in range(3, 9):                      # judged -> streak 6 (E=20)
            fh.write(_EPOCH_LINE.format(e=e, m="0.5") + "\n")
    kill = _watch_run(art, "--once")
    assert kill.returncode == 0, kill.stderr
    sleeper.wait(timeout=20)

    status = _train_status(art)
    assert status["stage"] == "killed"
    assert status["over_budget_streak"] == 6
    assert status["stopped_at_epoch"] == 8
    row = _latest_row(art)
    assert row["outcome"] == "accuracy_fail"
    assert row["stopped_at_epoch"] == 8 and row["gap"] == pytest.approx(0.4)
    assert not (art / "devices" / "0.lock").exists()
    assert (vd / ".rules_pending").is_file()

    proc = _run_cli([sys.executable, str(_SCRIPTS / "dashboard_snapshot.py"),
                     "--artifacts", str(art)])
    assert proc.returncode == 0, proc.stderr
    dash = json.loads((art / "dashboard.json").read_text(encoding="utf-8"))
    row = next(r for r in dash["variants"] if r["vid"] == "r1-01")
    assert row["status"] == "accuracy_fail"
    assert "r1-01" in dash["curves"]               # the curve file survived


def test_scenario_gate_report_exit_and_round_cap(tmp_path):
    """§13.2-4: a success row exits the loop to report EVEN while another
    variant is still in flight (eligibility kept, nothing killed); at the
    round cap with no success the gate still reports and names the vids the
    terminal harvest must await — and the in-flight vid's train_status is
    exactly the non-terminal stage the report parks on."""
    art = tmp_path / "ws"
    _write_anchor(art, target=500)
    for rnd in (1, 2, 3):
        (art / "rounds" / f"{rnd:03d}").mkdir(parents=True)
    hist = art / "history.jsonl"
    _impl(hist, "r1-01", "sig:a")                  # in flight, no terminal
    history_lib.append_latency(hist, "r1-01", structural_check="pass",
                               makespan_cycles=460, latency_gate="pass",
                               pred_actual_ratio=None, outcome="latency_improved")
    _impl(hist, "r2-01", "sig:b")
    history_lib.append_latency(hist, "r2-01", structural_check="pass",
                               makespan_cycles=480, latency_gate="pass",
                               pred_actual_ratio=None, outcome="latency_improved")
    history_lib.append_terminal(hist, "r2-01", outcome="success", gap=0.02,
                                stopped_at_epoch=20, final_acc=0.88)
    (art / "variants" / "r1-01").mkdir(parents=True, exist_ok=True)
    (art / "variants" / "r1-01" / "train_status.json").write_text(json.dumps(
        {"vid": "r1-01", "stage": "training", "epoch": 7, "metric": 0.8,
         "gap": 0.1, "over_budget_streak": 0, "stopped_at_epoch": None,
         "device": 1, "ts": "2026-09-01T00:00:00+00:00"}), encoding="utf-8")

    out = decide(art, max_rounds=100)
    assert out["decision"] == "report"
    assert out["success_vids"] == ["r2-01"]
    assert out["in_flight"] == ["r1-01"]           # kept eligible, not killed

    cap = tmp_path / "cap-ws"
    _write_anchor(cap, target=500)
    for rnd in (1, 2):
        (cap / "rounds" / f"{rnd:03d}").mkdir(parents=True)
    hist2 = cap / "history.jsonl"
    _impl(hist2, "r1-01", "sig:a")
    history_lib.append_latency(hist2, "r1-01", structural_check="pass",
                               makespan_cycles=460, latency_gate="pass",
                               pred_actual_ratio=None, outcome="latency_improved")
    capped = decide(cap, max_rounds=2)
    assert capped["decision"] == "report"          # the cap never loops
    assert capped["success_vids"] == []
    assert capped["in_flight"] == ["r1-01"]
    assert "awaits" in capped["reason"]
