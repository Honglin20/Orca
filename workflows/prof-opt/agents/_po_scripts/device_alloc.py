#!/usr/bin/env python3
"""device_alloc.py — the run-scoped training-device allocation ledger (v6 §3.2).

Three deterministic operations over the artifacts workspace (--artifacts):

  acquire --artifacts <ws> --vid <VID> [--pid PID]
      Atomically claims the first free device index: devices/<idx>.lock is
      created with O_CREAT|O_EXCL (atomic against concurrent acquirers) and
      holds {"vid", "pid", "acquired_at", "backend"}. The pid records the
      lock OWNER for free's death-reclaim check: callers that already know
      the long-lived owner process (probe passing the training wrapper /
      watchdog pid at P2+ wiring) pass --pid explicitly; without it the
      lock is owned by THIS short-lived acquirer process, whose death makes
      the lock reclaimable — passing the acquirer's own transient pid and
      expecting permanence is a caller contract error. An existing lock on
      an index — even a dead-pid one, recycling is free's job — moves the
      search to the next index. All indices locked -> {"ok": false} (a
      legitimate wait state, rc 0: the probe node keeps the workflow
      parked, it does not fail).

  free --artifacts <ws>
      The free set = the complement of (real backend occupancy UNION live
      locks). Locks whose pid is dead (ownership check failed) or whose
      content is unparseable are RECYCLED (deleted) and disclosed in the
      output. Real occupancy comes from the backend CLI recorded in
      train_device.json; a missing or failing probe exits 2 — a guessed
      free set is never emitted.

  release --artifacts <ws> --idx <N>
      Deletes devices/<N>.lock (the watchdog's terminal action; po_report's
      last-resort sweep). Idempotent per v6 §7.6: an already-absent lock is
      a no-op disclosed as {"released": false}, never an error.

No cross-run preemption: this ledger is run-scoped only; a device really
held by an outside process shows up through the occupancy probe, not by
stealing locks. All output is a single-line JSON on stdout; bad input fails
loud (stderr message, exit 2).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKENDS = ("npu", "cuda")


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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by another user — still an owner
    return True


def _read_lock(path: Path) -> dict:
    """Parsed lock content; raises on unparseable (the caller decides whether
    that means recycle — free — or hard error — acquire never reads locks)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("vid"), str):
        raise AllocError(f"lock is not a JSON object with a vid: {path}")
    return data


# ── real backend occupancy (fail loud; never a guessed set) ───────────────────

def _run(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise AllocError(
            f"backend probe '{cmd[0]}' not found on PATH — cannot determine "
            f"real device occupancy") from exc
    except subprocess.TimeoutExpired as exc:
        raise AllocError(f"backend probe timed out: {' '.join(cmd)}") from exc
    if proc.returncode != 0:
        raise AllocError(
            f"backend probe failed (rc {proc.returncode}): {' '.join(cmd)} "
            f"stderr: {proc.stderr.strip()[:200]}")
    return proc.stdout


def _csv_rows(text: str) -> list[list[str]]:
    return [[c.strip() for c in line.split(",")]
            for line in text.splitlines() if line.strip()]


def _cuda_occupancy() -> set[int]:
    index_of_uuid: dict[str, int] = {}
    for row in _csv_rows(_run(["nvidia-smi", "--query-gpu=index,uuid",
                               "--format=csv,noheader"])):
        if len(row) >= 2 and row[0].isdigit():
            index_of_uuid[row[1]] = int(row[0])
    busy_uuids = {row[0] for row in _csv_rows(
        _run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid",
              "--format=csv,noheader"])) if len(row) >= 2}
    return {index_of_uuid[u] for u in busy_uuids if u in index_of_uuid}


def _npu_occupancy() -> set[int]:
    """`npu-smi info` table parse: the per-NPU Process column carries the
    live process list ('-' / empty = idle). A table without that column is
    a hard error — the real-machine format is calibrated by the user-side
    NPU E2E (v6 §16), never guessed here."""
    text = _run(["npu-smi", "info"])
    rows: list[list[str]] = []
    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells or any(c and set(c) <= {"=", "-", " "} for c in cells):
            continue  # separator rows
        if any("npu-smi" in c.lower() for c in cells):
            continue  # version banner
        rows.append(cells)
    proc_idx = None
    for cells in rows:
        for i, cell in enumerate(cells):
            if "Process" in cell or "进程" in cell:
                proc_idx = i
                break
        if proc_idx is not None:
            break
    if proc_idx is None:
        raise AllocError(
            "npu-smi info carries no Process column — cannot determine real "
            "NPU occupancy (calibrate the probe on the target NPU host)")
    busy: set[int] = set()
    for cells in rows:
        if not cells or not cells[0].isdigit() or len(cells) <= proc_idx:
            continue
        cell = cells[proc_idx]
        if cell and cell != "-" and cell != "0" and not set(cell) <= {" ", "-"}:
            busy.add(int(cells[0]))
    return busy


