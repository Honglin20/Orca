#!/usr/bin/env python3
"""device_alloc.py — the run-scoped training-device allocation ledger (v7 §6).

Judgement lives with the agent, mechanics live here. The agent READS the
backend CLI's raw occupancy output (via probe) and CHOOSES a free card; the
ledger only makes the claim atomic and the lock observable:

  probe  --artifacts <ws> --backend <npu|cuda>
      Observability ONLY: prints {"backend", "device_count", "locks":
      [{idx, vid, pid, acquired_at}...], "raw": "<the backend CLI's
      COMPLETE stdout verbatim>"}. The raw text is NOT parsed here — no
      busy set is computed (v7 deletes the occupancy parsers: reading
      vendor CLI tables was the misjudged "mechanical" half of v6). The
      backend probe command failing or missing exits 2 (fail loud: without
      observation there is no honest selection). device_count comes from
      train_device.json (resolved once at the entry node) and must agree
      with the requested backend.

  claim  --artifacts <ws> --vid <VID> --idx <N>
      The agent-chosen card becomes ours atomically: devices/N.lock is
      created with O_CREAT|O_EXCL holding {"vid", "pid", "acquired_at",
      "backend"}. idx outside [0, device_count) exits 2 (fail loud). An
      existing lock — whatever its state; recycling is release/report
      business — returns {"ok": false, "reason": "device N locked by
      vid=<...>"} with rc 0: the caller re-probes and picks another card.

  adopt  --artifacts <ws> --vid <VID> --pid <PID>
      Rebinds the lock named for VID to a long-lived owner pid (atomic
      tmp+rename; vid/acquired_at/backend preserved). The claim necessarily
      runs BEFORE the detached training wrapper exists (the render needs
      the idx), so the claiming process is only a placeholder owner: the
      caller adopts the wrapper (or guardian) pid as soon as it is alive.
      Exactly one lock must name the vid; zero or several fail loud. The
      new pid must be CONFIRMED alive (pid_lib): adopting an
      unconfirmable/dead pid would pin the card behind a phantom owner
      once the pid is recycled and reused by an unrelated process.

  release --artifacts <ws> --idx <N>
      Deletes devices/N.lock (the watchdog's terminal action; po_report's
      sweep). Idempotent: an already-absent lock is a no-op disclosed as
      {"released": false}, never an error.

  sweep  --artifacts <ws>
      The terminal-harvest BACKSTOP (v7 §6.2 — po_report calls this instead
      of re-implementing the judgment): judge EVERY devices/*.lock by its
      owner pid through pid_lib's three-valued liveness and release the
      locks whose owner is CONFIRMED DEAD. alive -> kept (the owner still
      holds the card); unknown -> kept AND disclosed ("liveness
      unverifiable" — never guessed away); an unparseable lock file ->
      kept AND disclosed (a torn ledger is surfaced, never patched around).
      Output: {"released": N, "locks": [{idx, vid, pid, liveness,
      action}...]} — every lock's verdict is in the output, so the caller's
      disclosure is mechanical. Exit 0 (the sweep itself never fails the
      report; its findings are data).

No cross-run preemption: this ledger is run-scoped only; a device really
held by an outside process shows up in the probe's raw output for the
agent to see, not by stealing locks. All output is a single-line JSON on
stdout; bad input fails loud (stderr message, exit 2).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pid_lib import liveness  # noqa: E402

BACKENDS = ("npu", "cuda")
# the observability command per backend — output passed through VERBATIM
_BACKEND_PROBE = {
    "npu": ["npu-smi", "info"],
    "cuda": ["nvidia-smi"],
}


class AllocError(RuntimeError):
    """Raised on unusable workspace state — callers fail loud, never guess."""


def _load_train_device(artifacts: Path) -> dict:
    path = artifacts / "train_device.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AllocError(
            f"train_device.json missing: {path} (the entry stage resolves it "
            f"once via resolve_train_device.sh — run the entry node first)") from exc
    except json.JSONDecodeError as exc:
        raise AllocError(f"train_device.json unparseable: {path} ({exc})") from exc
    if not isinstance(data, dict) or data.get("backend") not in BACKENDS:
        raise AllocError(
            f"train_device.json must carry a backend of {BACKENDS}: {data!r}")
    count = data.get("device_count")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        raise AllocError(
            f"train_device.json device_count must be a positive int: {count!r}")
    return data


def _devices_dir(artifacts: Path) -> Path:
    d = artifacts / "devices"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lock_path(artifacts: Path, idx: int) -> Path:
    return _devices_dir(artifacts) / f"{idx}.lock"


def _read_lock(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("vid"), str):
        raise AllocError(f"lock is not a JSON object with a vid: {path}")
    return data


def _locks(artifacts: Path) -> list[dict]:
    """The current ledger state, idx-ascending (parsed LOCKS only — our own
    files; the backend occupancy raw text is probe's verbatim pass-through)."""
    out: list[dict] = []
    for path in sorted(_devices_dir(artifacts).glob("*.lock"),
                       key=lambda p: p.name):
        if not path.stem.isdigit():
            continue
        try:
            lock = _read_lock(path)
        except (AllocError, ValueError, json.JSONDecodeError, OSError) as exc:
            out.append({"idx": path.stem, "vid": None, "pid": None,
                        "acquired_at": None,
                        "unparseable": f"{exc}"})
            continue
        out.append({"idx": int(path.stem), "vid": lock.get("vid"),
                    "pid": lock.get("pid"),
                    "acquired_at": lock.get("acquired_at")})
    return out


# ── operations ────────────────────────────────────────────────────────────────

def probe(artifacts: Path, backend: str) -> dict:
    """Run the backend's CLI once and pass its stdout through VERBATIM —
    the agent reads the raw text and judges which card is free. No parsing,
    no busy set, no silent degradation (v7 §6.1)."""
    if backend not in BACKENDS:
        raise AllocError(f"probe: --backend must be one of {BACKENDS}, "
                         f"got {backend!r}")
    device = _load_train_device(artifacts)
    if device["backend"] != backend:
        raise AllocError(
            f"probe: requested backend {backend!r} disagrees with the "
            f"workspace's train_device.json backend {device['backend']!r} "
            f"(resolved once at the entry node; a real disagreement needs "
            f"fresh_start, never a silent switch)")
    cmd = _BACKEND_PROBE[backend]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise AllocError(
            f"backend CLI '{cmd[0]}' not found on PATH — occupancy cannot be "
            f"observed for backend {backend!r}; refusing to guess a free "
            f"card (install the CLI or claim a card you can verify another "
            f"way is impossible here — fail loud)") from exc
    except subprocess.TimeoutExpired as exc:
        raise AllocError(f"backend CLI timed out: {' '.join(cmd)}") from exc
    if proc.returncode != 0:
        raise AllocError(
            f"backend CLI failed (rc {proc.returncode}): {' '.join(cmd)} "
            f"stderr: {proc.stderr.strip()[:200]} — occupancy cannot be "
            f"observed, never guessed")
    return {"backend": backend, "device_count": device["device_count"],
            "locks": _locks(artifacts), "raw": proc.stdout}


def claim(artifacts: Path, vid: str, idx: int) -> dict:
    """Atomically claim the AGENT-CHOSEN idx (v7: no auto-selection)."""
    if not vid:
        raise AllocError("device_alloc: --vid must be non-empty")
    device = _load_train_device(artifacts)
    backend = device["backend"]
    count = device["device_count"]
    if not isinstance(idx, int) or isinstance(idx, bool):
        raise AllocError(f"device_alloc: --idx must be an int, got {idx!r}")
    if not 0 <= idx < count:
        raise AllocError(
            f"claim: idx {idx} is outside [0, {count}) (train_device.json "
            f"device_count) — an out-of-range card cannot be claimed")
    path = _lock_path(artifacts, idx)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        try:
            holder = _read_lock(path).get("vid")
        except (AllocError, ValueError, json.JSONDecodeError, OSError):
            holder = "<unparseable lock>"
        return {"ok": False,
                "reason": f"device {idx} locked by vid={holder}",
                "idx": idx, "lock": f"devices/{idx}.lock"}
    payload = {"vid": vid, "pid": os.getpid(),
               "acquired_at": datetime.now(timezone.utc)
               .isoformat(timespec="seconds"),
               "backend": backend}
    try:
        os.write(fd, (json.dumps(payload, sort_keys=True) + "\n")
                 .encode("utf-8"))
    finally:
        os.close(fd)
    return {"ok": True, "idx": idx, "lock": f"devices/{idx}.lock", **payload}


def release(artifacts: Path, idx: int) -> dict:
    if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
        raise AllocError(f"device_alloc: --idx must be >= 0, got {idx!r}")
    path = _lock_path(artifacts, idx)
    if not path.is_file():
        # idempotent: an already-released lock is a disclosed no-op
        return {"released": False, "idx": idx,
                "reason": f"no lock at devices/{idx}.lock"}
    path.unlink()
    return {"released": True, "idx": idx}


def sweep(artifacts: Path) -> dict:
    """Terminal backstop: release every lock whose owner is CONFIRMED dead
    (v7 §6.2 — the report's card-release sweep, mechanized here so the
    agent never re-implements the liveness judgment)."""
    locks: list[dict] = []
    released = 0
    for path in sorted(_devices_dir(artifacts).glob("*.lock"),
                       key=lambda p: p.name):
        if not path.stem.isdigit():
            continue
        idx = int(path.stem)
        try:
            lock = _read_lock(path)
        except (AllocError, ValueError, json.JSONDecodeError, OSError) as exc:
            locks.append({"idx": idx, "vid": None, "pid": None,
                          "liveness": "unparseable",
                          "action": "kept",
                          "note": f"lock unreadable ({exc}) — surfaced, "
                                  f"never patched around"})
            continue
        pid = lock.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            state = liveness(pid)
        else:
            # an absent/non-positive pid names no owner process — the lock
            # is behind nobody (the deterministic pid_lib.DEAD verdict)
            state = "dead"
        if state == "dead":
            path.unlink()
            released += 1
            locks.append({"idx": idx, "vid": lock.get("vid"), "pid": pid,
                          "liveness": state, "action": "released"})
        else:
            note = None
            if state == "unknown":
                note = ("liveness unverifiable on this host — lock kept, "
                        "never guessed away")
            locks.append({"idx": idx, "vid": lock.get("vid"), "pid": pid,
                          "liveness": state, "action": "kept",
                          **({"note": note} if note else {})})
    return {"released": released, "locks": locks}


def adopt(artifacts: Path, vid: str, pid: int) -> dict:
    """Rebind the vid's lock to a long-lived owner pid (atomic replace)."""
    if not vid:
        raise AllocError("device_alloc: --vid must be non-empty")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise AllocError(f"device_alloc: --pid must be a positive int, got {pid!r}")
    state = liveness(pid)
    if state == "dead":
        # a dead pid (or one about to be recycled and reused by an unrelated
        # process) would pin the card behind a phantom owner forever
        raise AllocError(f"adopt: target owner pid {pid} is not alive — "
                         "adopt only a process you just launched")
    if state == "unknown":
        raise AllocError(
            f"adopt: liveness unverifiable for pid {pid} on this host — "
            f"refusing to adopt an unconfirmable owner (the lock would sit "
            f"behind a possibly-dead pid with nobody able to prove it)")
    matches: list[tuple[int, dict, Path]] = []
    for path in sorted(_devices_dir(artifacts).glob("*.lock"),
                       key=lambda p: p.name):
        if not path.stem.isdigit():
            continue
        try:
            lock = _read_lock(path)
        except (AllocError, ValueError, json.JSONDecodeError, OSError):
            continue
        if lock.get("vid") == vid:
            matches.append((int(path.stem), lock, path))
    if not matches:
        raise AllocError(f"adopt: no lock under devices/ names vid {vid!r}")
    if len(matches) > 1:
        idxs = [m[0] for m in matches]
        raise AllocError(f"adopt: {len(matches)} locks name vid {vid!r} "
                         f"(idx {idxs}) — the ledger is torn, never guess")
    idx, lock, path = matches[0]
    doc = dict(lock)
    doc["pid"] = pid
    tmp = path.with_name(path.name + f".adopt.{os.getpid()}")
    tmp.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return {"adopted": True, "idx": idx, "vid": vid, "pid": pid}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)

    ap_probe = sub.add_parser(
        "probe", help="observe: backend CLI raw stdout + the current locks")
    ap_probe.add_argument("--artifacts", required=True)
    ap_probe.add_argument("--backend", required=True, choices=list(BACKENDS))

    ap_claim = sub.add_parser(
        "claim", help="atomically claim the agent-chosen idx (O_EXCL lock)")
    ap_claim.add_argument("--artifacts", required=True)
    ap_claim.add_argument("--vid", required=True)
    ap_claim.add_argument("--idx", type=int, required=True)

    ap_adopt = sub.add_parser(
        "adopt", help="rebind the vid's lock to a long-lived owner pid")
    ap_adopt.add_argument("--artifacts", required=True)
    ap_adopt.add_argument("--vid", required=True)
    ap_adopt.add_argument("--pid", type=int, required=True)

    ap_release = sub.add_parser(
        "release", help="delete one device lock (idempotent)")
    ap_release.add_argument("--artifacts", required=True)
    ap_release.add_argument("--idx", type=int, required=True)

    ap_sweep = sub.add_parser(
        "sweep", help="terminal backstop: release locks whose owner pid is "
                      "confirmed dead (the report's card-release sweep)")
    ap_sweep.add_argument("--artifacts", required=True)
    ns = ap.parse_args()

    try:
        artifacts = Path(ns.artifacts)
        if ns.command == "probe":
            result: dict = probe(artifacts, ns.backend)
        elif ns.command == "claim":
            result = claim(artifacts, ns.vid, ns.idx)
        elif ns.command == "adopt":
            result = adopt(artifacts, ns.vid, ns.pid)
        elif ns.command == "sweep":
            result = sweep(artifacts)
        else:
            result = release(artifacts, ns.idx)
    except (AllocError, OSError, ValueError) as exc:
        print(f"device_alloc: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
