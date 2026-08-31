"""test_po_v6.py — v6 mechanism tests (single-variant convergence redesign).

Script-level unit tests for the v6 P0 mechanics: the training-device
resolver (four-level first-match-wins + write-if-absent + reuse-mismatch
fail-loud), the device allocation ledger (O_EXCL acquire / free = the
complement of real occupancy UNION live locks, dead-pid recycling with
disclosure / idempotent release / full-house {"ok": false}), round_state
working = current + 1, the gate decision order (success -> report / round
cap -> report / loop, with in_flight), history append_terminal row
semantics + the unchanged (vid, change_sig) dedup key, the unified latency
recheck boundary (makespan == target passes), the ledger aggregator's
purity (same shard set -> same output, full rebuildability), and the
deployed-set stamp roundtrip over the three new scripts.

Device-facing cases are fully synthetic (PATH stubs / hand-written lock
files) — no real GPU/NPU is ever required.
"""
from __future__ import annotations

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
from gate_decide import decide  # noqa: E402

_BASH = shutil.which("bash") or "/bin/bash"
_RECHECK_SH = (_REPO / "workflows" / "prof-opt" / "agents" / "po_propose"
               / "scripts" / "run_latency_recheck.sh")
_RESOLVE_SH = _SCRIPTS / "resolve_train_device.sh"
_ALLOC_PY = _SCRIPTS / "device_alloc.py"
_LEDGER_AGG = _SCRIPTS / "ledger_aggregate.py"
_LEDGER_PY = _SCRIPTS / "experiment_ledger.py"
_DEPLOY_SH = _SCRIPTS / "deploy_scripts.sh"


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


# ── device_alloc: acquire / release / full house ──────────────────────────────

def test_device_alloc_acquire_is_exclusive_and_fills_in_order(tmp_path):
    art = tmp_path / "ws"
    _write_train_device(art, count=2)
    first = json.loads(_alloc(art, "acquire", "--vid", "r1-01").stdout)
    assert first["ok"] is True and first["idx"] == 0
    lock = json.loads((art / "devices" / "0.lock").read_text(encoding="utf-8"))
    assert lock["vid"] == "r1-01" and lock["backend"] == "cuda"
    assert isinstance(lock["pid"], int) and "acquired_at" in lock

    second = json.loads(_alloc(art, "acquire", "--vid", "r2-01").stdout)
    assert second["ok"] is True and second["idx"] == 1

    # all devices locked -> {"ok": false} is a WAIT state, not an error (rc 0)
    full = _alloc(art, "acquire", "--vid", "r3-01")
    assert full.returncode == 0, full.stderr
    payload = json.loads(full.stdout)
    assert payload["ok"] is False and payload["locked"] == [0, 1]


def test_device_alloc_acquire_skips_dead_lock_without_recycling(tmp_path):
    """Acquire never recycles (that is free's job): a dead-pid lock still
    blocks its index — double-checking the division of labor the spec pins."""
    art = tmp_path / "ws"
    _write_train_device(art, count=2)
    _write_lock(art, 0, "r0-99", _dead_pid())
    out = json.loads(_alloc(art, "acquire", "--vid", "r1-01").stdout)
    assert out["ok"] is True and out["idx"] == 1
    assert (art / "devices" / "0.lock").is_file()   # untouched by acquire


def test_device_alloc_acquire_with_live_owner_pid_survives_free(tmp_path):
    """A lock acquired with an explicit long-lived owner pid (--pid) is NOT
    reclaimed by a later free while that owner lives — the ledger's mutual
    exclusion must not rest on the short-lived acquirer process alone."""
    art = tmp_path / "ws"
    _write_train_device(art, count=2)
    out = json.loads(_alloc(art, "acquire", "--vid", "r1-01",
                             "--pid", str(os.getpid())).stdout)
    assert out["ok"] is True and out["pid"] == os.getpid()
    env = _stub_env(tmp_path, tools={
        "nvidia-smi": _NVIDIA_SMI_IDLE_STUB.format(bash=_BASH)})
    proc = _alloc(art, "free", env=env)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["recycled"] == [] and payload["locked"] == [out["idx"]]
    assert payload["free"] == [i for i in (0, 1) if i != out["idx"]]


