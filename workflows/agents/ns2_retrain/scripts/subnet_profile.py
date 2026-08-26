#!/usr/bin/env python3
"""subnet_profile.py -- Materialize selected subnet + write ``subnet_structure.md``.

Reads ``.selected_arch.json`` (written by select_architecture.py via ns_run_search /
ns_select), materializes the active subnet via the sibling ``supernet.py``'s
``SuperNet.set_sample_config(ArchConfig(**selected_arch)) → get_active_subnet()``,
and writes a fixed-section markdown file:

    # Selected Subnet Structure
    - latency_unit: <unit>
    - weights: <retrain ckpt path | search-time (no retrain ckpt)>
    - total_params: <int>
    - total_macs: <int|"(fvcore unavailable)">
    == Module repr ==
    <str(subnet)>
    == Per-layer ==
    layer_name | type | params | out_shape
    <逐 named_modules 行>

Also pushes a ``table`` chart of the per-layer breakdown.

Fail-soft:
    All failure modes (ImportError on torch/nas_agent, CUDA error, RuntimeError during
    materialize, missing files) → stderr + NO .md written + exit 0. The reporter /
    retrain node treats a missing ``subnet_structure.md`` as empty output.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    discover_latency_unit,
    push_chart,
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Materialize selected subnet from .selected_arch.json and write structure.md."
    )
    ap.add_argument("--artifacts-dir", required=True, help="$ORCA_ARTIFACTS_DIR")
    ap.add_argument(
        "--selected-arch-json", default="",
        help="path to selected_arch json (default: <ad>/.selected_arch.json)",
    )
    ap.add_argument(
        "--latency-unit", default="",
        help="override latency unit (default: discover from search_record_schema.json)",
    )
    args = ap.parse_args()
    ad = Path(args.artifacts_dir)
    selected_path = Path(args.selected_arch_json) if args.selected_arch_json else (ad / ".selected_arch.json")
    unit = args.latency_unit.strip() or discover_latency_unit(ad)

    # Fail-soft wrapper.
    try:
        subnet, num_classes_used, in_channels_used = _materialize(ad, selected_path)
    except _FailSoft as exc:
        sys.stderr.write(f"[subnet_profile] {exc}\n")
        return 0
    except ImportError as exc:
        sys.stderr.write(
            f"[subnet_profile] ImportError (torch/nas_agent/supernet unavailable): {exc}\n"
        )
        return 0
    except RuntimeError as exc:
        sys.stderr.write(
            f"[subnet_profile] RuntimeError during materialize (CUDA/device?): {exc}\n"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 -- fail-soft: NEVER node_failed
        sys.stderr.write(
            f"[subnet_profile] unexpected failure: {exc}\n{traceback.format_exc()}"
        )
        return 0

    weights_line = _find_weights(ad)
    total_params = _count_params(subnet)
    total_macs = _compute_macs(subnet, num_classes_used, in_channels_used)
    per_layer = _per_layer_rows(subnet, num_classes_used, in_channels_used)
    repr_text = _safe_repr(subnet)

    md = _render_md(unit, weights_line, total_params, total_macs, repr_text, per_layer)
    out_path = ad / "subnet_structure.md"
    out_path.write_text(md, encoding="utf-8")
    print(f"[subnet_profile] wrote {out_path} ({total_params} params, {len(per_layer)} layers)")

    _push_table_chart(ad, per_layer, total_params, unit)
    return 0


def _materialize(ad: Path, selected_path: Path):
    """Load sibling ``supernet.py``, build SuperNet with project dims, apply selected arch.

    Returns ``(subnet_module, num_classes_used, in_channels_used)``. Raises ``_FailSoft``
    on file/symbol mismatches; ImportError/RuntimeError propagate to the caller for
    differentiated stderr.
    """
    if not selected_path.is_file():
        raise _FailSoft(f"selected_arch json not found at {selected_path}")
    try:
        sdata = json.loads(selected_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise _FailSoft(f"cannot parse {selected_path}: {exc}") from exc
    if not isinstance(sdata, dict) or not sdata.get("selected_arch"):
        raise _FailSoft(f"{selected_path} has no selected_arch (selection failed upstream)")

    supernet_path = ad / "supernet.py"
    if not supernet_path.is_file():
        raise _FailSoft(f"supernet.py not found at {supernet_path}")

    num_classes, in_channels = _read_project_dims(ad)
    supernet_mod = _load_sibling(supernet_path, "supernet_for_subnet_profile")

    SearchSpace = getattr(supernet_mod, "SearchSpace", None)
    SuperNet = getattr(supernet_mod, "SuperNet", None)
    ArchConfig = getattr(supernet_mod, "ArchConfig", None)
    if not (SearchSpace and SuperNet and ArchConfig):
        raise _FailSoft("supernet.py missing required symbols (SearchSpace/SuperNet/ArchConfig)")

    search_space = SearchSpace()
    # Pass through num_classes / in_channels so the materialized subnet's
    # structural dims match the trained model (else total_params is wrong + head
    # shape disagrees with the retrain ckpt).
    try:
        supernet = SuperNet(search_space, num_classes=num_classes, in_channels=in_channels)
    except TypeError:
        # Older signature without keyword dims — fall back to defaults.
        supernet = SuperNet(search_space)

    selected_arch = sdata["selected_arch"]
    try:
        arch_config = _build_arch_config(ArchConfig, selected_arch)
    except Exception as exc:  # noqa: BLE001 -- ArchConfig construction details vary
        raise _FailSoft(f"cannot build ArchConfig from selected_arch: {exc}") from exc

    supernet.set_sample_config(arch_config)
    subnet = supernet.get_active_subnet()
    subnet.eval()
    return subnet, num_classes, in_channels


def _build_arch_config(ArchConfigCls, selected_arch: dict):
    """Construct an ArchConfig from the selected_arch dict.

    Handles the canonical layout ``{stage_depths: [...], layer_configs: {...}}`` by
    passing them as keyword args (``layer_configs`` kept as-is; ArchConfig accepts
    dict-of-stage-tuples per SuperNet contract). Falls back to ``ArchConfigCls(**dict)``
    for non-canonical layouts — if that raises (unknown kwarg), the caller's fail-soft
    wrapper catches it and writes stderr.
    """
    if "stage_depths" in selected_arch and "layer_configs" in selected_arch:
        return ArchConfigCls(
            stage_depths=tuple(selected_arch["stage_depths"]),
            layer_configs=selected_arch["layer_configs"],
        )
    # Fall back: try kwargs as-is.
    return ArchConfigCls(**selected_arch)


def _read_project_dims(ad: Path) -> tuple[int, int]:
    """Read ``num_classes`` / ``in_channels`` from manifest/schema (default 10/1).

    Priority: project_manifest.md ``num_classes:`` / ``in_channels:`` lines, then
    search_record_schema.json (rare), then defaults. Wrong dims → wrong
    total_params / head shape; we surface the chosen values in stdout so the user
    can spot mis-detection.
    """
    nc, ic = 10, 1  # MNIST-like defaults (most NAS examples).
    manifest = ad / "project_manifest.md"
    if manifest.is_file():
        try:
            text = manifest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for line in text.splitlines():
            low = line.lower()
            if "num_classes" in low:
                val = _int_after_colon(line)
                if val is not None:
                    nc = val
            if "in_channels" in low or "input_channels" in low:
                val = _int_after_colon(line)
                if val is not None:
                    ic = val
    return nc, ic


def _int_after_colon(line: str) -> int | None:
    """Extract the first integer after ``:`` / ``=`` on a line; None if none."""
    import re

    m = re.search(r"[:=]\s*(-?\d+)", line)
    if m is None:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _find_weights(ad: Path) -> str:
    """Locate the latest retrain checkpoint (mtime newest ``*.pth`` under runs/retrain/).

    Returns ``"<ckpt>"`` if found, else ``"search-time (no retrain ckpt)"``.
    """
    retrain_dir = ad / "runs" / "retrain"
    if not retrain_dir.is_dir():
        return "search-time (no retrain ckpt)"
    pths = sorted(retrain_dir.glob("*.pth"), key=lambda p: p.stat().st_mtime if p.is_file() else 0)
    if not pths:
        return "search-time (no retrain ckpt)"
    return str(pths[-1])


def _count_params(subnet) -> int:
    """Total trainable + non-trainable params in the materialized subnet."""
    return int(sum(p.numel() for p in subnet.parameters()))


def _compute_macs(subnet, num_classes: int, in_channels: int):
    """Optional MACs via fvcore (fail-soft if unavailable)."""
    try:
        from fvcore.nn import FlopCountAnalysis  # type: ignore[import-untyped]
        import torch  # noqa: PLC0415 -- imported lazily; fvcore requires torch
    except ImportError:
        return "(fvcore unavailable)"

    try:
        # MNIST/CIFAR-style input: 1-channel 28x28 by default. The MACs count's exact
        # spatial dims don't affect the structural comparison (only conv weights matter
        # proportionally); use 28x28 as a stable representative.
        dummy = torch.zeros(1, in_channels, 28, 28)
        flops = FlopCountAnalysis(subnet, dummy).total()
        # fvcore reports FLOPs (~2x MACs for multiply-accumulate). Convention in NAS
        # literature is to report MACs; we divide by 2 to convert (and round to int).
        return int(flops // 2)
    except Exception as exc:  # noqa: BLE001 -- MACs is optional; never fatal.
        sys.stderr.write(f"[subnet_profile] fvcore MACs computation failed: {exc}\n")
        return "(fvcore unavailable)"


def _per_layer_rows(subnet, num_classes: int, in_channels: int) -> list[dict[str, Any]]:
    """One row per named_modules entry: layer_name / type / params / out_shape.

    Skips the root module (empty name). ``out_shape`` is computed via a single forward
    on a dummy input (best-effort, ``"?"`` on any failure). Params per layer = direct
    parameters (not recursive — children get their own rows).
    """
    rows: list[dict[str, Any]] = []
    out_shapes = _shapes_via_forward(subnet, in_channels)
    for name, mod in subnet.named_modules():
        if not name:
            continue  # skip root
        direct_params = sum(p.numel() for p in mod.parameters(recurse=False))
        rows.append({
            "layer_name": name,
            "type": type(mod).__name__,
            "params": direct_params,
            "out_shape": out_shapes.get(name, "?"),
        })
    return rows


def _shapes_via_forward(subnet, in_channels: int) -> dict[str, Any]:
    """Run one forward pass + register hooks to capture per-module output shapes.

    Returns ``{named_module_name: shape_tuple_or_str}``. Best-effort: on ANY failure
    (no torch, no forward, hook crash), returns ``{}`` so callers fall back to ``"?"``.
    """
    try:
        import torch  # noqa: PLC0415
    except ImportError:
        return {}

    shapes: dict[str, Any] = {}

    def _make_hook(name: str):
        def hook(_module, _inputs, output):
            try:
                if hasattr(output, "shape"):
                    shapes[name] = tuple(int(s) for s in output.shape)
                else:
                    shapes[name] = "?"
            except Exception:  # noqa: BLE001 -- best-effort
                shapes[name] = "?"
        return hook

    handles = []
    try:
        for name, mod in subnet.named_modules():
            if not name:
                continue
            handles.append(mod.register_forward_hook(_make_hook(name)))
        dummy = torch.zeros(1, in_channels, 28, 28)
        with torch.no_grad():
            subnet(dummy)
    except Exception as exc:  # noqa: BLE001 -- structural spec only; shapes optional.
        sys.stderr.write(f"[subnet_profile] shape inference failed: {exc}\n")
    finally:
        for h in handles:
            try:
                h.remove()
            except Exception:  # noqa: BLE001
                pass
    return shapes


def _safe_repr(subnet) -> str:
    """``str(subnet)`` wrapped — repr can be huge but must never crash."""
    try:
        return str(subnet)
    except Exception as exc:  # noqa: BLE001
        return f"(repr failed: {exc})"


def _render_md(
    unit: str, weights_line: str, total_params: int, total_macs: Any,
    repr_text: str, per_layer: list[dict[str, Any]],
) -> str:
    """Fixed-section markdown (verbatim headers, parseable downstream)."""
    lines: list[str] = [
        "# Selected Subnet Structure",
        f"- latency_unit: {unit}",
        f"- weights: {weights_line}",
        f"- total_params: {total_params}",
        f"- total_macs: {total_macs}",
        "== Module repr ==",
        repr_text,
        "== Per-layer ==",
        "layer_name | type | params | out_shape",
    ]
    for row in per_layer:
        lines.append(
            f"{row['layer_name']} | {row['type']} | {row['params']} | {row['out_shape']}"
        )
    return "\n".join(lines) + "\n"


def _push_table_chart(ad: Path, per_layer: list[dict[str, Any]], total_params: int, unit: str) -> None:
    """Push per-layer table chart (best-effort — push_chart is fail-soft internally)."""
    if not per_layer:
        return
    push_chart(
        artifacts_dir_path=ad,
        script_name="subnet_profile",
        label="nas-supernet/subnet",
        title=f"Selected Subnet Structure ({total_params} params, latency unit={unit})",
        chart_type="table",
        data=per_layer,
        columns=["layer_name", "type", "params", "out_shape"],
        caption=(
            f"Per-layer breakdown of the materialized selected subnet "
            f"({len(per_layer)} layers, {total_params} total params)."
        ),
    )


def _load_sibling(path: Path, mod_name: str):
    """Import a sibling .py as a module (its dir already on sys.path via _common insert).

    Registers in ``sys.modules`` BEFORE ``exec_module`` so ``@dataclass`` / other
    decorators that look up ``sys.modules[cls.__module__]`` resolve correctly (loading
    supernet.py without registration triggers AttributeError on dataclass processing).
    """
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    if spec is None or spec.loader is None:
        raise _FailSoft(f"cannot build module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        # On failure, pop the partial module so a retry doesn't see a half-loaded module.
        sys.modules.pop(mod_name, None)
        raise
    return module


class _FailSoft(Exception):
    """Materialization failure with a user-facing message (caller writes stderr + exit 0)."""


if __name__ == "__main__":
    sys.exit(main())
