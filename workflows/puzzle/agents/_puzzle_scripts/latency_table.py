"""latency_table.py —— Puzzle per (layer, kind, variant) 单块 latency 实测（U6 适配器）。

**standalone 单块 latency 模型**(Design A)：对每 (layer, kind, variant)，测该块**独立**
forward 的 latency。单块 latency 可靠且**加性**——Σ 单块 ≈ block 对整模的贡献。
整模 latency = floor(非 block) + Σ block，其中 floor = baseline_whole − Σ identity_block
（mip_select 算）。

U6 改造：
  - root cause A/K：parent 激活捕获 + floor latency 整模 forward 走
    ``adapters.forward_model(model, batch)``（不再 ``model(single_tensor)``）。
  - root cause E：主循环 + floor 循环都过 ``is_candidate_valid_for_slot``；
    非方 slot 的 floor 用「原块实测 latency」兜底，**禁** ``make_zero`` 对非方 slot raise。
    具体地：floor 循环遍历 slot，valid-for-zero（in_dim==out_dim）的 slot 替 ``make_zero``；
    非方 slot 保留原块（其 latency 计入 floor 实测，反映真实非 block overhead）。

避免「整模 replace-1-block」测量的非加性（单块 no_op 在上下文里只显省 0.03ms，但全 drop
省 0.6ms——dispatch/缓存交互使 per-swap savings 不可加）。单块隔离测量去掉上下文耦合，
加性成立。

- passthrough(identity)：测原父块单块 latency。
- latency_script_path → ONNX 单文件契约（导出单块 → fn(onnx_path)）；否则 PyTorch median ms（100 reps 稳定）。

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
    build_pretrained_model,
    capture_parent_activations,
    get_candidate,
    is_candidate_valid_for_slot,
    is_passthrough,
    load_puzzle_adapters,
    load_variant_state_dict,
    measure_module_latency_via_onnx_script,
    measure_whole_model_latency,
    replace_slot,
)


def _measure_block_latency(
    variant_module: nn.Module,
    sample_input: torch.Tensor,
    device: torch.device,
    latency_script_path: str,
) -> float:
    """测单块 latency（standalone）。

    单块 latency 的输入是 slot 抓到的 main-path 张量（非 native batch），所以这里
    仍走 ``module(sample_input)``（block 自身契约：单 tensor 主路径）。

    ``latency_script_path`` 提供 → **ONNX 单文件契约**（SPEC P2.5）：把单块导出为单文件
    ONNX → 调 ``fn(onnx_path)`` 得 float（用户脚本唯一权威，非 ``fn(model, batch)``）。
    """
    if latency_script_path:
        return measure_module_latency_via_onnx_script(
            variant_module, (sample_input,), {}, device, latency_script_path
        )
    import time
    variant_module.eval().to(device)
    with torch.no_grad():
        for _ in range(30):
            variant_module(sample_input)
        times: list[float] = []
        for _ in range(100):
            t0 = time.perf_counter()
            variant_module(sample_input)
            times.append(time.perf_counter() - t0)
    times.sort()
    return times[len(times) // 2] * 1000.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Puzzle U6 latency 表（单块 standalone）")
    parser.add_argument("--block_map", required=True)
    parser.add_argument("--flat_model", required=True, help="flat model .py（架构源）")
    parser.add_argument("--build_fn", required=True)
    parser.add_argument("--build_cfg", default="")
    parser.add_argument("--block_library", required=True)
    parser.add_argument(
        "--adapters", required=True,
        help="puzzle_adapters.py 路径（U6 §2.1：脚本唯一项目接口）",
    )
    parser.add_argument(
        "--manifest", default="",
        help="manifest.yaml 路径（metadata 用；脚本不解析）",
    )
    parser.add_argument("--latency_unit", default="ms", choices=["ms", "us", "s"],
                        help="latency 单位（仅标注，不换算数值）")
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
        adapters = load_puzzle_adapters(args.adapters)
        block_map = BlockMap.from_json(args.block_map)
        if not block_map.slots:
            raise ValueError("block_map 无 slot")

        # U6：teacher 走 adapters；不再 load_father_model
        model = build_pretrained_model(adapters)
        device = torch.device("cpu")
        model.eval().to(device)

        # U6 root cause A/K：calib 走 adapters.calib_iter，forward 走 adapters.forward_model
        calib_iter = adapters.calib_iter(device=device)
        activations = capture_parent_activations(
            model, block_map, calib_iter, adapters.forward_model, device
        )

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

                    # U6 root cause E：主循环也过 is_candidate_valid_for_slot——
                    # 结构不匹配的 variant（如 mask-blind × mask slot）跳过不打分。
                    if not is_candidate_valid_for_slot(variant, slot):
                        continue

                    if is_passthrough(variant):
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
            "assessment": f"latency 表完成（单块 standalone, unit={args.latency_unit}）",
            "max_retries_hit": False,
            "healed_files": [],
            "fidelity_retriggered": False,
        }

        # ── floor：全 slot 退化为 floor 块的整模 latency（§6.7 kind-specific）────────
        # U6 root cause E：floor 循环对 block 粒度（attention/ffn/...）过 is_candidate_valid_for_slot
        # （no_op 仍注册在 catalog）；非方 slot 用「原块实测 latency」兜底——禁 make_zero 对非方
        # slot raise（保留原块，其 latency 计入 floor）。
        # §6.7 floor 语义 kind-specific：transformer_layer → passthrough（return x，保 residual
        # stream）；其他 kind → zero（block 在 residual 内，x+0=x 合法）。
        # F1：no_op_layer 已退出 catalog（不作 MIP 候选）。layer 粒度 floor **不经 catalog 校验**——
        # make_no_op_layer 经直接 import（transformer_layer_variants），仅校验 in_dim==out_dim
        # （make_no_op_layer 构造要求；非方 layer slot 保留原块）。
        from puzzle_blocks import make_zero
        from transformer_layer_variants import make_no_op_layer
        floor_replaced_zero: list[str] = []
        floor_kept_original: list[str] = []
        for slot in block_map.slots:
            if slot.kind == "transformer_layer":
                # §6.7 layer floor：make_no_op_layer 直接 import（不经 catalog——no_op_layer
                # 已退出候选集）。仅校验 in_dim==out_dim（构造要求 + 残差直通合法）。
                if slot.in_dim != slot.out_dim:
                    floor_kept_original.append(slot.parent_module_path)
                    continue
                # layer residual unit：passthrough（return x）保 residual stream，非 zero
                floor_mod = make_no_op_layer(slot).to(device).eval()
            else:
                # block 粒度 floor：no_op 仍注册在 catalog（block zero 合法作 MIP 候选）。
                if not is_candidate_valid_for_slot("no_op", slot) or slot.in_dim != slot.out_dim:
                    # 非方 slot / 结构不匹配：保留原块（其 latency 计入 floor）
                    floor_kept_original.append(slot.parent_module_path)
                    continue
                # block 在 residual 内：zero（x + 0 = x）合法
                floor_mod = make_zero(slot).to(device).eval()
            replace_slot(model, slot.parent_module_path, floor_mod)
            floor_replaced_zero.append(slot.parent_module_path)

        # floor 整模 forward 走 adapters.forward_model（U6：不再 model(dummy_input)）
        calib_iter2 = adapters.calib_iter(device=device)
        try:
            floor_batch = next(iter(calib_iter2))
        except StopIteration as e:
            raise RuntimeError(
                "adapters.calib_iter() 返回空——latency_table floor 无 batch"
            ) from e
        floor_latency = measure_whole_model_latency(
            model, adapters.forward_model, floor_batch, device, args.latency_script_path,
            convention=adapters.FORWARD_CALLING_CONVENTION,
        )
        floor_path = output_dir / "latency_floor.json"
        with open(floor_path, "w", encoding="utf-8") as ff:
            json.dump({
                "floor_latency": floor_latency,
                "unit": args.latency_unit,
                "replaced_zero_slots": floor_replaced_zero,
                "kept_original_slots": floor_kept_original,
            }, ff, ensure_ascii=False, indent=2)
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