def test_device_alloc_release_is_idempotent(tmp_path):
    art = tmp_path / "ws"
    _write_train_device(art, count=1)
    _write_lock(art, 0, "r1-01", os.getpid())
    first = _alloc(art, "release", "--idx", "0")
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout) == {"released": True, "idx": 0}
    assert not (art / "devices" / "0.lock").exists()

    again = _alloc(art, "release", "--idx", "0")
    assert again.returncode == 0, again.stderr       # §7.6: no double release
    payload = json.loads(again.stdout)
    assert payload["released"] is False and payload["idx"] == 0


def test_device_alloc_missing_train_device_fails_loud(tmp_path):
    proc = _alloc(tmp_path / "ws", "acquire", "--vid", "r1-01")
    assert proc.returncode == 2
    assert "train_device.json" in proc.stderr


# ── device_alloc free: real occupancy UNION live locks, dead-pid recycling ────

_NVIDIA_SMI_STUB = r"""#!{bash}
if [ "${{1:-}}" = "-L" ]; then
  printf 'GPU 0: stub\nGPU 1: stub\n'
elif [ "${{1:-}}" = "--query-gpu=index,uuid" ]; then
  printf '0, GPU-aa\n1, GPU-bb\n'
elif [ "${{1:-}}" = "--query-compute-apps=gpu_uuid,pid" ]; then
  printf 'GPU-aa, 4242\n'
else
  echo "unsupported query: $*" >&2; exit 9
fi
"""

# same machine, but every GPU idle (no compute apps)
_NVIDIA_SMI_IDLE_STUB = r"""#!{bash}
if [ "${{1:-}}" = "-L" ]; then
  printf 'GPU 0: stub\nGPU 1: stub\n'
elif [ "${{1:-}}" = "--query-gpu=index,uuid" ]; then
  printf '0, GPU-aa\n1, GPU-bb\n'
elif [ "${{1:-}}" = "--query-compute-apps=gpu_uuid,pid" ]; then
  :
else
  echo "unsupported query: $*" >&2; exit 9
fi
"""

_NPU_SMI_INFO_TABLE = """+----------------------------------------------------+
| NPU | Name  | Health | Process                      |
|===  | ===== | ====== | ============================ |
| 0   | 910B3 | OK     | 4242                         |
| 1   | 910B3 | OK     | -                            |
+----------------------------------------------------+
"""


def _stub_env(tmp_path: Path, *, tools: dict[str, str],
              real_tools: str = "python3 dirname grep sed head mkdir") -> dict:
    """PATH holding ONLY the named tool stubs plus the few real utilities the
    scripts need — backend-tool presence/absence is then deterministic."""
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    for name, body in tools.items():
        stub = stub_dir / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
    for name in real_tools.split():
        link = stub_dir / name
        if not link.exists():
            target = shutil.which(name)
            if target:
                link.symlink_to(target)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ORCA_PO_DEVICE")}
    env["PATH"] = str(stub_dir)
    return env


def test_device_alloc_free_unions_real_occupancy_and_live_locks(tmp_path):
    art = tmp_path / "ws"
    _write_train_device(art, backend="cuda", count=2)
    # device 0: really busy (a foreign process), no lock; device 1: live lock
    _write_lock(art, 1, "r1-01", os.getpid())
    env = _stub_env(tmp_path, tools={
        "nvidia-smi": _NVIDIA_SMI_STUB.format(bash=_BASH)})
    proc = _alloc(art, "free", env=env)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["free"] == []                      # 0 busy-real, 1 live-locked
    assert out["busy_real"] == [0] and out["locked"] == [1]


