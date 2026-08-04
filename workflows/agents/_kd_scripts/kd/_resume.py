"""kd._resume — atomic latest.pt read/write + hash/mode verification.

Payload schema (no absolute paths on disk)::

    {
      "state_dict":       <OrderedDict>,
      "optimizer_state":  <dict | None>,
      "scheduler_state":  <dict | None>,
      "epoch":            int,         # last completed epoch (0-indexed)
      "best_metric":      dict | None, # {"epoch": int, "value": float, "kind": str}
      "mode":             "teacher" | "distill",
      "build_cfg_hash":   str,         # sha16 of sort_keys(build_cfg JSON)
      "kd_config_hash":   str,         # sha16 of sort_keys(kd_config JSON); "" for teacher
    }

Discipline:

* **Atomic write**: serialize to a same-fs ``.tmp`` then ``os.replace`` so a
  kill mid-write leaves either the previous latest.pt or the new one, never a
  truncated file.  Caller owns the training loop; this module owns on-disk
  crash-safety.
* **Hashes**: ``sha256(json.dumps(d, sort_keys=True, ensure_ascii=False))[:16]``
  — mode + both hashes must match on resume, else fail loud.
* **Scheduler state mismatch**: if the persisted payload has a scheduler
  state but the current run built no scheduler, drop it with a stderr WARN
  (do not crash).  The caller passes ``scheduler_state=None`` on save when no
  scheduler exists.
* **R1**: the persisted dict is checked to contain only the schema keys above
  (no absolute paths leak in — verified by a test, not enforced here).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import torch


RESUME_SCHEMA_KEYS: frozenset[str] = frozenset(
    {
        "state_dict",
        "optimizer_state",
        "scheduler_state",
        "epoch",
        "best_metric",
        "mode",
        "build_cfg_hash",
        "kd_config_hash",
    }
)


class ResumeMismatchError(RuntimeError):
    """latest.pt exists but mode/hash doesn't match the current run (fail loud)."""


def config_hash(d: Any) -> str:
    """Stable sha16 of a JSON-serialisable dict (sort_keys ⇒ order-independent)."""
    if d is None:
        d = {}
    blob = json.dumps(d, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def save_latest(
    path: Path | str,
    *,
    state_dict: dict,
    optimizer_state: dict | None,
    scheduler_state: dict | None,
    epoch: int,
    best_metric: dict | None,
    mode: str,
    build_cfg_hash: str,
    kd_config_hash: str,
) -> None:
    """Atomically write ``latest.pt`` (tmp + os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state_dict": state_dict,
        "optimizer_state": optimizer_state,
        "scheduler_state": scheduler_state,
        "epoch": int(epoch),
        "best_metric": best_metric,
        "mode": mode,
        "build_cfg_hash": build_cfg_hash,
        "kd_config_hash": kd_config_hash,
    }
    tmp = path.with_name(path.name + ".tmp")
    torch.save(payload, tmp)
    os.replace(tmp, path)


def load_latest(
    path: Path | str | None,
    *,
    expected_mode: str,
    expected_build_cfg_hash: str,
    expected_kd_config_hash: str,
) -> dict | None:
    """Load + verify ``latest.pt``.  Returns ``None`` when absent.

    Fail loud on mode/build_cfg/kd_config hash mismatch.  Caller handles
    scheduler_state drop via :data:`ResumeState.scheduler_state`.
    """
    if path is None:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        blob = torch.load(p, map_location="cpu")
    except Exception as e:  # corrupted tmp etc. — surface loudly, never silent.
        raise ResumeMismatchError(
            f"{p}: failed to load latest.pt ({type(e).__name__}: {e}); "
            f"remove it or investigate before retrying."
        ) from e
    if not isinstance(blob, dict):
        raise ResumeMismatchError(f"{p}: latest.pt payload is not a dict (got {type(blob)})")

    extra = set(blob.keys()) - RESUME_SCHEMA_KEYS
    if extra:
        raise ResumeMismatchError(
            f"{p}: latest.pt payload has unexpected keys {sorted(extra)}; "
            f"allowed {sorted(RESUME_SCHEMA_KEYS)}"
        )

    if blob.get("mode") != expected_mode:
        raise ResumeMismatchError(
            f"{p}: latest.pt mode={blob.get('mode')!r} != current mode={expected_mode!r}; "
            f"resume cross-mode is forbidden (would silently restore wrong weights)."
        )
    if blob.get("build_cfg_hash") != expected_build_cfg_hash:
        raise ResumeMismatchError(
            f"{p}: latest.pt build_cfg_hash mismatch — "
            f"stored={blob.get('build_cfg_hash')!r}, current={expected_build_cfg_hash!r}. "
            f"build_cfg changed since last checkpoint; cannot resume."
        )
    if blob.get("kd_config_hash") != expected_kd_config_hash:
        raise ResumeMismatchError(
            f"{p}: latest.pt kd_config_hash mismatch — "
            f"stored={blob.get('kd_config_hash')!r}, current={expected_kd_config_hash!r}. "
            f"kd_config changed since last checkpoint; cannot resume."
        )
    return blob


# ---------------------------------------------------------------------------
# Resume state — what the trainer wants out of a successful resume.
# ---------------------------------------------------------------------------
class ResumeState:
    """Parsed resume payload — trainer-friendly view."""

    def __init__(self, blob: dict) -> None:
        self.epoch: int = int(blob["epoch"])
        self.state_dict: dict = blob["state_dict"]
        self.optimizer_state: dict | None = blob.get("optimizer_state")
        self.scheduler_state: dict | None = blob.get("scheduler_state")
        self.best_metric: dict | None = blob.get("best_metric")

    @property
    def start_epoch(self) -> int:
        """Next epoch to train — last completed + 1."""
        return self.epoch + 1

    @property
    def best(self) -> dict | None:
        return self.best_metric


def maybe_load(
    path: Path | str | None,
    *,
    expected_mode: str,
    expected_build_cfg_hash: str,
    expected_kd_config_hash: str,
) -> ResumeState | None:
    """Load + verify; return :class:`ResumeState` or ``None`` when absent."""
    blob = load_latest(
        path,
        expected_mode=expected_mode,
        expected_build_cfg_hash=expected_build_cfg_hash,
        expected_kd_config_hash=expected_kd_config_hash,
    )
    if blob is None:
        return None
    return ResumeState(blob)


def warn_scheduler_drop(path: Path | str, reason: str) -> None:
    """B4 — scheduler_state present but current run has no scheduler: drop loudly."""
    print(
        f"[kd._resume] WARN: latest.pt ({path}) carries scheduler_state but the "
        f"current run built no scheduler ({reason}); dropping scheduler state, "
        f"optimizer state is still restored.",
        file=sys.stderr,
    )


__all__ = [
    "ResumeMismatchError",
    "ResumeState",
    "RESUME_SCHEMA_KEYS",
    "config_hash",
    "save_latest",
    "load_latest",
    "maybe_load",
    "warn_scheduler_drop",
]
