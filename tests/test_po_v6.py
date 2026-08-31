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
    _recheck_variant(art, "r1-01", 600)          # above the line -> latency_fail
    _seed_repair_trace(art, "r1-01", 4)
    (art / "variants" / "r1-01" / "verdict.json").unlink(missing_ok=True)
    proc = _run_recheck(art)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["latency_pass_count"] == 0
    trace = json.loads((art / "variants" / "r1-01" / "repair_trace.json")
                       .read_text(encoding="utf-8"))
    assert trace["repair_count"] == 5 and len(trace["attempts"]) == 5
    assert trace["attempts"][-1]["measured_makespan_cycles"] == 600
    assert trace["attempts"][-1]["gap_cycles"] == 100
    assert trace["attempts"][-1]["target_cycles"] == 500


def test_repair_trace_fifth_failure_is_terminal_sixth_fails_loud(tmp_path):
    art = _recheck_ws(tmp_path, target=500)
    _recheck_variant(art, "r1-01", 600)
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
    _recheck_variant(art2, "r1-01", 600)
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
    _recheck_variant(art, "r1-01", 600)
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


# ── P1: check_propose_emit v6 — §5.3 gate over both ending paths ───────────────

_CHECK_EMIT = _SCRIPTS / "check_propose_emit.py"
_BL_SENTINEL = "[subagent:business-logic-analyst v1 BLA7K4]"
_INFO_SENTINEL = "[subagent:information-analyst v1 IXA3N7]"
_BL_SECTIONS = ("## 任务语义", "## 输入输出", "## 架构动机",
                "## 逐模块职责与物理意义", "## 训练目标与指标方向", "## 与基线差异")
_INFO_SECTIONS = ("## 信息核心", "## 近似与牺牲项", "## 被牺牲信息与预期精度代价")


def _variant_doc(sentinel: str, sections: tuple[str, ...],
                 drop: str = "", bare: bool = False) -> str:
    lines = [sentinel]
    for heading in sections:
        if heading == drop:
            continue
        lines.append(heading)
        if not bare:
            lines.append(f"content for {heading}")
    return "\n".join(lines) + "\n"