def test_device_alloc_free_recycles_dead_pid_with_disclosure(tmp_path):
    art = tmp_path / "ws"
    _write_train_device(art, backend="cuda", count=2)
    env = _stub_env(tmp_path, tools={
        "nvidia-smi": _NVIDIA_SMI_IDLE_STUB.format(bash=_BASH)})
    dead = _dead_pid()
    live = _write_lock(art, 1, "r1-01", os.getpid())
    dead_lock = _write_lock(art, 0, "r0-99", dead)
    proc = _alloc(art, "free", env=env)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    # the dead-pid lock was reclaimed (and disclosed), the live one kept
    assert not dead_lock.exists() and live.is_file()
    assert [r["idx"] for r in out["recycled"]] == [0]
    assert out["recycled"][0]["vid"] == "r0-99" and out["recycled"][0]["pid"] == dead
    assert "dead" in out["recycled"][0]["reason"]
    assert out["free"] == [0] and out["locked"] == [1]


def test_device_alloc_free_npu_process_column_parse(tmp_path):
    art = tmp_path / "ws"
    _write_train_device(art, backend="npu", count=2)
    env = _stub_env(tmp_path, tools={
        "npu-smi": f"#!{_BASH}\nprintf '%s' '{_NPU_SMI_INFO_TABLE}'\n"})
    proc = _alloc(art, "free", env=env)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["free"] == [1] and out["busy_real"] == [0]