def _real_occupancy(backend: str) -> set[int]:
    return _cuda_occupancy() if backend == "cuda" else _npu_occupancy()


# ── operations ────────────────────────────────────────────────────────────────

def acquire(artifacts: Path, vid: str, pid: int | None = None) -> dict:
    if not vid:
        raise AllocError("device_alloc: --vid must be non-empty")
    owner = os.getpid() if pid is None else pid
    if not isinstance(owner, int) or isinstance(owner, bool) or owner <= 0:
        raise AllocError(f"device_alloc: --pid must be a positive int, got {owner!r}")
    device = _load_train_device(artifacts)
    backend = device["backend"]
    count = device["device_count"]
    for idx in range(count):
        path = _lock_path(artifacts, idx)
        fd = None
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue  # locked — try the next index
        payload = {"vid": vid, "pid": owner,
                   "acquired_at": datetime.now(timezone.utc)
                   .isoformat(timespec="seconds"),
                   "backend": backend}
        try:
            os.write(fd, (json.dumps(payload, sort_keys=True) + "\n")
                     .encode("utf-8"))
        finally:
            os.close(fd)
        return {"ok": True, "idx": idx, "lock": f"devices/{idx}.lock", **payload}
    return {"ok": False, "reason": f"all {count} device(s) locked",
            "locked": list(range(count))}


def free(artifacts: Path) -> dict:
    device = _load_train_device(artifacts)
    backend = device["backend"]
    count = device["device_count"]
    real = _real_occupancy(backend)

    live: dict[int, dict] = {}
    recycled: list[dict] = []
    for path in sorted(_devices_dir(artifacts).glob("*.lock"),
                       key=lambda p: p.name):
        if not path.stem.isdigit():
            recycled.append({"idx": path.name, "vid": None, "pid": None,
                             "reason": "non-numeric lock file name"})
            path.unlink()
            continue
        idx = int(path.stem)
        try:
            lock = _read_lock(path)
            pid = lock.get("pid")
            if not isinstance(pid, int) or isinstance(pid, bool):
                raise AllocError(f"lock pid is not an int: {pid!r}")
        except (AllocError, ValueError, json.JSONDecodeError, OSError) as exc:
            recycled.append({"idx": idx, "vid": None, "pid": None,
                             "reason": f"unparseable lock: {exc}"})
            path.unlink()
            continue
        if _pid_alive(pid):
            live[idx] = lock
        else:
            recycled.append({"idx": idx, "vid": lock.get("vid"), "pid": pid,
                             "reason": "owner pid is dead (ownership check "
                                       "failed) — lock reclaimed"})
            path.unlink()

    free_idx = [i for i in range(count)
                if i not in real and i not in live]
    return {"backend": backend, "device_count": count, "free": free_idx,
            "busy_real": sorted(real), "locked": sorted(live),
            "recycled": recycled}


def release(artifacts: Path, idx: int) -> dict:
    if idx < 0:
        raise AllocError(f"device_alloc: --idx must be >= 0, got {idx}")
    path = _lock_path(artifacts, idx)
    if not path.is_file():
        # idempotent per §7.6: an already-released lock is a disclosed no-op
        return {"released": False, "idx": idx,
                "reason": f"no lock at devices/{idx}.lock"}
    path.unlink()
    return {"released": True, "idx": idx}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)

    ap_acquire = sub.add_parser(
        "acquire", help="claim the first free device with an O_EXCL lock")
    ap_acquire.add_argument("--artifacts", required=True)
    ap_acquire.add_argument("--vid", required=True)
    ap_acquire.add_argument(
        "--pid", type=int, default=None,
        help="long-lived owner pid recorded in the lock (default: this "
             "acquirer process — whose death makes the lock reclaimable)")

    ap_free = sub.add_parser(
        "free", help="free set = complement(real occupancy UNION live locks)")
    ap_free.add_argument("--artifacts", required=True)

    ap_release = sub.add_parser(
        "release", help="delete one device lock (idempotent)")
    ap_release.add_argument("--artifacts", required=True)
    ap_release.add_argument("--idx", type=int, required=True)
    ns = ap.parse_args()

    try:
        artifacts = Path(ns.artifacts)
        if ns.command == "acquire":
            result: dict = acquire(artifacts, ns.vid, ns.pid)
        elif ns.command == "free":
            result = free(artifacts)
        else:
            result = release(artifacts, ns.idx)
    except (AllocError, OSError, ValueError) as exc:
        print(f"device_alloc: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