def _emit_ws(tmp_path: Path, *, outcome: str = "latency_pass",
             delta: int = -600) -> Path:
    """A green single-variant round: one admitted proposal, both §4.1 analyst
    documents + conformance, the history rows, and the round analysis."""
    art = tmp_path / "ws"
    (art / "scripts").mkdir(parents=True)
    for src in ("history_lib.py", "round_state.py"):
        shutil.copy(_SCRIPTS / src, art / "scripts" / src)
    (art / "profile_mode.json").write_text(json.dumps(
        {"mode": "mfu", "chip": "6613", "precision": "INT8", "core_num": 1}),
        encoding="utf-8")
    (art / "base" / "profile").mkdir(parents=True)
    (art / "base" / "profile" / "profile_summary.json").write_text(json.dumps(
        {"makespan_cycles": 1000, "op_count": 2}), encoding="utf-8")
    _write_anchor(art, target=500)

    rd = art / "rounds" / "001"
    rd.mkdir(parents=True)
    (rd / "proposals.json").write_text(json.dumps({
        "round": 1, "exhausted": False, "filtered_count": 0,
        "exhausted_rationale": [],
        "proposals": [{"vid": "r1-01", "lever": "activation",
                       "change_sig": "sig:r1-01", "target_modules": ["m"],
                       "target_pattern_id": "low-mfu-matmul",
                       "rationale": "why", "change_spec": "edit",
                       "op_delta": {"Erf": -4, "Relu": 4},
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
        "## latency\nreached the line; calibration note; next direction\n",
        encoding="utf-8")

    vd = art / "variants" / "r1-01"
    vd.mkdir(parents=True)
    (vd / "business_logic.md").write_text(
        _variant_doc(_BL_SENTINEL, _BL_SECTIONS), encoding="utf-8")
    (vd / "information_analysis.md").write_text(
        _variant_doc(_INFO_SENTINEL, _INFO_SECTIONS), encoding="utf-8")
    (vd / "conformance.md").write_text(
        f"# conformance — r1-01\n{_BL_SENTINEL} verified\n"
        f"{_INFO_SENTINEL} verified\n## 对齐结论\naligned\n## 差异披露\nnone\n",
        encoding="utf-8")

    hist = art / "history.jsonl"
    _impl(hist, "r1-01", "sig:r1-01")
    if outcome == "latency_pass":
        history_lib.append_latency(hist, "r1-01", structural_check="pass",
                                   makespan_cycles=400, latency_gate="pass",
                                   pred_actual_ratio=None,
                                   outcome="latency_pass")
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
    """The success path (latency_pass) and the honest elimination path
    (latency_fail after repair exhaustion) both pass the §5.3 gate — and
    neither needs the retired mode_state machinery (P0's dangling reference
    is gone: no mode is read anywhere)."""
    for outcome in ("latency_pass", "latency_fail"):
        art = _emit_ws(tmp_path / outcome, outcome=outcome)
        proc = _check_emit(art)
        assert proc.returncode == 0, (outcome, proc.stderr)
        assert json.loads(proc.stdout)["ok"] is True


def test_emit_gate_rejects_multi_proposal_and_above_line_prediction(tmp_path):
    art = _emit_ws(tmp_path / "many")
    proposals_path = art / "rounds" / "001" / "proposals.json"
    doc = json.loads(proposals_path.read_text(encoding="utf-8"))
    doc["proposals"].append(dict(doc["proposals"][0], vid="r1-02",
                                 change_sig="sig:r1-02"))
    proposals_path.write_text(json.dumps(doc), encoding="utf-8")
    proc = _check_emit(art)
    assert proc.returncode == 1
    assert "exactly ONE" in proc.stderr

    # prediction above the frozen line never admits (900 > 500)
    art2 = _emit_ws(tmp_path / "above", delta=-100)
    proc2 = _check_emit(art2)
    assert proc2.returncode == 1
    assert "admission" in proc2.stderr

    # exactly ON the line is admissible (inclusive boundary)
    art3 = _emit_ws(tmp_path / "on-line", delta=-500)
    assert _check_emit(art3).returncode == 0


def test_emit_gate_conformance_matrix(tmp_path):
    """§4.1 document gate: sentinel / non-empty body / conclusion section
    missing -> intercepted; all present -> admitted."""
    def break_doc(art: Path, name: str, content: str) -> None:
        (art / "variants" / "r1-01" / name).write_text(content, encoding="utf-8")

    # sentinel broken
    art = _emit_ws(tmp_path / "s")
    break_doc(art, "business_logic.md",
              _variant_doc("wrong sentinel", _BL_SECTIONS))
    proc = _check_emit(art)
    assert proc.returncode == 1 and "sentinel" in proc.stderr

    # body empty (sentinel only)
    art = _emit_ws(tmp_path / "e")
    break_doc(art, "information_analysis.md", _INFO_SENTINEL + "\n")
    proc = _check_emit(art)
    assert proc.returncode == 1 and "empty" in proc.stderr

    # conclusion section missing (business: 与基线差异)
    art = _emit_ws(tmp_path / "c1")
    break_doc(art, "business_logic.md",
              _variant_doc(_BL_SENTINEL, _BL_SECTIONS, drop="## 与基线差异"))
    proc = _check_emit(art)
    assert proc.returncode == 1 and "与基线差异" in proc.stderr

    # conclusion section missing (information: 被牺牲信息与预期精度代价)
    art = _emit_ws(tmp_path / "c2")
    break_doc(art, "information_analysis.md",
              _variant_doc(_INFO_SENTINEL, _INFO_SECTIONS,
                           drop="## 被牺牲信息与预期精度代价"))
    proc = _check_emit(art)
    assert proc.returncode == 1 and "被牺牲信息与预期精度代价" in proc.stderr

    # conformance.md empty / not recording both sentinels
    art = _emit_ws(tmp_path / "cf1")
    break_doc(art, "conformance.md", "")
    proc = _check_emit(art)
    assert proc.returncode == 1 and "conformance" in proc.stderr

    art = _emit_ws(tmp_path / "cf2")
    break_doc(art, "conformance.md",
              f"# conformance\n{_BL_SENTINEL} verified\nno info record\n")
    proc = _check_emit(art)
    assert proc.returncode == 1 and _INFO_SENTINEL in proc.stderr


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
    for outcome in ("latency_pass", "latency_fail"):
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


def test_emit_gate_history_and_admission_input_failures(tmp_path):
    # no history row for the round's vid -> intercepted
    art = _emit_ws(tmp_path / "nohist")
    (art / "history.jsonl").unlink()
    proc = _check_emit(art)
    assert proc.returncode == 1 and "no history row" in proc.stderr

    # an impl row without a latency-stage outcome is not a legal ending
    art2 = _emit_ws(tmp_path / "nooutcome")
    hist = art2 / "history.jsonl"
    hist.unlink()
    history_lib.append_implemented(
        hist, "r1-01", round=1, seq=1, parent_vid=None,
        change_sig="sig:r1-01", probe_epochs=1, probe_max_steps=None,
        probe_data_value=None, target_modules=["m"],
        predicted_delta_cycles=-600,
        base_at_proposal={"vid": None, "makespan_cycles": 1000})
    proc2 = _check_emit(art2)
    assert proc2.returncode == 1 and "legal round ending" in proc2.stderr

    # the admission line needs both single sources (base summary + anchor)
    art3 = _emit_ws(tmp_path / "noanchor")
    (art3 / "base" / "origin_anchor.json").unlink()
    proc3 = _check_emit(art3)
    assert proc3.returncode == 1 and "target_cycles unavailable" in proc3.stderr


def test_emit_gate_zero_proposal_round_is_legal(tmp_path):
    art = _emit_ws(tmp_path / "z")
    rd = art / "rounds" / "001"
    shutil.rmtree(art / "variants")
    (rd / "verdicts.jsonl").unlink()
    (rd / "proposals.json").write_text(json.dumps({
        "round": 1, "exhausted": False, "filtered_count": 1,
        "exhausted_rationale": [{"lever": "activation", "direction": "x",
                                 "why_not": "prediction above the target line"}],
        "proposals": []}), encoding="utf-8")
    proc = _check_emit(art)
    assert proc.returncode == 0, proc.stderr

    # but a bare zero-proposal round without rationale is rejected
    doc = json.loads((rd / "proposals.json").read_text(encoding="utf-8"))
    doc["exhausted_rationale"] = []
    (rd / "proposals.json").write_text(json.dumps(doc), encoding="utf-8")
    assert _check_emit(art).returncode == 1


# ── P2: flatten→probe wiring smoke + check_probe_emit v6 (§6.2) ───────────────

_PROBE_EMIT = _SCRIPTS / "check_probe_emit.py"
_WATCH_SH = _SCRIPTS / "watch_variant.sh"


def test_flatten_to_probe_resource_chain_smoke(tmp_path):
    """The entry→probe resource wiring over mocked backend CLIs: the entry
    resolver freezes train_device.json, and the probe-side ledger turns the
    same backend facts into claim / mutual exclusion / release."""
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

    # probe side: claim (free -> acquire -> idx-in-free-set guard) then adopt
    # a long-lived owner — the lock must survive free (mutual exclusion rests
    # on the OWNER, never on the short-lived claiming command)
    free1 = _alloc(art, "free", env=env)
    assert free1.returncode == 0, free1.stderr
    assert json.loads(free1.stdout)["free"] == [0, 1]
    claimed = _alloc(art, "claim", "--vid", "r1-01", env=env)
    assert claimed.returncode == 0, claimed.stderr
    doc = json.loads(claimed.stdout)
    assert doc["ok"] is True and doc["idx"] == 0
    adopted = _alloc(art, "adopt", "--vid", "r1-01",
                     "--pid", str(os.getpid()), env=env)
    assert adopted.returncode == 0, adopted.stderr
    assert json.loads(adopted.stdout) == {"adopted": True, "idx": 0,
                                          "vid": "r1-01", "pid": os.getpid()}
    free2 = json.loads(_alloc(art, "free", env=env).stdout)
    assert free2["free"] == [1] and free2["locked"] == [0]
    assert free2["recycled"] == []

    # release reopens the card for the next variant
    rel = _alloc(art, "release", "--idx", "0", env=env)
    assert rel.returncode == 0, rel.stderr
    assert json.loads(_alloc(art, "free", env=env).stdout)["free"] == [0, 1]


def test_device_alloc_claim_guard_never_trains_on_busy_real(tmp_path):
    """The claim guard: acquire is lock-scoped, so on a machine with a
    FOREIGN (lockless) process on device 0 it would hand out idx 0 — claim
    releases that card and fails loud instead of training on it."""
    art = tmp_path / "ws"
    _write_train_device(art, backend="cuda", count=2)
    env = _stub_env(tmp_path, tools={
        "nvidia-smi": _NVIDIA_SMI_STUB.format(bash=_BASH)})  # GPU 0 busy-real

    proc = _alloc(art, "claim", "--vid", "r1-01", env=env)
    assert proc.returncode == 2
    assert "outside the free set" in proc.stderr
    assert not (art / "devices" / "0.lock").exists()   # never kept

    # the park state: 0 busy-real + 1 live lock -> a legitimate wait, rc 0
    _write_lock(art, 1, "r0-01", os.getpid())
    parked = _alloc(art, "claim", "--vid", "r1-01", env=env)
    assert parked.returncode == 0, parked.stderr
    payload = json.loads(parked.stdout)
    assert payload["ok"] is False
    assert payload["reason"] == "no free training device"
    assert payload["busy_real"] == [0] and payload["locked"] == [1]


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


def test_watch_variant_stub_pins_signature(tmp_path):
    art = tmp_path / "ws"
    art.mkdir()
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)

    helped = _run_cli([_BASH, str(_WATCH_SH), "--help"], env=env)
    assert helped.returncode == 0
    assert "--vid" in helped.stdout and "--device" in helped.stdout

    missing_vid = _run_cli([_BASH, str(_WATCH_SH), "--device", "0"], env=env)
    assert missing_vid.returncode == 2
    bad_device = _run_cli([_BASH, str(_WATCH_SH), "--vid", "r1-01",
                           "--device", "x"], env=env)
    assert bad_device.returncode == 2

    ok = _run_cli([_BASH, str(_WATCH_SH), "--vid", "r1-01", "--device", "0"],
                  env=env)
    assert ok.returncode == 0, ok.stderr
    vdir = art / "variants" / "r1-01"
    pid = (vdir / "watchdog.pid").read_text(encoding="utf-8").strip()
    assert pid.isdigit()
    log = (vdir / "watchdog.log").read_text(encoding="utf-8")
    assert "vid=r1-01" in log and "device=0" in log


def _probe_ws(tmp_path: Path, *, makespan: int = 400,
              verdict: bool = True, lock_vid: str = "r1-01",
              watchdog: bool = True,
              liveness: dict | None = None) -> Path:
    """A launched-variant workspace for the §6.2 gate: one latency_pass vid
    with a verdict at/below the frozen line, a ledger lock naming it, the
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
                               pred_actual_ratio=None, outcome="latency_pass")
    vd = art / "variants" / "r1-01"
    (vd / "train").mkdir(parents=True)
    if verdict:
        (vd / "verdict.json").write_text(json.dumps(
            {"vid": "r1-01", "round": 1, "outcome": "latency_pass",
             "makespan_cycles": makespan, "target_cycles": 500}),
            encoding="utf-8")
    _write_lock(art, 0, lock_vid, os.getpid())
    if watchdog:
        (vd / "watchdog.pid").write_text("4242\n", encoding="utf-8")
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


def test_probe_emit_rejects_torn_verdict(tmp_path):
    # above the frozen line -> torn workspace, never admissible
    art = _probe_ws(tmp_path, makespan=600, liveness={})
    (art / "variants" / "r1-01" / "train" / "train.pid").write_text(
        f"{os.getpid()}\n", encoding="utf-8")
    proc = _check_probe(art)
    assert proc.returncode == 1
    assert "torn workspace" in proc.stderr and "600" in proc.stderr

    # verdict file gone entirely -> same failure class
    art2 = _probe_ws(tmp_path / "b", verdict=False, liveness={})
    (art2 / "variants" / "r1-01" / "train" / "train.pid").write_text(
        f"{os.getpid()}\n", encoding="utf-8")
    proc2 = _check_probe(art2)
    assert proc2.returncode == 1
    assert "verdict.json missing" in proc2.stderr


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

    # a workspace with no latency_pass vid at all: nothing to verify
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
    assert proc3.returncode == 1 and "target_cycles unavailable" in proc3.stderr