def test_device_alloc_free_fails_loud_on_unusable_backend_probe(tmp_path):
    art = tmp_path / "ws"
    _write_train_device(art, backend="cuda", count=1)
    # train_device.json says cuda but no nvidia-smi anywhere -> never a
    # guessed free set
    env = _stub_env(tmp_path, tools={})
    proc = _alloc(art, "free", env=env)
    assert proc.returncode == 2
    assert "occupancy" in proc.stderr

    # npu table without a Process column -> same honesty
    art2 = tmp_path / "ws2"
    _write_train_device(art2, backend="npu", count=1)
    env2 = _stub_env(tmp_path / "b", tools={
        "npu-smi": "#!/bin/sh\nprintf '| NPU  Name |\\n| 0  910B3 |\\n'\n"})
    proc2 = _alloc(art2, "free", env=env2)
    assert proc2.returncode == 2
    assert "Process column" in proc2.stderr


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
        {"vid": "r1-01", "round": 1, "outcome": "latency_pass",
         "makespan_cycles": 450},
        {"vid": "r1-01", "round": 1, "outcome": "success",
         "makespan_cycles": 450, "gap": 0.02, "stopped_at_epoch": 10,
         "final_acc": 0.91},
        {"vid": "r2-01", "round": 2, "outcome": "latency_pass",
         "makespan_cycles": 460},
    ]
    art = _gate_ws(tmp_path, rounds=[1, 2], history_rows=rows)
    out = decide(art, max_rounds=1)        # cap reached AND success present
    assert out["decision"] == "report"     # branch 1 wins over the cap
    assert out["success_vids"] == ["r1-01"]
    assert out["in_flight"] == ["r2-01"]   # passed but not terminal
    assert out["round"] == 2 and out["target_cycles"] == 501
    assert set(out) == {"decision", "round", "target_cycles",
                        "success_vids", "in_flight", "reason"}


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
        {"vid": "r1-01", "round": 1, "outcome": "latency_pass",
         "makespan_cycles": 450},
        {"vid": "r1-02", "round": 1, "outcome": "latency_pass",
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
    rows = [{"vid": f"r{i}-01", "round": 1, "outcome": "latency_pass"}
            for i in range(4)]
    rows += [{"vid": f"r{i}-01", "round": 1, "outcome": o}
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

def _impl(hist: Path, vid: str, sig: str, *, probe_epochs: int = 1,
          probe_max_steps=None, probe_data_value=None) -> None:
    history_lib.append_implemented(
        hist, vid, round=1, seq=1, parent_vid=None, change_sig=sig,
        probe_epochs=probe_epochs, probe_max_steps=probe_max_steps,
        probe_data_value=probe_data_value, target_modules=["m"],
        predicted_delta_cycles=-100,
        base_at_proposal={"vid": None, "makespan_cycles": 1000})


def test_append_terminal_success_row_semantics(tmp_path):
    hist = tmp_path / "history.jsonl"
    _impl(hist, "r1-01", "activation:gelu->relu:m")
    history_lib.append_latency(hist, "r1-01", structural_check="pass",
                               makespan_cycles=450, latency_gate="pass",
                               pred_actual_ratio=1.0, outcome="latency_pass")
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
    _impl(hist, "r1-01", "norm:relax:m", probe_epochs=1,
          probe_max_steps=500, probe_data_value=2000)
    history_lib.append_terminal(hist, "r1-01", outcome="probe_insufficient",
                                stage="liveness", max_retries_hit=True)
    assert history_lib.dedup_state(hist, "norm:relax:m", 1, 500,
                                   2000)["blocked"] is True
    assert history_lib.dedup_state(hist, "norm:relax:m", 2, 500,
                                   2000)["blocked"] is False

    hist2 = tmp_path / "h2.jsonl"
    _impl(hist2, "r1-01", "act:swap:m")
    history_lib.append_terminal(hist2, "r1-01", outcome="latency_fail",
                                measured_makespan_cycles=900)
    assert history_lib.dedup_state(hist2, "act:swap:m", 1,
                                   None)["blocked"] is False


# ── run_latency_recheck: the unified gate boundary (== target passes) ─────────

def _recheck_ws(tmp_path: Path, *, target: int = 500) -> Path:
    """mfu-mode fixture: the recheck consumes each variant's four-piece
    makespan verbatim, so the two variants pin the gate boundary exactly."""
    pytest.importorskip("onnx")
    import onnx
    from onnx import TensorProto, helper
    art = tmp_path / "ws"
    (art / "scripts").mkdir(parents=True)
    for src in ("diff_check.py", "history_lib.py", "emit_result.py",
                "round_state.py"):
        shutil.copy(_SCRIPTS / src, art / "scripts" / src)
    (art / "contracts.json").write_text(json.dumps(
        {"interpreter": {"sys_executable": sys.executable}}), encoding="utf-8")
    (art / "profile_mode.json").write_text(json.dumps(
        {"mode": "mfu", "chip": "6613", "precision": "INT8", "core_num": 1,
         "resolved_by": "env"}), encoding="utf-8")
    (art / "base" / "profile").mkdir(parents=True)
    (art / "base" / "profile" / "profile_summary.json").write_text(json.dumps(
        {"schema_version": 1, "onnx": "smoke.onnx",
         "makespan_cycles": 1000, "op_count": 2}), encoding="utf-8")
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
    (vd / "profile").mkdir(parents=True)
    (vd / "profile" / "profile_summary.json").write_text(json.dumps(
        {"schema_version": 1, "onnx": "smoke.onnx",
         "makespan_cycles": makespan, "op_count": 2}), encoding="utf-8")
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


def test_recheck_unified_gate_boundary_equal_passes(tmp_path):
    art = _recheck_ws(tmp_path, target=500)
    _recheck_variant(art, "r1-01", 500)   # exactly ON the line
    _recheck_variant(art, "r1-02", 501)   # one cycle above
    proc = _run_recheck(art)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["status"] == "executed"
    assert out["target_cycles"] == 500
    assert out["latency_pass_count"] == 1
    assert out["summary"] == "2 verdicts [latency_pass=1 latency_fail=1]"

    boundary = json.loads((art / "variants" / "r1-01" / "verdict.json")
                          .read_text(encoding="utf-8"))
    assert boundary["outcome"] == "latency_pass"
    assert boundary["latency_gate"] == "pass"
    assert boundary["target_cycles"] == 500
    above = json.loads((art / "variants" / "r1-02" / "verdict.json")
                       .read_text(encoding="utf-8"))
    assert above["outcome"] == "latency_fail" and above["latency_gate"] == "fail"
    # best.json / mode no longer feed the gate: no mode field is emitted
    assert "gate_mode" not in out and "gate_mode" not in boundary
    latest = history_lib.read_latest(art / "history.jsonl")
    assert latest["r1-01"]["outcome"] == "latency_pass"
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
