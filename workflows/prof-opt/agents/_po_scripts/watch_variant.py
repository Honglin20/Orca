#!/usr/bin/env python3
"""watch_variant.py — detached per-variant training watchdog (v7 §7.2).

A stdlib-only resident process (the v6 bash guardian rewritten so the
curve/streak/anchor semantics are one implementation, not a shell quilt).
Detached by po_probe right after the training wrapper; this script
self-writes watchdog.pid and every watchdog.log line starts with an
ISO8601 UTC stamp. Contract table (v7 §7.2), each row mechanically below:

  轮询       one supervision cycle every 10 s; SIGTERM -> attribution-checked
             kill of the training process group + terminal state + card
             release (the platform tearing the run down stops the training
             honestly — never an orphan card).
  曲线       metric_curve extract, incremental; an epoch with MULTIPLE
             metric lines -> the LAST line wins, disclosed once; "log has
             no lines yet" (transient) is distinct from "pattern matched
             nothing" (fail loud, the pattern named — v7 deletes the
             bash-era silent `rm tmp`).
  train_status  stage ∈ waiting|training|killed|done|failed; while waiting
             for the baseline full-acc anchor the LAST KNOWN epoch/metric/
             gap are preserved (the v6 B12 wipe-and-regress bug).
  流式早停   warmup = ceil(warmup_frac x E); over-budget streak >=
             max(2, ceil(streak_frac x E)) -> attribution-checked kill ->
             stage=killed. The fractions come from contracts.json
             `early_stop` (never a hardcoded streak of 10).
  崩溃       the training process dying WITHOUT an rc file is terminal:
             recorded, stage=failed, crash attribution + log paths
             disclosed (v7 drops the bash-era crash relaunch).
  终态链     final_check (its failure reason lands VERBATIM in
             watchdog.log — no guessing) -> full eval -> k eval (when
             ckpt_per_epoch) -> final_acc.json -> verdict -> stage=done ->
             .rules_pending marker -> card release.
  心跳       every cycle touches $ORCA_ARTIFACTS_DIR/.run_lock (the mtime
             heartbeat reuse_check's stale judgment reads — a long
             training must never look stale).
  日志       every diagnostic goes to variants/<VID>/watchdog.log; stderr
             is never swallowed.

The early-stop curve stays a PREFIX of the full-budget render (fairness
invariant): the training renders at the SAME full_train_budget the
baseline trained under — only the kill is early, never the budget.

Usage:
  watch_variant.py --vid <VID> --device <IDX> [--once]
--once: run exactly ONE supervision cycle against the current disk state
  and exit (stdout: the cycle's status JSON). The tests drive the judgment
  boundaries through it; the detached guardian never uses it.

Environment: ORCA_ARTIFACTS_DIR (required — the run workspace root).
stdout: ALWAYS exactly one JSON line (the cycle / terminal / replay
status). Hard errors exit 2 with a FATAL line on stderr + watchdog.log;
the early-stop attribution check failing is exactly such a FATAL (refuse
to kill, never touch the terminal state — a torn workspace is the report
sweep's business, not something to paper over here).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time
import math
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import device_alloc  # noqa: E402
import metric_curve  # noqa: E402
from history_lib import append_terminal  # noqa: E402
from pid_lib import liveness  # noqa: E402

TERMINAL_STAGES = frozenset({"killed", "done", "failed"})
TRAIN_STAGES = frozenset({"waiting", "training"}) | TERMINAL_STAGES
POLL_SECONDS = 10
# v6 defaults kept only as the fallback when contracts.json predates the
# early_stop block (the gate pins the block; this never fires on a fresh
# v7 workspace)
_EARLY_STOP_DEFAULTS = {"warmup_frac": 0.1, "streak_frac": 0.3}

VID = ""
DEVICE = -1
ONCE = False
ART = Path()
VDIR = Path()
TRAIN_DIR = Path()
TLOG = TRAIN_DIR / "train.log"
TPID = TRAIN_DIR / "train.pid"
TRC = TRAIN_DIR / "rc"
RENDERED = TRAIN_DIR / "train.rendered.sh"
METRICS = Path()
STATUS = Path()
SHARD = Path()
WPID = Path()
WLOG = Path()
EDIR = Path()
FINAL_ACC = Path()
RULES_PENDING = Path()
RUN_LOCK = Path()

PY = ""
PROJ_ROOT = ""
EPOCHS = 1
DIRECTION = ""
SHADOW_PKGS = ""
CKPT_RULE = ""
CKPT_PER_EPOCH = False
PROBE_K = 1
BUDGET = 0.0
WARMUP_FRAC = 0.1
STREAK_FRAC = 0.3
SIGTERM_SEEN = False


def wlog(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(WLOG, "a", encoding="utf-8") as fh:
        fh.write(f"{stamp} {message}\n")


def fatal(message: str) -> None:
    wlog(f"FATAL: {message}")
    print(f"FATAL: {message}", file=sys.stderr)
    sys.exit(2)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, doc: dict) -> None:
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ── startup: contract / anchor readers (fail loud when the ws is torn) ───────

def _load_contracts() -> None:
    global PY, EPOCHS, DIRECTION, SHADOW_PKGS, CKPT_RULE, CKPT_PER_EPOCH
    global PROBE_K, WARMUP_FRAC, STREAK_FRAC, PROJ_ROOT
    cpath = ART / "contracts.json"
    try:
        c = json.loads(cpath.read_text(encoding="utf-8"))
        PY = c["interpreter"]["sys_executable"]
        EPOCHS = int(c["full_train_budget"]["epochs"])
        DIRECTION = c["eval"]["metric_direction"]
        SHADOW_PKGS = ",".join(c["shadow"]["shadow_pkgs"])
        CKPT_RULE = c["train"]["ckpt_output_rule"]
        CKPT_PER_EPOCH = bool(c["train"]["ckpt_per_epoch"])
        PROBE_K = int(c["proxy_budget"]["epochs"])
        PROJ_ROOT = json.loads(
            (ART / "readiness" / "readiness.json").read_text(encoding="utf-8")
        )["project_root"]
        early = c.get("early_stop") or _EARLY_STOP_DEFAULTS
        WARMUP_FRAC = float(early["warmup_frac"])
        STREAK_FRAC = float(early["streak_frac"])
    except (OSError, KeyError, TypeError, ValueError) as exc:
        fatal(f"contracts.json field missing/unusable ({exc}) — the watchdog "
              f"cannot supervise without the pinned budget/direction")
    if EPOCHS < 1:
        fatal(f"contracts.json full_train_budget.epochs must be an int >= 1 "
              f"(got {EPOCHS})")


def _load_budget() -> None:
    global BUDGET
    try:
        anchor = json.loads(
            (ART / "base" / "origin_anchor.json").read_text(encoding="utf-8"))
        BUDGET = float(anchor["accuracy_budget"])
    except (OSError, KeyError, TypeError, ValueError) as exc:
        fatal(f"origin anchor unreadable ({exc}) — the accuracy budget is "
              f"the frozen anchor, never a guess")
    if BUDGET < 0:
        fatal(f"origin anchor accuracy_budget must be >= 0 (got {BUDGET})")


def _check_startup_files() -> None:
    for rel in ("contracts.json", "readiness/readiness.json",
                "templates/run_eval.template.sh", "scripts/metric_curve.py",
                "scripts/verdict_decide.py", "scripts/history_lib.py",
                "scripts/ledger_aggregate.py", "scripts/device_alloc.py",
                "scripts/render_run.sh", "baseline/baseline_metrics.jsonl"):
        if not (ART / rel).is_file():
            wlog(f"FATAL startup: missing {rel}")
            print(f"FATAL: upstream artifact missing: {ART / rel} (contract "
                  f"stage incomplete or launch torn)", file=sys.stderr)
            sys.exit(2)
    if not RENDERED.is_file():
        wlog("FATAL startup: train.rendered.sh missing")
        print(f"FATAL: {RENDERED} missing (no rendered training to supervise)",
              file=sys.stderr)
        sys.exit(2)


# ── small helpers ─────────────────────────────────────────────────────────────

def pid_from_file() -> int:
    try:
        return int(TPID.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def group_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(-pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def cmdline_matches(pid: int, expect: str) -> bool:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    if not cmdline:
        return False
    return expect in cmdline.replace(b"\0", b" ").decode("utf-8", "replace")


def read_status() -> dict:
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}           # no status yet: a fresh launch, not an anomaly
    except json.JSONDecodeError as exc:
        fatal(f"train_status.json unparseable: {exc} (single-writer file — a "
              f"torn read is a real anomaly)")


def stage_now() -> str:
    stage = read_status().get("stage", "")
    if stage and stage not in TRAIN_STAGES:
        fatal(f"train_status.json stage {stage!r} is outside the v7 enum")
    return stage if stage in TERMINAL_STAGES else ""


def print_status() -> dict:
    doc = read_status()
    print(json.dumps(doc, sort_keys=True))
    return doc


KEEP = object()   # write_status sentinel: preserve the recorded value


def write_status(stage: str, epoch=KEEP, metric=KEEP, gap=KEEP,
                 streak=KEEP, stopped=KEEP) -> None:
    """Atomic train_status.json write; KEEP preserves the recorded value (a
    failed extract cycle must not silently wipe the streak — the early stop
    would be postponed by a transient parse failure)."""
    previous = read_status()
    if not isinstance(previous, dict):
        previous = {}

    def maybe(value, conv, key):
        if value is KEEP:
            return previous.get(key)
        return None if value is None else conv(value)

    doc = {"vid": VID, "stage": stage,
           "epoch": maybe(epoch, int, "epoch"),
           "metric": maybe(metric, float, "metric"),
           "gap": maybe(gap, float, "gap"),
           "over_budget_streak": maybe(streak, int, "over_budget_streak"),
           "stopped_at_epoch": maybe(stopped, int, "stopped_at_epoch"),
           "device": DEVICE, "ts": _utc_now()}
    try:
        _atomic_write_json(STATUS, doc)
    except OSError as exc:
        fatal(f"writing train_status.json failed: {exc}")


def update_shard(status: str, epoch=KEEP, metric=KEEP, gap=KEEP) -> None:
    """The variant's single-writer ledger shard; the proposal-seeded
    change_summary is the one field preserved verbatim."""
    existing: dict = {}
    if SHARD.is_file():
        try:
            existing = json.loads(SHARD.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            fatal(f"ledger shard unparseable: {SHARD} ({exc}) — single-writer "
                  f"file, never patched around")
    if not isinstance(existing, dict):
        fatal(f"ledger shard is not a JSON object: {SHARD}")

    def maybe(value, conv, key):
        if value is KEEP:
            return existing.get(key)
        return None if value is None else conv(value)

    doc = {"vid": VID, "status": status,
           "epoch": maybe(epoch, int, "epoch"),
           "metric": maybe(metric, float, "metric"),
           "gap": maybe(gap, float, "gap"),
           "device": DEVICE,
           "change_summary": existing.get("change_summary"),
           "ts": _utc_now()}
    try:
        _atomic_write_json(SHARD, doc)
    except OSError as exc:
        fatal(f"updating the ledger shard failed: {exc}")


def push_curves() -> None:
    # best-effort live line (never stalls the guardian)
    try:
        subprocess.run([PY, str(ART / "scripts" / "push_curves.py"),
                        "--artifacts", str(ART)],
                       capture_output=True, timeout=30)
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        wlog(f"WARN: push_curves failed: {exc}")


def release_device() -> None:
    # idempotent terminal sweep (no double release)
    try:
        result = device_alloc.release(ART, DEVICE)
        if result.get("released"):
            wlog(f"stage=device_release idx={DEVICE}")
    except Exception as exc:  # noqa: BLE001 — disclosed, never fatal here
        wlog(f"WARN: device lock release failed for idx={DEVICE} "
             f"({exc}) — the report sweep covers it")


def append_history(outcome: str, **kwargs) -> None:
    try:
        append_terminal(ART / "history.jsonl", VID, outcome=outcome, **kwargs)
    except Exception as exc:  # noqa: BLE001 — disclosed via fatal below
        fatal(f"history append_terminal failed for vid={VID} "
              f"outcome={outcome} ({exc}) — no terminal row was written")


# ── the terminal tail ─────────────────────────────────────────────────────────
# status + shard + push + aggregate + release + history row + rules marker.
def finalize_terminal(outcome: str, status_stage: str, epoch, metric, gap,
                      streak, stopped, **history_kwargs) -> dict:
    # status_stage is the train_status.json lifecycle stage; a history row's
    # `stage` kwarg (the failure attribution) rides history_kwargs.
    # The standard terminal extras ride along unless a caller overrode them
    # (KEEP means "preserve the recorded status field" — never a history value):
    if gap is not None and gap is not KEEP:
        history_kwargs.setdefault("gap", gap)
    if stopped is not None and stopped is not KEEP:
        history_kwargs.setdefault("stopped_at_epoch", stopped)
    if outcome == "accuracy_fail" and streak is not None and streak is not KEEP:
        history_kwargs.setdefault("over_budget_streak", streak)
    write_status(status_stage, epoch, metric, gap, streak, stopped)
    update_shard(outcome, epoch, metric, gap)
    push_curves()
    try:
        subprocess.run([PY, str(ART / "scripts" / "ledger_aggregate.py"),
                        "--artifacts", str(ART)], capture_output=True,
                       timeout=60)
    except Exception as exc:  # noqa: BLE001
        wlog(f"WARN: ledger aggregate failed at terminal ({exc}) — the next "
             f"trigger point converges; shard data is intact")
    release_device()
    append_history(outcome, **history_kwargs)
    try:
        RULES_PENDING.write_text(json.dumps(
            {"vid": VID, "outcome": outcome, "ts": _utc_now()},
            sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        wlog(f"WARN: .rules_pending write failed: {exc}")
    wlog(f"stage=terminal outcome={outcome} stage_name={status_stage} "
         f"epoch={epoch} "
         f"stopped_at={stopped} gap={gap} streak={streak}")
    return print_status()


def finalize_probe_insufficient(stage_name: str, max_retries_hit: bool,
                                epoch=None) -> dict:
    return finalize_terminal(
        "probe_insufficient", "failed", epoch, None, None, None, None,
        stage=stage_name, max_retries_hit=max_retries_hit)


# ── curve extraction (single source: metric_curve) ───────────────────────────

def extract_curve(log: Path, out: Path, expected_epochs: int | None = None
                  ) -> tuple[bool, str]:
    """Incremental extract. Returns (ok, note). Transient early states keep
    the previous curve untouched; the pattern-mismatch failure names the
    pattern and lands in watchdog.log (v7 §7.2 曲线 row)."""
    try:
        pattern = metric_curve._contract_pattern(ART / "contracts.json")
        points, duplicates = metric_curve._extract(log, pattern)
    except metric_curve.MetricCurveError as exc:
        note = str(exc)
        if "has no lines yet" in note or "training log not found" in note:
            return False, note        # transient: no rows yet — next cycle
        wlog(f"stage=curve verdict=fail {note}")
        return False, note            # pattern drift — disclosed, not fatal
    if expected_epochs is not None and len(points) != expected_epochs:
        note = (f"expected {expected_epochs} epoch metrics, parsed "
                f"{len(points)}")
        wlog(f"stage=final_check verdict=fail {note}")
        return False, note
    tmp = out.with_name(out.name + f".tmp.{os.getpid()}")
    tmp.write_text("".join(json.dumps(p, sort_keys=True) + "\n"
                           for p in points), encoding="utf-8")
    os.replace(tmp, out)
    if duplicates:
        # one disclosure per duplicate epoch set — surfaced, not swallowed
        wlog(f"stage=curve note=duplicate epoch lines, last wins: "
             f"{duplicates}")
    return True, ""


def max_epoch() -> int:
    try:
        pattern = metric_curve._contract_pattern(ART / "contracts.json")
        points, _ = metric_curve._extract(TLOG, pattern)
        return max(int(p["epoch"]) for p in points)
    except metric_curve.MetricCurveError:
        return 0


# ── per-epoch judgment (warmup, one count per NEW epoch, streak) ──────────────

def judgment_scan() -> dict:
    status = read_status()
    prev_epoch = int(status.get("epoch") or 0)
    prev_streak = int(status.get("over_budget_streak") or 0)
    try:
        base = metric_curve.load_curve(
            ART / "baseline" / "baseline_metrics.jsonl")
        cand = metric_curve.load_curve(METRICS)
    except metric_curve.MetricCurveError as exc:
        return {"epoch": prev_epoch, "streak": prev_streak, "gap": None,
                "metric": None, "stop": False, "skip": f"curve not ready: {exc}"}

    base_m = {int(r["epoch"]): float(r["metric"]) for r in base}
    cand_m = {int(r["epoch"]): float(r["metric"]) for r in cand}
    common = sorted(set(base_m) & set(cand_m))
    if not common:
        return {"epoch": prev_epoch, "streak": prev_streak, "gap": None,
                "metric": None, "stop": False,
                "skip": "no common epoch with the baseline yet"}

    warmup = math.ceil(WARMUP_FRAC * EPOCHS)
    # v7 §7.2: max(2, ceil(streak_frac x E)) — from contracts.json, never 10
    threshold = max(2, math.ceil(STREAK_FRAC * EPOCHS))
    streak, gap, metric, upto, stop = prev_streak, None, None, prev_epoch, False
    for e in common:
        if e <= prev_epoch:
            continue
        if e <= warmup:                # warmup: never judged, never counted
            upto = e
            continue
        b, c = base_m[e], cand_m[e]
        loss = metric_curve.normalize_loss(b, c, DIRECTION)
        streak = 0 if loss <= BUDGET else streak + 1
        gap, metric, upto = loss, c, e
        if streak >= threshold:
            stop = True
            break
    return {"epoch": int(upto), "streak": int(streak), "gap": gap,
            "metric": metric, "stop": stop, "threshold": threshold,
            "warmup": warmup}


# ── early-stop kill (attribution first) ───────────────────────────────────────

def early_stop_kill(gap, streak, epoch) -> None:
    pid = pid_from_file()
    if not group_alive(pid):
        return          # the training died by itself — the next cycle reads rc
    if not cmdline_matches(pid, "train.rendered.sh"):
        fatal(f"refusing to kill pid {pid} — /proc cmdline does not reference "
              f"'train.rendered.sh' (pid reuse or wrong pid file: {TPID}); no "
              f"terminal written (torn workspace)")
    wlog(f"stage=early_stop streak={streak} gap={gap}: TERM process group {pid}")
    try:
        os.kill(-pid, signal.SIGTERM)
    except OSError:
        pass
    grace = 0
    while group_alive(pid) and grace < 10:
        time.sleep(1)
        grace += 1
    if group_alive(pid):
        wlog(f"stage=early_stop group survived {grace}s grace — "
             f"KILL process group {pid}")
        try:
            os.kill(-pid, signal.SIGKILL)
        except OSError:
            pass
        grace = 0
        while group_alive(pid) and grace < 5:
            time.sleep(1)
            grace += 1
    frozen = max_epoch()
    if frozen < epoch:
        fatal(f"frozen log re-parse found max epoch {frozen} < the epoch the "
              f"kill decided on ({epoch}) — inconsistent log state")
    finalize_terminal("accuracy_fail", "killed", frozen, None, gap, streak,
                      frozen, stopped_at_epoch=frozen,
                      over_budget_streak=streak)
    sys.exit(0)


# ── eval chain (rc == 0) ──────────────────────────────────────────────────────

def resolve_ckpt(kth: int | None = None) -> str:
    pattern = CKPT_RULE.replace("{out_dir}", str(TRAIN_DIR))
    if kth is None:
        if "*" not in pattern:
            p = Path(pattern)
            if not p.is_file():
                raise RuntimeError(
                    f"FATAL: ckpt rule predicts {p} but it does not exist")
            return str(p)
        hits = sorted(glob.glob(pattern), key=os.path.getmtime)
        if not hits:
            raise RuntimeError(f"FATAL: ckpt rule glob matched nothing: {pattern}")
        return hits[-1]
    if "*" not in pattern:
        raise RuntimeError(
            "FATAL: per-epoch ckpt addressing needs a glob ckpt_output_rule")
    hits = sorted(glob.glob(pattern), key=os.path.getmtime)
    if len(hits) < kth:
        raise RuntimeError(f"FATAL: ckpt rule glob matched {len(hits)} files "
                           f"< k={kth}: {pattern}")
    return hits[kth - 1]


def render_eval(ckpt: str, out_rendered: Path, log: Path) -> None:
    proc = subprocess.run(
        ["bash", str(ART / "scripts" / "render_run.sh"),
         "--template", str(ART / "templates" / "run_eval.template.sh"),
         "--out", str(out_rendered),
         "--set", f"ckpt={ckpt}", "--set", f"log={log}",
         "--set", f"shadow_dir={VDIR / 'shadow'}",
         "--set", f"shadow_pkgs={SHADOW_PKGS}",
         "--set", f"project_root={PROJ_ROOT}",
         "--set", f"python={PY}"],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(
            f"FATAL: eval render failed: {proc.stderr.strip()[-300:]}")


def run_rendered(rendered: Path, log: Path) -> None:
    with open(log, "ab") as fh:
        proc = subprocess.run(["bash", str(rendered)], stdout=fh,
                              stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"FATAL: eval run rc != 0 (log: {log})")


def extract_metric(log: Path) -> float:
    rule = json.loads(
        (ART / "contracts.json").read_text(encoding="utf-8")
    )["eval"]["metric_extraction"]
    text = log.read_text(encoding="utf-8", errors="replace")
    if rule["kind"] == "stdout_regex":
        m = re.search(rule["pattern"], text, re.MULTILINE)
        if not m:
            raise RuntimeError(f"FATAL: metric regex did not match in {log}")
        raw = m.group(1) if m.groups() else m.group(0)
        return float(raw)
    if rule["kind"] == "json":
        data = json.loads(text)
        for part in rule["json_pointer"].strip("/").split("/"):
            data = data[int(part)] if isinstance(data, list) else data[part]
        return float(data)
    raise RuntimeError(f"FATAL: unknown metric_extraction kind {rule['kind']!r}")


def baseline_full_acc() -> float:
    path = ART / "baseline" / "baseline_full_acc.json"
    try:
        return float(json.loads(
            path.read_text(encoding="utf-8"))["baseline_full_acc"])
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"FATAL: baseline_full_acc unreadable ({exc})") from exc


def write_final_acc(acc: float, base_full: float) -> None:
    budget = json.loads(
        (ART / "contracts.json").read_text(encoding="utf-8")
    )["full_train_budget"]
    _atomic_write_json(FINAL_ACC, {
        "vid": VID, "final_acc": acc,
        "baseline_full_acc": base_full, "metric_direction": DIRECTION,
        "full_train_budget": budget, "within_budget": None})


def final_verdict(gap: float, acc: float) -> None:
    proc = subprocess.run(
        [PY, str(ART / "scripts" / "verdict_decide.py"), "final-budget",
         "--artifacts", str(ART), "--vid", VID],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError("FATAL: final-budget verdict failed "
                           f"({proc.stderr.strip()[-300:]})")
    verdict = json.loads(
        FINAL_ACC.read_text(encoding="utf-8"))["within_budget"]
    wlog(f"stage=full_eval acc={acc} baseline_full={base_full_display()} "
         f"within_budget={verdict} gap={round(gap, 6)}")
    if verdict is True:
        finalize_terminal("success", "done", EPOCHS, acc, gap, None, EPOCHS,
                          stopped_at_epoch=EPOCHS, final_acc=acc)
    else:
        finalize_terminal("accuracy_fail", "done", EPOCHS, acc, gap, None,
                          EPOCHS, stopped_at_epoch=EPOCHS)
    sys.exit(0)


def base_full_display() -> str:
    return f"{baseline_full_acc():g}"


def finalize_natural() -> None:
    """rc == 0 — final check, eval chain, verdict, terminal. Exits on every
    terminal path; RETURNS only on the waiting-for-anchor path."""
    baseline_path = ART / "baseline" / "baseline_full_acc.json"
    if not baseline_path.is_file() or baseline_path.stat().st_size == 0:
        if (ART / "baseline" / "train_final.json").is_file():
            wlog("stage=final_eval verdict=probe_insufficient (baseline "
                 "reached train_final without baseline_full_acc.json — the "
                 "comparison anchor is unreachable)")
            finalize_probe_insufficient("baseline_anchor_unavailable", False,
                                        EPOCHS)
            sys.exit(0)
        wlog("stage=final_eval_waiting (baseline_full_acc.json not yet on disk)")
        # B12 fix: while waiting, KEEP the last known epoch/metric/gap (a
        # null rewrite wiped the recorded progress in v6)
        write_status("waiting")
        update_shard("training")
        push_curves()
        print_status()
        return

    # final check: the log must prove exactly the rendered epoch count —
    # the failure reason lands VERBATIM in watchdog.log (no guessing)
    ok, note = extract_curve(TLOG, METRICS, expected_epochs=EPOCHS)
    if not ok:
        wlog(f"stage=final_check verdict=probe_insufficient ({note})")
        finalize_probe_insufficient("final_check", False, max_epoch())
        sys.exit(0)
    push_curves()

    try:
        ckpt = resolve_ckpt(None)
        rendered = EDIR / ".final_eval.rendered.sh"
        render_eval(ckpt, rendered, EDIR / "final_eval.log")
        run_rendered(rendered, EDIR / "final_eval.log")
        acc = extract_metric(EDIR / "final_eval.log")
        base_full = baseline_full_acc()
        write_final_acc(acc, base_full)
    except RuntimeError as exc:
        wlog(f"stage=full_eval verdict=probe_insufficient ({exc})")
        finalize_probe_insufficient("final_eval", False, EPOCHS)
        sys.exit(0)

    if CKPT_PER_EPOCH:
        # k-th ckpt eval at the probe depth (auxiliary evidence — disclosed
        # on failure, never a gate on the final verdict)
        try:
            kckpt = resolve_ckpt(PROBE_K)
            k_rendered = EDIR / ".k_eval.rendered.sh"
            render_eval(kckpt, k_rendered, EDIR / "k_eval.log")
            run_rendered(k_rendered, EDIR / "k_eval.log")
            kacc = extract_metric(EDIR / "k_eval.log")
            _atomic_write_json(EDIR / "k_acc.json", {
                "vid": VID, "k": PROBE_K, "ckpt": kckpt, "k_acc": kacc,
                "full_train_budget": json.loads(
                    (ART / "contracts.json").read_text(encoding="utf-8")
                )["full_train_budget"]})
            wlog(f"stage=k_eval acc={kacc} k={PROBE_K} ckpt={kckpt}")
        except (RuntimeError, OSError, ValueError) as exc:
            wlog(f"stage=k_eval verdict=failed (disclosed, non-gating): {exc}")

    gap = metric_curve.normalize_loss(base_full, acc, DIRECTION)
    final_verdict(gap, acc)


# ── SIGTERM (the platform is tearing the run down) ────────────────────────────

def _on_sigterm(signum, frame):  # noqa: ARG001
    global SIGTERM_SEEN
    SIGTERM_SEEN = True


def handle_sigterm() -> None:
    """Attribution-checked kill of the training group + terminal + release
    (v7 §7.2 轮询 row). Never an orphan card behind a stopped guardian."""
    wlog("stage=sigterm received — stopping the training honestly")
    pid = pid_from_file()
    if pid > 0 and group_alive(pid):
        if cmdline_matches(pid, "train.rendered.sh"):
            try:
                os.kill(-pid, signal.SIGTERM)
            except OSError:
                pass
            grace = 0
            while group_alive(pid) and grace < 10:
                time.sleep(1)
                grace += 1
            if group_alive(pid):
                try:
                    os.kill(-pid, signal.SIGKILL)
                except OSError:
                    pass
        else:
            wlog(f"stage=sigterm pid {pid} failed the attribution check — "
                 f"not signalled (pid reuse)")
    frozen = max_epoch()
    # KEEP the last known metric/gap through the terminal write — the crash
    # disclosure must not wipe the record the curves already published
    finalize_terminal("probe_insufficient", "killed", KEEP, KEEP, KEEP, KEEP,
                      frozen, stage="sigterm", max_retries_hit=False,
                      stopped_at_epoch=frozen)
    sys.exit(0)


# ── one supervision cycle ─────────────────────────────────────────────────────

def supervise_cycle() -> None:
    # terminal replay: never restart, never re-kill, never re-release
    stage = stage_now()
    if stage:
        wlog(f"terminal already present (stage={stage}) — replaying, "
             f"nothing to do")
        print_status()
        sys.exit(0)

    # heartbeat: the run lock's mtime says this workspace is alive (F5 —
    # a long training must never look stale to the reuse gate)
    try:
        RUN_LOCK.touch(exist_ok=True)
    except OSError as exc:
        wlog(f"WARN: cannot touch .run_lock heartbeat ({exc})")

    # rc written: the wrapper finished — its rc is the branch selector
    if TRC.is_file() and TRC.stat().st_size > 0:
        try:
            rc = int(TRC.read_text(encoding="utf-8").strip())
        except ValueError:
            fatal(f"{TRC} is not an int: {TRC.read_text(encoding='utf-8')!r}")
        wlog(f"stage=train_exit rc={rc}")
        if rc != 0:
            finalize_probe_insufficient("train", False, max_epoch())
            sys.exit(0)
        finalize_natural()      # exits on its terminal paths
        return

    # crash scene (v7 崩溃 row): the group died WITHOUT an rc file —
    # terminal failed, attribution + log paths disclosed. No relaunch.
    if not group_alive(pid_from_file()):
        pid = pid_from_file()
        wlog(f"stage=crash pid={pid} (train group died without rc — "
             f"attempts log: {TLOG}, wrapper log: "
             f"{TRAIN_DIR / 'wrapper.log'})")
        finalize_probe_insufficient("crash", False, max_epoch())
        sys.exit(0)

    # alive: incremental curve (atomic replace on content change)
    curve_ok, curve_note = extract_curve(TLOG, METRICS)
    if not curve_ok and ("has no lines yet" not in curve_note
                         and "not found" not in curve_note):
        pass  # pattern drift already logged inside extract_curve (fail loud)

    scan = judgment_scan()
    if curve_ok and scan.get("stop") is True:
        early_stop_kill(scan.get("gap"), scan.get("streak"), scan.get("epoch"))
        # a race let the training die first — fall through to the side effects

    if curve_ok:
        write_status("training", scan.get("epoch"), scan.get("metric"),
                     scan.get("gap"), scan.get("streak"), None)
        update_shard("training", scan.get("epoch"), scan.get("metric"),
                     scan.get("gap"))
    else:
        # transient extract failure: KEEP the recorded progress (a null
        # rewrite would silently reset the streak and postpone the early stop)
        write_status("training")
        update_shard("training")
    push_curves()
    wlog(f"alive epoch={scan.get('epoch')} streak={scan.get('streak')} "
         f"gap={scan.get('gap')}")
    print_status()


def main() -> int:
    global VID, DEVICE, ONCE, ART, VDIR, TRAIN_DIR, TLOG, TPID, TRC, RENDERED
    global METRICS, STATUS, SHARD, WPID, WLOG, EDIR, FINAL_ACC
    global RULES_PENDING, RUN_LOCK

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vid", required=True)
    ap.add_argument("--device", required=True, type=int)
    ap.add_argument("--once", action="store_true")
    ns = ap.parse_args()
    VID, DEVICE, ONCE = ns.vid, ns.device, ns.once
    if DEVICE < 0:
        print("FATAL: --device must be a non-negative integer", file=sys.stderr)
        return 2

    art = os.environ.get("ORCA_ARTIFACTS_DIR")
    if not art:
        print("FATAL: ORCA_ARTIFACTS_DIR not set (watch_variant.py)",
              file=sys.stderr)
        return 2
    ART = Path(art)
    VDIR = ART / "variants" / VID
    TRAIN_DIR = VDIR / "train"
    TLOG = TRAIN_DIR / "train.log"
    TPID = TRAIN_DIR / "train.pid"
    TRC = TRAIN_DIR / "rc"
    RENDERED = TRAIN_DIR / "train.rendered.sh"
    METRICS = VDIR / "metrics" / "metrics.jsonl"
    STATUS = VDIR / "train_status.json"
    SHARD = VDIR / "ledger_entry.json"
    WPID = VDIR / "watchdog.pid"
    WLOG = VDIR / "watchdog.log"
    EDIR = VDIR / "eval"
    FINAL_ACC = EDIR / "final_acc.json"
    RULES_PENDING = VDIR / ".rules_pending"
    RUN_LOCK = ART / ".run_lock"

    for d in (VDIR, TRAIN_DIR, VDIR / "metrics", EDIR):
        d.mkdir(parents=True, exist_ok=True)
    WPID.write_text(f"{os.getpid()}\n", encoding="utf-8")

    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        _load_contracts()
        _load_budget()
        _check_startup_files()
    except SystemExit:
        raise
    wlog(f"watchdog alive: vid={VID} device={DEVICE} pid={os.getpid()} "
         f"epochs={EPOCHS} budget={BUDGET} direction={DIRECTION}"
         f"{' (once mode)' if ONCE else ''}")

    supervise_cycle()
    if ONCE:
        return 0
    # detached guardian: one cycle every 10 s (§7.2)
    while True:
        for _ in range(int(POLL_SECONDS * 10)):
            if SIGTERM_SEEN:
                handle_sigterm()
            time.sleep(0.1)
        supervise_cycle()


if __name__ == "__main__":
    sys.exit(main())
