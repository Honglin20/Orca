#!/usr/bin/env python3
"""perturb_ckpt.py — deterministic perturbed copy of a checkpoint.

General deterministic ckpt-derivation utility: only the model subtree is
perturbed (bare stays bare, wrapper siblings untouched), a fixed seed drives
small noise on the first N floating-point tensors in sorted key order — fully
deterministic, so a re-run reproduces byte-identical semantics. (Disclosure:
no stage of the current workflow pipeline invokes this script — the dual-ckpt
eval evidence now derives its two checkpoints from two differently seeded
random initializations — kept as a general utility.) Historical use: proving
an eval entry actually loads the ckpt it is given (run it once with the
original and once with this copy — the metric must move, a static metric is
a fail-loud contract violation); the live pipeline sources its dual ckpts
from random initializations instead.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import torch


def perturb(ckpt: Path, out: Path, model_key: str | None, num_tensors: int,
            noise: float, seed: int) -> dict:
    container = torch.load(str(ckpt), map_location="cpu", weights_only=False)
    if not isinstance(container, dict):
        raise ValueError(f"checkpoint {ckpt} is not a dict container")
    sd = container if model_key is None else container.get(model_key)
    if sd is None or not isinstance(sd, dict):
        raise KeyError(f"model subtree {model_key!r} not found or not a state_dict")

    float_keys = sorted(k for k, v in sd.items()
                        if isinstance(v, torch.Tensor) and torch.is_floating_point(v))
    if not float_keys:
        raise ValueError("no floating-point tensors found to perturb")
    targets = float_keys[:num_tensors]

    new_sd = dict(sd)
    perturbed = []
    for i, key in enumerate(targets):
        tensor = sd[key]
        gen = torch.Generator().manual_seed(seed + i)
        new_sd[key] = tensor + noise * torch.randn(tensor.shape, generator=gen)

    out.parent.mkdir(parents=True, exist_ok=True)
    if model_key is None:
        torch.save(new_sd, str(out))
    else:
        merged = dict(container)
        merged[model_key] = new_sd
        torch.save(merged, str(out))
    return {"out": str(out.resolve()), "perturbed_keys": targets,
            "seed": seed, "noise": noise}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model-key", default=None)
    ap.add_argument("--num-tensors", type=int, default=3)
    ap.add_argument("--noise", type=float, default=1e-3)
    ap.add_argument("--seed", type=int, default=0)
    ns = ap.parse_args()
    try:
        result = perturb(Path(ns.ckpt), Path(ns.out), ns.model_key,
                         ns.num_tensors, ns.noise, ns.seed)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"perturb_ckpt: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
