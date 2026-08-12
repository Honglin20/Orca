"""latency_table.py —— Puzzle P2.5：per (layer, kind, variant) 单块 latency 实测。

**standalone 单块 latency 模型**(Design A):对每 (layer, kind, variant),测该块**独立**
forward 的 latency。单块 latency 可靠且**加性**——Σ 单块 ≈ block 对整模的贡献(实测:8 块
×~0.07ms ≈ 0.56ms ≈ baseline_whole − floor)。整模 latency = floor(非 block) + Σ block,
其中 floor = baseline_whole − Σ identity_block(mip_select 算)。

避免「整模 replace-1-block」测量的非加性(单块 no_op 在上下文里只显省 0.03ms,但全 drop
省 0.6ms——dispatch/缓存交互使 per-swap savings 不可加)。单块隔离测量去掉上下文耦合,
加性成立。

- passthrough(identity):测原父块单块 latency。
- latency_script_path → 包装它;否则 nas-agent measure_module_latency(100 reps 稳定)。

输出 ``latency_table.jsonl``：``{layer, kind, variant, latency_<unit>, unit}``。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

import torch
import torch.nn as nn

from puzzle_common import (
    BlockMap,
    build_calib_loader,
    capture_parent_activations,
    get_candidate,
    get_module_dummy_input,
    is_passthrough,
    load_external_callable,
    load_father_model,
    load_variant_state_dict,
)


def _measure_block_latency(
    variant_module: nn.Module,
    sample_input: torch.Tensor,
    device: torch.device,
    latency_script_path: str,
) -> float:
    """测单块 latency:默认 PyTorch median ms(100 reps 稳定);latency_script_path → 包装。"""
    if latency_script_path:
        fn = load_external_callable(latency_script_path)
        return float(fn(variant_module, sample_input))
    from nas_agent.latency import measure_module_latency
    return float(measure_module_latency(variant_module, sample_input, device, repetitions=100, warmup=30))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Puzzle P2.5 latency 表(单块 standalone)")
    parser.add_argument("--block_map", required=True)
    parser.add_argument("--flat_model", required=True)
    parser.add_argument("--build_fn", required=True)
    parser.add_argument("--build_cfg", default="")
    parser.add_argument("--block_library", required=True)
    parser.add_argument("--father_state", default="",
                        help="father state_dict(latency 是 shape 级与权重无关,但加载保证块在真实形状)")
    parser.add_argument("--latency_unit", default="ms", choices=["ms", "us", "s"],
                        help="latency 单位(仅标注,不换算数值)")
    parser.add_argument("--latency_script_path", default="")
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    torch.manual_seed(args.seed)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    latency_path = output_dir / "latency_table.jsonl"
    block_library_dir = Path(args.block_library).resolve()
    latency_field = f"latency_{args.latency_unit}"

    try:
        block_map = BlockMap.from_json(args.block_map)
        if not block_map.slots:
            raise ValueError("block_map 无 slot")

        model = load_father_model(
            args.flat_model, args.build_fn, args.build_cfg, args.father_state
        )
        device = torch.device("cpu")
        model.eval().to(device)

        dummy_meta = get_module_dummy_input(args.flat_model)
        calib_loader = build_calib_loader(model, dummy_meta, batch_size=1, device=device)
        activations = capture_parent_activations(model, block_map, calib_loader, device)

        with open(latency_path, "w", encoding="utf-8") as fout:
            for slot in block_map.slots:
                prefix = f"L{slot.layer_idx}_{slot.kind}_"
                ckpt_files = sorted(block_library_dir.glob(f"{prefix}*.pt"))
                if not ckpt_files:
                    raise FileNotFoundError(
                        f"slot {slot.parent_module_path} 在 block_library 找不到 ckpts"
                    )
                parent_in, _ = activations[slot.parent_module_path]
                sample_input = parent_in.to(device)
                if sample_input.dim() >= 1 and sample_input.shape[0] != 1:
                    sample_input = sample_input[:1]

                for ckpt_path in ckpt_files:
                    variant = ckpt_path.stem[len(prefix):]

                    if is_passthrough(variant):
                        # identity = 保留父块 → 测父块单块 latency
                        variant_module = model.get_submodule(slot.parent_module_path)
                    else:
                        entry = get_candidate(variant)
                        if slot.kind not in entry.kinds:
                            continue
                        variant_module = entry.factory(slot).to(device).eval()
                        ckpt = torch.load(
                            ckpt_path, map_location=device, weights_only=False
                        )
                        sd = ckpt.get("state_dict", ckpt) if isinstance(ckpt, dict) else ckpt
                        load_variant_state_dict(
                            variant_module, sd, variant, strict_unexpected=True
                        )

                    latency_val = _measure_block_latency(
                        variant_module, sample_input, device, args.latency_script_path
                    )
                    fout.write(
                        json.dumps(
                            {
                                "layer": slot.layer_idx,
                                "kind": slot.kind,
                                "variant": variant,
                                latency_field: latency_val,
                                "unit": args.latency_unit,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    fout.flush()

        result = {
            "status": "executed",
            "artifacts": [str(latency_path)],
            "assessment": f"latency 表完成(单块 standalone,unit={args.latency_unit})",
            "max_retries_hit": False,
            "healed_files": [],
            "fidelity_retriggered": False,
        }

        # ── 浬 floor:全 block → no_op(零输出)的整模 latency(非 block 固定开销)──
        # 用实测 floor 而非 baseline−Σidentity(后者因 standalone 欠计上下文成本而高估)。
        # mip_select 据此算 selected_whole = floor + Σ chosen_block。
        from puzzle_blocks import make_zero
        from puzzle_common import replace_slot
        for slot in block_map.slots:
            zblk = make_zero(slot).to(device).eval()
            replace_slot(model, slot.parent_module_path, zblk)
        whole_input_floor = torch.randn(
            *list(dummy_meta["shape"]),
            dtype=getattr(torch, str(dummy_meta.get("dtype", "float32"))),
        )
        floor_latency = _measure_block_latency(model, whole_input_floor, device, args.latency_script_path)
        floor_path = output_dir / "latency_floor.json"
        with open(floor_path, "w", encoding="utf-8") as ff:
            json.dump({"floor_latency": floor_latency, "unit": args.latency_unit}, ff)
        result["artifacts"].append(str(floor_path))
        print(f"LATENCY_TABLE: {latency_path}")
        print(f"RESULT_JSON: {json.dumps(result, ensure_ascii=False)}")
        return 0
    except Exception as e:
        tb = traceback.format_exc()
        print(f"ERROR: latency_table 失败 — {type(e).__name__}: {e}\n{tb}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
