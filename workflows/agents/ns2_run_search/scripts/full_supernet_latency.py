#!/usr/bin/env python3
"""full_supernet_latency.py -- Measure the FULL-OPEN supernet's latency.

The search measures per-candidate latency (each is an active subnet extracted from the
supernet). The latency of the fully-expanded supernet itself — every stage at max depth,
every elastic dim at max — was historically proxied by ``max(candidate latencies)``
(``compare_table.py`` fallback). This script produces a REAL measurement using the
SAME ``LatencyEstimator`` the search used, on ``SuperNet(SearchSpace()).arch_config``
(the default max architecture), and writes ``.full_supernet_latency.json`` for
``compare_table.py`` to prefer over the proxy.

File shape:
    {"latency": <number>, "unit": <"ms"|"us"|"s">, "source": <"estimator"|"proxy">}

Unit handling:
    - Default path (no ``latency_script_path``): ``LatencyEstimator`` wraps
      ``measure_module_latency`` which ALWAYS returns ms (CUDA ``elapsed_time`` /
      CPU ``perf_counter*1000``). Write ``unit="ms"`` literally.
    - User-script path (``latency_script_path`` non-empty): the user script's unit
      is whatever the user declared via ``latency_unit`` (default ms if not declared).
      Write ``unit = latency_unit`` (schema-discovered).
    Bootstrap invariant forbids ``latency_unit ∈ {us,s}`` + empty
    ``latency_script_path``, so a non-ms ``unit`` here implies the user-script path
    was actually taken.

Fail-soft:
    All failure modes (ImportError on torch/nas_agent, CUDA error, RuntimeError during
    measure, materialization failure) → stderr message + NO file written + exit 0.
    ``compare_table.py`` falls back to the max(candidate) proxy. This script NEVER
    raises ``node_failed`` (deterministic chart scripts must not fail the node).
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import discover_latency_unit  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Measure full-open supernet latency via the search's LatencyEstimator."
    )
    ap.add_argument("--artifacts-dir", required=True, help="$ORCA_ARTIFACTS_DIR")
    ap.add_argument(
        "--latency-unit", default="",
        help="override latency unit (default: discover from search_record_schema.json)",
    )
    ap.add_argument(
        "--latency-script-path", default="",
        help="user latency script path; non-empty => unit from --latency-unit/schema, "
        "empty => 'ms' (default PyTorch path always returns ms). SPEC §3.3.",
    )
    args = ap.parse_args()
    ad = Path(args.artifacts_dir)
    # SPEC §3.3 unit rule: default path (no user script) ALWAYS returns ms; only the
    # user-script path may carry a non-ms unit. Gating on latency_script_path (not the
    # declared latency_unit alone) prevents mislabeling ms as us/s when this script is
    # invoked directly during dev/test. In normal workflow runs the F1 bootstrap
    # invariant makes both paths agree.
    if args.latency_script_path.strip():
        unit = args.latency_unit.strip() or discover_latency_unit(ad)
    else:
        unit = "ms"

    # Fail-soft wrapper: every failure mode writes stderr + exits 0.
    try:
        latency = _measure(ad)
    except _FailSoft as exc:
        sys.stderr.write(f"[full_supernet_latency] {exc}\n")
        return 0
    except ImportError as exc:
        # torch / nas_agent / supernet module missing — most likely torch not installed.
        sys.stderr.write(
            f"[full_supernet_latency] ImportError (torch/nas_agent/supernet unavailable): "
            f"{exc}\n"
        )
        return 0
    except RuntimeError as exc:
        # CUDA errors / device-side errors typically raise RuntimeError.
        sys.stderr.write(
            f"[full_supernet_latency] RuntimeError during measurement (CUDA/device?): "
            f"{exc}\n"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 -- fail-soft: NEVER node_failed
        # Last-resort catch: write stderr with traceback + exit 0.
        sys.stderr.write(
            f"[full_supernet_latency] unexpected failure: {exc}\n"
            f"{traceback.format_exc()}"
        )
        return 0

    out_path = ad / ".full_supernet_latency.json"
    payload = {"latency": latency, "unit": unit, "source": "estimator"}
    out_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"[full_supernet_latency] wrote {out_path}: {payload}")
    return 0


def _measure(ad: Path) -> float:
    """Materialize supernet + measure full-open latency via sibling latency_estimator.

    Raises ``_FailSoft`` for materialization failures with a clear message (sibling
    module missing / config unreadable / arch invalid). ImportError / RuntimeError /
    generic Exception are caught by the caller for differentiated stderr.
    """
    supernet_path = ad / "supernet.py"
    if not supernet_path.is_file():
        raise _FailSoft(f"supernet.py not found at {supernet_path}")
    estimator_path = ad / "latency_estimator.py"
    if not estimator_path.is_file():
        raise _FailSoft(f"latency_estimator.py not found at {estimator_path}")

    cfg = _load_latency_cfg(ad)
    if cfg is None:
        raise _FailSoft(
            "search_config.yaml missing or has no latency_cfg — cannot measure "
            "(need warmup/repetitions/batch_size)"
        )

    # Sibling imports: load supernet.py + latency_estimator.py as modules from the
    # artifacts dir (their imports of nas_agent / torch resolve via sys.path).
    supernet_mod = _load_sibling(supernet_path, "supernet_for_full_latency")
    estimator_mod = _load_sibling(estimator_path, "estimator_for_full_latency")

    SearchSpace = getattr(supernet_mod, "SearchSpace", None)
    SuperNet = getattr(supernet_mod, "SuperNet", None)
    LatencyEstimator = getattr(estimator_mod, "LatencyEstimator", None)
    if SearchSpace is None or SuperNet is None or LatencyEstimator is None:
        raise _FailSoft(
            "supernet.py / latency_estimator.py missing required symbols "
            "(SearchSpace, SuperNet, LatencyEstimator)"
        )

    search_space = SearchSpace()
    # Note: num_classes / in_channels are NOT passed here (unlike subnet_profile.py).
    # Rationale: latency is dominated by conv stages; the final linear head's dim
    # contribution is below timer noise. SuperNet's own defaults (10/1) are accepted.
    supernet = SuperNet(search_space)

    # The supernet's default arch_config IS the full-open max architecture (every
    # stage at max depth + first choice with max config). See SuperNet.__init__
    # ``default_depths = tuple(max(dc) for dc in search_space.stage_depth_candidates)``.
    max_arch = supernet.arch_config

    # device is intentionally NOT hardcoded: the sibling latency_estimator.py's
    # LatencyEstimator has its own default (typically CPU; matches search path).
    # Pinning device here would diverge from search-time candidate measurements
    # and break the Full Supernet vs Selected Subnet comparison. If a project's
    # estimator requires an explicit device, the constructor will raise and the
    # outer fail-soft wrapper writes stderr + skips the file.
    estimator = LatencyEstimator(search_space, cfg)
    latency = estimator.get_latency(max_arch)
    return float(latency)


def _load_latency_cfg(ad: Path):
    """Read ``latency_cfg`` block from ``search_config.yaml`` (warmup/reps/batch_size).

    Returns an OmegaConf-like object (duck-typed: ``.warmup`` / ``.repetitions`` /
    ``.batch_size``) — if the latency_estimator was generated against OmegaConf, we use
    that; otherwise a small namespace wrapper. Falls back to None on missing/malformed.
    """
    cfg_path = ad / "search_config.yaml"
    if not cfg_path.is_file():
        return None
    try:
        import yaml  # type: ignore[import-untyped]

        with cfg_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (ImportError, OSError, ValueError):
        # ValueError covers yaml.YAMLError (a ValueError subclass when present) +
        # malformed-input parse failures.
        return None
    if not isinstance(data, dict):
        return None
    cfg = data.get("latency_cfg")
    if not isinstance(cfg, dict):
        return None
    # Try OmegaConf first (matches the generated latency_estimator's expectation).
    try:
        from omegaconf import OmegaConf  # type: ignore[import-untyped]

        return OmegaConf.create(cfg)
    except ImportError:
        # Fall back to a simple namespace exposing attribute access.
        return _SimpleLatencyCfg(cfg)


class _SimpleLatencyCfg:
    """Minimal attribute-access wrapper when OmegaConf isn't installed."""

    def __init__(self, raw: dict) -> None:
        self.warmup = int(raw.get("warmup", 10))
        self.repetitions = int(raw.get("repetitions", 50))
        self.batch_size = int(raw.get("batch_size", 1))


def _load_sibling(path: Path, mod_name: str):
    """Import a sibling .py as a module (its dir already on sys.path via _common insert).

    Registers in ``sys.modules`` BEFORE ``exec_module`` so ``@dataclass`` / other
    decorators that look up ``sys.modules[cls.__module__]`` resolve correctly.
    """
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    if spec is None or spec.loader is None:
        raise _FailSoft(f"cannot build module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    return module


class _FailSoft(Exception):
    """Materialization failure with a user-facing message (caller writes stderr + exit 0)."""


if __name__ == "__main__":
    sys.exit(main())
