"""test_po_v7.py — new-surface tests for the prof-opt v7 redesign
(docs/specs/prof-opt-v7-spec.md §15.2).

Covers the admission list the spec names:
  - device_alloc: probe raw passthrough (incl. CLI failure fail loud),
    claim --idx lock/refusal/out-of-range (the fuller matrix also lives in
    test_po_v6.py), adopt/release non-regression, pid_lib unknown semantics
  - check_verdict.py: the ONE latency-line predicate (inclusive boundary,
    torn verdicts) + its three callers agreeing (recheck / probe emit /
    protocol doc reference)
  - gate_decide idle exit: N consecutive zero-proposal rounds -> report;
    fewer -> loop; a non-idle round breaks the streak
  - watchdog.py: duplicate-epoch last-wins disclosure, streak threshold
    derived from E (already exercised in test_po_v6.py — here the
    final_check stderr lands VERBATIM in watchdog.log)
  - metric_curve: duplicate epoch last-wins + "no lines yet" vs "pattern
    matched nothing" distinction
  - check_baseline_docs three-document gate matrix lives in
    test_po_scripts.py; check_propose_emit assessment/stamp and negative
    prediction gates live in test_po_v6.py / test_po_scripts.py.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workflows" / "prof-opt" / "agents" / "_po_scripts"
sys.path.insert(0, str(_SCRIPTS))

from gate_decide import decide  # noqa: E402


def _run_cli(args, env=None, timeout=120):
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=timeout, env=merged)


# ── pid_lib: the shared three-valued liveness predicate ───────────────────────

def test_pid_lib_three_valued_liveness():
    import pid_lib

    # a live process is confirmed alive
    proc = subprocess.Popen(["sleep", "30"])
    try:
        assert pid_lib.liveness(proc.pid) == "alive"
        # a non-positive pid names no process, deterministically dead
        assert pid_lib.liveness(0) == "dead"
        assert pid_lib.liveness(-5) == "dead"
        # the disclosure helper surfaces "unverifiable" instead of guessing
        dead = subprocess.Popen(["true"])
        dead.wait()
        assert pid_lib.liveness(dead.pid) == "dead"
        ok, note = pid_lib.liveness_disclosed(proc.pid, "watchdog")
        assert ok is True and note is None
    finally:
        proc.kill()
        proc.wait()


def test_pid_lib_unknown_is_disclosed_never_alive(monkeypatch):
    """On a host without os.kill (non-posix), liveness is UNKNOWN and the
    caller MUST disclose it — the helper returns a disclosure sentence and
    never confirms alive (the phantom-owner bug this module exists to
    prevent)."""
    import pid_lib
    monkeypatch.setattr(pid_lib.os, "kill", None, raising=False)
    monkeypatch.setattr(pid_lib.os, "hasattr", lambda name, attr: False,
                        raising=False)
    assert pid_lib.liveness(4242) == "unknown"
    ok, note = pid_lib.liveness_disclosed(4242, "device lock owner")
    assert ok is False
    assert "liveness unverifiable" in note
    assert "4242" in note


# ── device_alloc probe: raw passthrough, no parsing, fail loud ────────────────

_ALLOC_PY = _SCRIPTS / "device_alloc.py"

_NVIDIA_RAW = """Wed Sep  1 10:00:00 2026
+-----------------------------------------------------------------------------+
| NVIDIA-SMI 535.104.05   Driver Version: 535.104.05   CUDA Version: 12.2     |
|-------------------------------+----------------------+----------------------+
| GPU  Name        Persistence-M| Bus-Id        Disp.A | Volatile Uncorr. ECC |
|   0  NVIDIA A100         Off  | 00000000:01:00.0 Off |                    0 |
| 30%  60C    P0    50W / 400W |    0MiB / 40960MiB |      0%      Default |
|-------------------------------+----------------------+----------------------|
|   1  NVIDIA A100         Off  | 00000000:02:00.0 Off |                    0 |
| 30%  60C    P0    50W / 400W |  512MiB / 40960MiB |     30%      Default |
+-----------------------------------------------------------------------------+
"""


def _alloc_env(tmp_path: Path, tools: dict[str, str]) -> dict:
    """PATH holding ONLY the stub tools dir + the essentials the CLI needs."""
    stub = tmp_path / "stubbin"
    stub.mkdir(exist_ok=True)
    for name, body in tools.items():
        (stub / name).write_text(body, encoding="utf-8")
        (stub / name).chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(stub)
    return env


def _probe(art: Path, backend: str, env: dict):
    return _run_cli([sys.executable, str(_ALLOC_PY), "probe",
                     "--artifacts", str(art), "--backend", backend], env=env)


def test_device_alloc_probe_passes_raw_stdout_through_verbatim(tmp_path):
    art = tmp_path / "ws"
    art.mkdir(parents=True, exist_ok=True)
    (art / "train_device.json").write_text(json.dumps(
        {"backend": "cuda", "device_count": 2, "resolved_by": "test"}),
        encoding="utf-8")
    env = _alloc_env(tmp_path, {"nvidia-smi": f"#!/bin/sh\nprintf '%s' \"\"\"\n"
                                               f"{_NVIDIA_RAW}\"\"\"\n"})
    proc = _probe(art, "cuda", env)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    # NOTHING is parsed into a busy set: the raw backend table passes through
    # verbatim for the AGENT to read; only our OWN locks are structured
    assert doc["backend"] == "cuda" and doc["device_count"] == 2
    assert doc["raw"].strip() == _NVIDIA_RAW.strip()
    assert "busy" not in doc and "free" not in doc
    assert doc["locks"] == []

    # our own live lock shows up in the structured locks view (idx-ascending)
    (art / "devices").mkdir(parents=True, exist_ok=True)
    (art / "devices" / "0.lock").write_text(json.dumps(
        {"vid": "r1-01", "pid": 111, "acquired_at": "t0",
         "backend": "cuda"}) + "\n", encoding="utf-8")
    doc2 = json.loads(_probe(art, "cuda", env).stdout)
    assert doc2["locks"] == [{"idx": 0, "vid": "r1-01", "pid": 111,
                              "acquired_at": "t0"}]
    assert doc2["raw"].strip() == _NVIDIA_RAW.strip()


def test_device_alloc_probe_backend_mismatch_fails_loud(tmp_path):
    art = tmp_path / "ws"
    art.mkdir(parents=True, exist_ok=True)
    (art / "train_device.json").write_text(json.dumps(
        {"backend": "npu", "device_count": 1, "resolved_by": "test"}),
        encoding="utf-8")
    env = _alloc_env(tmp_path, {})          # hermetic: no backend CLIs at all
    proc = _probe(art, "cuda", env)
    assert proc.returncode == 2
    assert "disagrees" in proc.stderr


def test_device_alloc_probe_cli_missing_fails_loud(tmp_path):
    """Without observation there is no honest card selection: a missing or
    failing backend CLI exits 2 (never a guessed free set)."""
    art = tmp_path / "ws"
    art.mkdir(parents=True, exist_ok=True)
    (art / "train_device.json").write_text(json.dumps(
        {"backend": "cuda", "device_count": 2, "resolved_by": "test"}),
        encoding="utf-8")
    proc = _probe(art, "cuda", _alloc_env(tmp_path, {}))
    assert proc.returncode == 2
    assert "cannot be observed" in proc.stderr

    # a failing CLI (rc != 0) fails the same way
    env = _alloc_env(tmp_path, {"nvidia-smi": "#!/bin/sh\nexit 3\n"})
    proc2 = _probe(art, "cuda", env)
    assert proc2.returncode == 2
    assert "rc 3" in proc2.stderr


def test_device_alloc_adopt_unknown_liveness_refuses(tmp_path, monkeypatch):
    """pid_lib integration: an UNVERIFIABLE owner pid is refused — the lock
    must never sit behind a pid nobody can prove is alive (behavioral: the
    liveness predicate is stubbed to `unknown` and adopt is driven for
    real)."""
    import device_alloc
    art = tmp_path / "ws"
    art.mkdir(parents=True, exist_ok=True)
    (art / "train_device.json").write_text(json.dumps(
        {"backend": "cuda", "device_count": 1, "resolved_by": "test"}),
        encoding="utf-8")
    (art / "devices").mkdir(parents=True, exist_ok=True)
    (art / "devices" / "0.lock").write_text(json.dumps(
        {"vid": "r1-01", "pid": 111, "acquired_at": "t0",
         "backend": "cuda"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(device_alloc, "liveness", lambda pid: "unknown")
    with pytest.raises(device_alloc.AllocError, match="liveness unverifiable"):
        device_alloc.adopt(art, "r1-01", 4242)
    # the lock is untouched — an unconfirmable owner never steals the card
    assert json.loads((art / "devices" / "0.lock").read_text(
        encoding="utf-8"))["pid"] == 111


def test_device_alloc_sweep_releases_dead_keeps_alive_and_unknown(tmp_path,
                                                                  monkeypatch):
    """The terminal backstop (v7 §6.2): a lock whose owner pid is CONFIRMED
    dead is released; alive and unknown owners keep their cards (unknown is
    disclosed, never guessed away); an unparseable lock file is kept and
    surfaced."""
    import device_alloc
    art = tmp_path / "ws"
    art.mkdir(parents=True, exist_ok=True)
    (art / "train_device.json").write_text(json.dumps(
        {"backend": "cuda", "device_count": 4, "resolved_by": "test"}),
        encoding="utf-8")
    devices = art / "devices"
    devices.mkdir(parents=True, exist_ok=True)
    live = subprocess.Popen(["sleep", "30"])
    dead = subprocess.Popen(["true"])
    dead.wait()
    try:
        (devices / "0.lock").write_text(json.dumps(
            {"vid": "r1-01", "pid": live.pid, "acquired_at": "t0",
             "backend": "cuda"}) + "\n", encoding="utf-8")     # alive
        (devices / "1.lock").write_text(json.dumps(
            {"vid": "r1-02", "pid": dead.pid, "acquired_at": "t0",
             "backend": "cuda"}) + "\n", encoding="utf-8")     # dead
        (devices / "2.lock").write_text(json.dumps(
            {"vid": "r1-03", "pid": 111, "acquired_at": "t0",
             "backend": "cuda"}) + "\n", encoding="utf-8")     # -> unknown
        (devices / "3.lock").write_text("{not json", encoding="utf-8")

        real_liveness = device_alloc.liveness

        def fake_liveness(pid):
            return "unknown" if pid == 111 else real_liveness(pid)

        monkeypatch.setattr(device_alloc, "liveness", fake_liveness)
        result = device_alloc.sweep(art)
    finally:
        live.kill()
        live.wait()

    assert result["released"] == 1
    by_idx = {row["idx"]: row for row in result["locks"]}
    assert by_idx[0]["action"] == "kept" and by_idx[0]["liveness"] == "alive"
    assert by_idx[1]["action"] == "released"
    assert by_idx[2]["action"] == "kept" \
        and by_idx[2]["liveness"] == "unknown" \
        and "liveness unverifiable" in by_idx[2]["note"]
    assert by_idx[3]["action"] == "kept" \
        and by_idx[3]["liveness"] == "unparseable"
    assert (devices / "1.lock").exists() is False
    for kept in (0, 2, 3):
        assert (devices / f"{kept}.lock").exists()


# ── check_verdict: the ONE latency-line predicate ─────────────────────────────

def _verdict_ws(tmp_path: Path, makespan=400, target=500) -> Path:
    art = tmp_path / "ws"
    art.mkdir(parents=True, exist_ok=True)
    (art / "base").mkdir(parents=True, exist_ok=True)
    (art / "base" / "origin_anchor.json").write_text(json.dumps(
        {"target_cycles": target}), encoding="utf-8")
    vd = art / "variants" / "r1-01"
    vd.mkdir(parents=True)
    (vd / "verdict.json").write_text(json.dumps(
        {"vid": "r1-01", "makespan_cycles": makespan}), encoding="utf-8")
    return art


def test_check_verdict_inclusive_boundary(tmp_path):
    art = _verdict_ws(tmp_path, makespan=500, target=500)
    proc = _run_cli([sys.executable, str(_SCRIPTS / "check_verdict.py"),
                     "--vid", "r1-01", "--artifacts", str(art)])
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"vid": "r1-01", "makespan_cycles": 500,
                                       "target_cycles": 500, "ok": True}

    # one ABOVE the line is refused
    art2 = _verdict_ws(tmp_path / "b", makespan=501, target=500)
    proc2 = _run_cli([sys.executable, str(_SCRIPTS / "check_verdict.py"),
                      "--vid", "r1-01", "--artifacts", str(art2)])
    assert proc2.returncode == 1
    assert "above the frozen line" in proc2.stderr

    # the --makespan pre-verdict mode judges the same boundary
    art3 = _verdict_ws(tmp_path / "c", makespan=500, target=500)
    proc3 = _run_cli([sys.executable, str(_SCRIPTS / "check_verdict.py"),
                      "--vid", "r1-01", "--artifacts", str(art3),
                      "--makespan", "501"])
    assert proc3.returncode == 1


def test_check_verdict_torn_states_fail_loud(tmp_path):
    # missing verdict
    art = tmp_path / "ws"
    art.mkdir(parents=True, exist_ok=True)
    (art / "base").mkdir(parents=True, exist_ok=True)
    (art / "base" / "origin_anchor.json").write_text('{"target_cycles": 500}',
                                                     encoding="utf-8")
    proc = _run_cli([sys.executable, str(_SCRIPTS / "check_verdict.py"),
                     "--vid", "r9-99", "--artifacts", str(art)])
    assert proc.returncode == 1
    assert "torn" in proc.stderr

    # a structural-mismatch verdict carries null makespan
    art2 = _verdict_ws(tmp_path / "b")
    (art2 / "variants" / "r1-01" / "verdict.json").write_text(json.dumps(
        {"vid": "r1-01", "makespan_cycles": None}), encoding="utf-8")
    proc2 = _run_cli([sys.executable, str(_SCRIPTS / "check_verdict.py"),
                      "--vid", "r1-01", "--artifacts", str(art2)])
    assert proc2.returncode == 1


def test_check_verdict_is_the_single_predicate_three_callers():
    """v7 §6.2: the recheck gate, the probe emit gate, and the probe protocol
    all reference check_verdict.py — no hand-copied comparison survives."""
    recheck = (_REPO / "workflows" / "prof-opt" / "agents" / "po_propose"
               / "scripts" / "run_latency_recheck.sh").read_text(encoding="utf-8")
    assert "check_verdict.py" in recheck
    assert '<= target' not in recheck.replace("check_verdict", "")  # no hand copy
    probe_emit = (_SCRIPTS / "check_probe_emit.py").read_text(encoding="utf-8")
    assert "from check_verdict import check_verdict" in probe_emit
    protocol = (_REPO / "workflows" / "prof-opt" / "agents" / "po_probe"
                / "references" / "probe_protocol.md").read_text(encoding="utf-8")
    assert "check_verdict.py" in protocol


# ── gate idle exit (§8) ────────────────────────────────────────────────────────

def _idle_ws(tmp_path: Path, rounds: dict[int, list | None]) -> Path:
    """rounds: {round_no: proposals list | None} — None = no proposals.json."""
    art = tmp_path / "ws"
    art.mkdir(parents=True, exist_ok=True)
    (art / "base").mkdir(parents=True, exist_ok=True)
    (art / "base" / "origin_anchor.json").write_text(json.dumps(
        {"target_cycles": 500}), encoding="utf-8")
    for rnd, proposals in rounds.items():
        rd = art / "rounds" / f"{rnd:03d}"
        rd.mkdir(parents=True, exist_ok=True)
        if proposals is not None:
            (rd / "proposals.json").write_text(json.dumps(
                {"round": rnd, "filtered_count": 0,
                 "exhausted_rationale": [{"lever": "x", "direction": "y",
                                          "why_not": "z"}] if not proposals else [],
                 "proposals": proposals}), encoding="utf-8")
    return art


def test_gate_idle_exhausted_exits_to_report(tmp_path):
    """idle_round_cap consecutive zero-proposal rounds -> report with the
    consecutive count disclosed (the loop never spins on a spent space)."""
    art = _idle_ws(tmp_path, {r: [] for r in (1, 2, 3)})   # 3 idle rounds
    out = decide(art, max_rounds=100, idle_round_cap=3)
    assert out["decision"] == "report"
    assert out["idle_rounds"] == 3
    assert "idle_exhausted" in out["reason"]
    assert "3" in out["reason"]


def test_gate_idle_below_cap_loops(tmp_path):
    art = _idle_ws(tmp_path, {1: [], 2: []})               # 2 < cap 3
    out = decide(art, max_rounds=100, idle_round_cap=3)
    assert out["decision"] == "loop"
    assert out["idle_rounds"] == 2


def test_gate_idle_streak_breaks_on_a_real_proposal(tmp_path):
    """A non-empty round (or a MISSING proposals.json — an incomplete round)
    breaks the backwards streak: only consecutive zero-proposal rounds count."""
    art = _idle_ws(tmp_path, {1: [], 2: [], 3: [], 4: [
        {"vid": "r4-01", "change_sig": "s"}]})             # round 4 non-idle
    out = decide(art, max_rounds=100, idle_round_cap=3)
    assert out["decision"] == "loop"
    assert out["idle_rounds"] == 0

    # latest rounds idle but an older real round walls the streak at 2
    art2 = _idle_ws(tmp_path, {1: [], 2: [
        {"vid": "r2-01", "change_sig": "s"}], 3: [], 4: []})
    out2 = decide(art2, max_rounds=100, idle_round_cap=3)
    assert out2["decision"] == "loop"
    assert out2["idle_rounds"] == 2


def test_gate_idle_cap_zero_disables_the_exit(tmp_path):
    art = _idle_ws(tmp_path, {r: [] for r in range(1, 6)})
    out = decide(art, max_rounds=100, idle_round_cap=0)
    assert out["decision"] == "loop"


# ── metric_curve v7: duplicate epochs + the empty-vs-pattern distinction ─────

def test_metric_curve_duplicate_epoch_last_wins_disclosed(tmp_path):
    import metric_curve as mc
    log = tmp_path / "train.log"
    # epoch 2 re-printed (refreshed progress line) — the LAST line must win
    log.write_text("epoch 1 metric=0.5\nepoch 2 metric=0.6\nepoch 2 metric=0.65\n",
                   encoding="utf-8")
    out = tmp_path / "curve.jsonl"
    proc = _run_cli([sys.executable, str(_SCRIPTS / "metric_curve.py"),
                     "extract", "--log", str(log),
                     "--pattern", r"epoch (?P<epoch>\d+) metric=(?P<metric>[0-9.]+)",
                     "--out", str(out)])
    assert proc.returncode == 0, proc.stderr
    rows = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]
    assert rows == [{"epoch": 1, "metric": 0.5}, {"epoch": 2, "metric": 0.65}]
    # the disclosure is surfaced once (stdout summary + stderr note), never
    # silently swallowed
    assert json.loads(proc.stdout)["duplicate_epochs_last_wins"] == [2]
    assert "last line wins" in proc.stderr
    # the module API returns the duplicate set for in-process callers
    points, duplicates = mc._extract(log, r"epoch (?P<epoch>\d+) metric=(?P<metric>[0-9.]+)")
    assert duplicates == [2]


def test_metric_curve_empty_log_vs_pattern_mismatch(tmp_path):
    """v7 §7.2 curve row: 'no lines yet' is a TRANSIENT state distinct from
    'pattern matched nothing in a non-empty log' — the latter fails loud
    naming the pattern."""
    import metric_curve as mc
    pattern = r"epoch (?P<epoch>\d+) metric=(?P<metric>[0-9.]+)"

    empty = tmp_path / "empty.log"
    empty.write_text("   \n", encoding="utf-8")
    with pytest.raises(mc.MetricCurveError, match="no lines yet"):
        mc._extract(empty, pattern)
    missing = tmp_path / "missing.log"
    with pytest.raises(mc.MetricCurveError, match="not found"):
        mc._extract(missing, pattern)

    wrong_format = tmp_path / "wrong.log"
    wrong_format.write_text("step 1 loss=0.5\nstep 2 loss=0.4\n", encoding="utf-8")
    with pytest.raises(mc.MetricCurveError, match="pattern") as excinfo:
        mc._extract(wrong_format, pattern)
    assert "matched nothing" in str(excinfo.value)


# ── watchdog: final_check stderr lands verbatim in watchdog.log ───────────────

def test_watch_final_check_stderr_lands_in_watchdog_log(tmp_path):
    """v7 终态链 row: the final_check failure's REASON lands VERBATIM in
    watchdog.log (metric_curve's own message, no guessed cause)."""
    src_watch = _SCRIPTS / "watch_variant.py"
    art = tmp_path / "ws"
    art.mkdir(parents=True, exist_ok=True)
    (art / "scripts").mkdir(parents=True)
    for src in ("metric_curve.py", "verdict_decide.py", "history_lib.py",
                "ledger_aggregate.py", "device_alloc.py", "render_run.sh",
                "assert_shadow.py", "pid_lib.py", "push_curves.py"):
        shutil.copy(_SCRIPTS / src, art / "scripts" / src)
    (art / "orca_inject").mkdir(parents=True)
    for src in ("header.env", "sitecustomize.py"):
        shutil.copy(_SCRIPTS / "orca_inject" / src, art / "orca_inject" / src)
    (art / "templates").mkdir(parents=True)
    (art / "templates" / "run_eval.template.sh").write_text(
        'cat "<<ckpt>>.metric"\n', encoding="utf-8")
    (art / "contracts.json").write_text(json.dumps({
        "interpreter": {"sys_executable": sys.executable},
        "full_train_budget": {"epochs": 5, "seed": 7},
        "proxy_budget": {"epochs": 1, "seed": 7},
        "early_stop": {"warmup_frac": 0.1, "streak_frac": 0.3},
        "eval": {"metric_extraction": {
                     "kind": "stdout_regex",
                     "pattern": r"final metric: ([0-9]*\.?[0-9]+)"},
                 "metric_direction": "higher_better", "tier": "A"},
        "train": {"ckpt_output_rule": "{out_dir}/ckpt_*.pt",
                  "epoch_metric_extraction":
                      r"epoch (?P<epoch>[0-9]+) metric (?P<metric>[0-9]*\.?[0-9]+)",
                  "ckpt_per_epoch": False},
        "shadow": {"shadow_pkgs": ["pkg"]}}), encoding="utf-8")
    (art / "readiness").mkdir(parents=True)
    (art / "readiness" / "readiness.json").write_text(
        json.dumps({"project_root": str(tmp_path)}), encoding="utf-8")
    (art / "base").mkdir(parents=True, exist_ok=True)
    (art / "base" / "origin_anchor.json").write_text(json.dumps(
        {"target_cycles": 500, "accuracy_budget": 0.05}), encoding="utf-8")
    (art / "baseline").mkdir(parents=True)
    (art / "baseline" / "baseline_metrics.jsonl").write_text(
        "".join(json.dumps({"epoch": e, "metric": 0.9}) + "\n"
                for e in range(1, 6)), encoding="utf-8")
    (art / "baseline" / "baseline_full_acc.json").write_text(json.dumps(
        {"baseline_full_acc": 0.9}), encoding="utf-8")
    vd = art / "variants" / "r1-01"
    (vd / "train").mkdir(parents=True, exist_ok=True)
    (vd / "train" / "train.rendered.sh").write_text(
        '#!/usr/bin/env bash\necho done\n', encoding="utf-8")
    (vd / "train" / "rc").write_text("0\n", encoding="utf-8")
    # 3 epochs logged vs E=5 -> the final check fails; the metric_curve
    # message (with the real counts) must appear VERBATIM in watchdog.log
    (vd / "train" / "train.log").write_text(
        "".join(f"epoch {e} metric 0.85\n" for e in range(1, 4)),
        encoding="utf-8")
    (art / "devices").mkdir(parents=True)
    (art / "devices" / "0.lock").write_text(json.dumps(
        {"vid": "r1-01", "pid": os.getpid(), "acquired_at": "t0",
         "backend": "cuda"}) + "\n", encoding="utf-8")

    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    proc = _run_cli([sys.executable, str(src_watch), "--vid", "r1-01",
                     "--device", "0", "--once"], env=env)
    assert proc.returncode == 0, proc.stderr
    status = json.loads((vd / "train_status.json").read_text(encoding="utf-8"))
    assert status["stage"] == "failed"
    row = json.loads([ln for ln in
                      (art / "history.jsonl").read_text(encoding="utf-8")
                      .splitlines()][-1])
    assert row["outcome"] == "probe_insufficient"
    assert row["stage"] == "final_check"
    log = (vd / "watchdog.log").read_text(encoding="utf-8")
    # the VERBATIM metric_curve failure text (not a guessed cause)
    assert "expected 5 epoch metrics" in log
    assert "stage=final_check verdict=probe_insufficient" in log
    assert not (art / "devices" / "0.lock").exists()   # the card is released


# ── deployed-set sanity: the v7 scripts ride the deploy manifest ─────────────

def test_v7_scripts_are_in_the_shared_set():
    for name in ("watch_variant.py", "pid_lib.py", "check_verdict.py",
                 "shadow_pkgs_csv.py"):
        assert (_SCRIPTS / name).is_file(), name
    for gone in ("watch_variant.sh", "resolve_profile_mode.sh",
                 "placeholder_profiler.py", "check_bottleneck.py",
                 "healed_files.py"):
        assert not (_SCRIPTS / gone).exists(), gone


def test_spec_deletion_checklist_paths_are_gone():
    """§12: every retired path must not exist anywhere under prof-opt."""
    wf = _REPO / "workflows" / "prof-opt"
    gone_rel = [
        "agents/_po_scripts/resolve_profile_mode.sh",
        "agents/_po_scripts/placeholder_profiler.py",
        "agents/_po_scripts/check_bottleneck.py",
        "agents/_po_scripts/healed_files.py",
        "agents/_po_scripts/watch_variant.sh",
        "subagents/bottleneck-analyst.md",
        "agents/po_baseline/scripts/check_business_logic.sh",
        "agents/po_contract/scripts/shadow_pkgs_csv.py",   # moved to shared
    ]
    for rel in gone_rel:
        assert not (wf / rel).exists(), rel


# ── check_report.py: the human report's structural gate ───────────────────────

def _report_doc(tmp_path: Path, sections: dict[str, str] | None = None,
                disclosure: str | None = None) -> Path:
    """A compliant prof_opt_report.md skeleton (every section + the three
    disclosure tokens), overridable per test."""
    body = {
        "## 披露": disclosure or ("- profiling 来源：mfu 实测 via 用户内网评测工具 "
                                 "(chip 6613 / INT8 / 1)\n"
                                 "- 训练设备后端：train_device.json "
                                 "{backend: cuda, device_count: 2}\n"
                                 "- chart daemon 状态：pushed curves/pareto "
                                 "(.chart_push.log 末行)\n"),
        "## 终态": "status=success stage=report\n",
        "## 逐轮表": "| round | proposals |\n|---|---|\n",
        "## 训练结局披露": "killed=1 done=1\n",
        "## 胜出者": "r1-01 gap=0.02\n",
        "## 公平性说明": "same full_train_budget\n",
        "## 基线与最终": "500 -> 240\n",
        "## 轮次结论": "r1: activation swap delivered\n",
        "## 精度规则": "3 rules\n",
        "## 写回": "model_prof_optimized.py\n",
        "## 面板与文档": "dashboard.html\n",
    }
    if sections is not None:
        body = sections
    art = tmp_path / "ws"
    art.mkdir(parents=True, exist_ok=True)
    (art / "prof_opt_report.md").write_text(
        "".join(f"{h}\n{t}\n" for h, t in body.items()), encoding="utf-8")
    return art


def _check_report(art: Path):
    return _run_cli([sys.executable, str(_SCRIPTS / "check_report.py"),
                     "--artifacts", str(art)])


def test_check_report_gate_matrix(tmp_path):
    # compliant report passes
    assert _check_report(_report_doc(tmp_path)).returncode == 0

    # a missing section is named
    doc = _report_doc(tmp_path / "a")
    text = (doc / "prof_opt_report.md").read_text(encoding="utf-8")
    (doc / "prof_opt_report.md").write_text(
        text.replace("## 轮次结论\nr1: activation swap delivered\n", ""),
        encoding="utf-8")
    proc = _check_report(doc)
    assert proc.returncode == 1
    assert "轮次结论" in proc.stderr

    # a disclosure line gone -> its token is named
    doc2 = _report_doc(tmp_path / "b",
                       disclosure="- profiling 来源：mfu 实测 via 内网工具\n"
                                  "- 训练设备后端：cuda x2\n"
                                  "- 图表推送正常\n")
    proc2 = _check_report(doc2)
    assert proc2.returncode == 1
    assert "train_device" in proc2.stderr and "chart daemon" in proc2.stderr

    # missing report entirely
    empty = tmp_path / "c"
    empty.mkdir()
    assert _check_report(empty).returncode == 1


def test_check_report_literals_pinned_in_format_doc():
    """Drift pin: every heading and disclosure token the gate enforces is
    enumerated in report_format.md §5 (the authoring contract) — editing
    one side alone breaks this test, never the runtime agent."""
    import check_report
    fmt = (_REPO / "workflows" / "prof-opt" / "agents" / "po_report"
           / "references" / "report_format.md").read_text(encoding="utf-8")
    for heading in check_report.SECTIONS:
        assert heading in fmt, heading
    for token in check_report.DISCLOSURE_TOKENS:
        assert token in fmt, token
