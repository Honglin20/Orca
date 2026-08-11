"""bld.py —— Puzzle P2.3：Blockwise Local Distillation。

对每 (layer_idx, slot, variant)：实例化候选块（维度对齐 in_dim/out_dim）→
冻结 parent 为 teacher → normalized MSE ``MSE(o_p, o_c) / MSE(o_p, 0)`` 蒸馏
到收敛 → save ``block_library/L<layer>_<slot>_<variant>.pt``。

- identity / no_op 候选不训练（直接保存空 state_dict）。
- per-variant 写 ``runs/bld/progress.jsonl``：``{"step":N,"metrics":{...}}``。
- 写 ``bld_summary.json``（每 variant 最终 BLD loss）。
- stdout：``BLD_COMPLETE: <summary_path>`` / ``RESULT_JSON: {...}``。

通用：block_map + flat_model 参数化，不硬编码任何模型。fail loud。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F

from puzzle_common import (
    BlockMap,
    Slot,
    build_calib_loader,
    candidate_registry,
    capture_parent_activations,
    get_module_dummy_input,
    is_passthrough,
    load_father_model,
    parse_block_candidates,
    variant_file_name,
)


def _normalized_mse(o_p: torch.Tensor, o_c: torch.Tensor) -> torch.Tensor:
    """normalized MSE = MSE(o_p, o_c) / MSE(o_p, 0)。

    分母为 teacher 输出的能量；防 teacher 输出近零（数值稳定加 eps）。
    """
    num = F.mse_loss(o_c, o_p)
    den = F.mse_loss(o_p, torch.zeros_like(o_p)) + 1e-8
    return num / den


def _train_one_variant(
    slot: Slot,
    variant: str,
    parent_in: torch.Tensor,
    parent_out: torch.Tensor,
    device: torch.device,
    epochs: int,
    lr: float,
    progress_path: Path,
    global_step: int,
) -> tuple[float, nn.Module]:
    """单 variant 的 BLD：min normalized MSE(student(in), teacher_out)。

    返回 ``(final_loss, trained_block_module)``。identity/no_op 不训练
    （由 caller 短路，不会进到这里）。
    """
    factory, applicable = candidate_registry[variant]
    if slot.slot_type not in applicable:
        raise ValueError(
            f"variant {variant!r} 不适用 slot_type={slot.slot_type}"
        )
    block_module = factory(slot).to(device).train()

    in_t = parent_in.to(device)
    target = parent_out.to(device)
    # 确认候选块 forward 能产出 target 同 shape（fail loud）
    with torch.no_grad():
        try:
            probe = block_module(in_t)
        except Exception as e:
            raise RuntimeError(
                f"variant {variant!r} 在 slot {slot.parent_module_path} "
                f"forward 失败：{e}"
            ) from e
    if probe.shape != target.shape:
        raise RuntimeError(
            f"variant {variant!r} 输出 shape {tuple(probe.shape)} 与 teacher "
            f"{tuple(target.shape)} 不匹配（slot {slot.parent_module_path}）"
        )

    # 参数零的 variant（如 fnet 的 DFT mixer，basis 确定性）→ 无可训参数，
    # BLD 无意义：只算一次 loss 入 summary，不优化。
    n_params = sum(p.numel() for p in block_module.parameters())
    if n_params == 0:
        with torch.no_grad():
            final = float(_normalized_mse(target, block_module(in_t)).item())
        block_module.eval()
        return final, block_module

    opt = torch.optim.Adam(block_module.parameters(), lr=lr)
    last_loss = 0.0
    step = global_step
    with open(progress_path, "a", encoding="utf-8") as flog:
        for ep in range(epochs):
            opt.zero_grad(set_to_none=True)
            out = block_module(in_t)
            loss = _normalized_mse(target, out)
            loss.backward()
            opt.step()
            last_loss = float(loss.detach().cpu().item())
            step += 1
            flog.write(
                json.dumps(
                    {
                        "step": step,
                        "metrics": {
                            "loss": last_loss,
                            "layer": slot.layer_idx,
                            "slot": slot.slot_type,
                            "variant": variant,
                        },
                    }
                )
                + "\n"
            )
            flog.flush()
    block_module.eval()
    return last_loss, block_module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Puzzle P2.3 BLD 块级蒸馏")
    parser.add_argument("--block_map", required=True, help="block_map.json 路径")
    parser.add_argument("--flat_model", required=True, help="flat model .py 路径")
    parser.add_argument("--build_fn", required=True)
    parser.add_argument("--build_cfg", default="")
    parser.add_argument("--output_dir", required=True)
    parser.add_argument(
        "--block_candidates",
        default="",
        help="候选块 JSON；空 → 默认集",
    )
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument(
        "--father_state",
        default="",
        help="预训练父模型权重 .pt 路径（expand 保存的 father_state_dict.pt）。"
        "Puzzle 的 teacher 必须预训练——空串回退随机 init（仅 dry-run 兼容）",
    )
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    torch.manual_seed(args.seed)
    output_dir = Path(args.output_dir).resolve()
    block_library_dir = output_dir / "block_library"
    block_library_dir.mkdir(parents=True, exist_ok=True)
    runs_dir = output_dir / "runs" / "bld"
    runs_dir.mkdir(parents=True, exist_ok=True)
    progress_path = runs_dir / "progress.jsonl"

    try:
        block_map = BlockMap.from_json(args.block_map)
        if not block_map.slots:
            raise ValueError("block_map 无 slot")

        # father/teacher 必须预训练：load_father_model 注入 expand 保存的预训练权重
        model = load_father_model(
            args.flat_model, args.build_fn, args.build_cfg, args.father_state
        )
        device = torch.device("cpu")
        model.eval().to(device)
        for p in model.parameters():
            p.requires_grad_(False)

        dummy_meta = get_module_dummy_input(args.flat_model)
        calib_loader = build_calib_loader(model, dummy_meta, batch_size=2, device=device)

        # 捕获 parent activations
        activations = capture_parent_activations(model, block_map, calib_loader, device)

        candidates = parse_block_candidates(args.block_candidates)
        summary: dict[str, Any] = {"variants": []}
        global_step = 0
        saved_ckpts: list[str] = []

        for slot in block_map.slots:
            variant_list = candidates.get(slot.slot_type, [])
            if not variant_list:
                raise ValueError(
                    f"slot_type={slot.slot_type} 无候选变体（block_candidates 配置缺）"
                )
            parent_in, parent_out = activations[slot.parent_module_path]
            for variant in variant_list:
                fname = variant_file_name(slot.layer_idx, slot.slot_type, variant)
                ckpt_path = block_library_dir / fname
                if is_passthrough(variant):
                    # identity = 保留父块（passthrough），不训不存权重；
                    # 写一个 sentinel ckpt 让 score/latency/build 统一识别。
                    torch.save(
                        {
                            "state_dict": {},
                            "variant": variant,
                            "passthrough": True,
                            "layer": slot.layer_idx,
                            "slot": slot.slot_type,
                        },
                        ckpt_path,
                    )
                    summary["variants"].append(
                        {
                            "layer": slot.layer_idx,
                            "slot": slot.slot_type,
                            "variant": variant,
                            "final_loss": 0.0,
                            "trained": False,
                            "passthrough": True,
                        }
                    )
                    saved_ckpts.append(str(ckpt_path))
                    continue
                final_loss, trained_block = _train_one_variant(
                    slot=slot,
                    variant=variant,
                    parent_in=parent_in,
                    parent_out=parent_out,
                    device=device,
                    epochs=args.epochs,
                    lr=args.lr,
                    progress_path=progress_path,
                    global_step=global_step,
                )
                torch.save(
                    {
                        "state_dict": _state_dict_clean(trained_block),
                        "variant": variant,
                        "layer": slot.layer_idx,
                        "slot": slot.slot_type,
                        "slot_in_dim": slot.in_dim,
                        "slot_out_dim": slot.out_dim,
                    },
                    ckpt_path,
                )
                summary["variants"].append(
                    {
                        "layer": slot.layer_idx,
                        "slot": slot.slot_type,
                        "variant": variant,
                        "final_loss": final_loss,
                        "trained": True,
                    }
                )
                saved_ckpts.append(str(ckpt_path))
                global_step += args.epochs

        summary_path = output_dir / "bld_summary.json"
        summary["n_variants"] = len(summary["variants"])
        summary["block_library_dir"] = str(block_library_dir)
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        # Write completion marker (status.sh / emit_result.py 契约路径).
        # BLD produces a block library, not a single model — this marker carries
        # summary metadata so downstream runners can validate via torch.load.
        runs_dir = output_dir / "runs" / "bld"
        runs_dir.mkdir(parents=True, exist_ok=True)
        torch.save(
            {"state_dict": {"bld_complete": torch.tensor([1.0])}, "summary": summary},
            runs_dir / "bld_complete.pt",
        )

        result = {
            "status": "executed",
            "artifacts": [str(block_library_dir), str(summary_path)],
            "assessment": (
                f"BLD 完成：{len(summary['variants'])} 个 variant，"
                f"avg_final_loss="
                f"{sum(v['final_loss'] for v in summary['variants']) / max(1, len(summary['variants'])):.4f}"
            ),
            "max_retries_hit": False,
            "healed_files": [],
            "fidelity_retriggered": False,
        }
        print(f"BLD_COMPLETE: {summary_path}")
        print(f"RESULT_JSON: {json.dumps(result, ensure_ascii=False)}")
        return 0
    except Exception as e:
        tb = traceback.format_exc()
        print(f"ERROR: bld 失败 — {type(e).__name__}: {e}\n{tb}", file=sys.stderr)
        return 2


def _state_dict_clean(module: nn.Module) -> dict[str, torch.Tensor]:
    """干净地取 state_dict（过滤 NonTensor）。"""
    sd = module.state_dict()
    return {k: v.cpu() for k, v in sd.items()}


if __name__ == "__main__":
    sys.exit(main())
