"""score.py —— Puzzle P2.4：replace-1-block 打分。

对每 (layer, slot, variant)：把该 slot 替换成 variant（载块库权重），其余冻结
父模型，calibration set 上算 block-distance 分：
  - classification → ``logits_kd_loss``（KL）vs 父 logits
  - embedding → hidden cosine distance ``1 - cos(h_var, h_parent)``
  - regression → output MSE
score = -distance（越大越好）。

输出 ``scores.jsonl``：``{layer, slot, variant, score, valid}``。
stdout：``SCORES: <path>`` / ``RESULT_JSON: {...}``。
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
    get_candidate,
    get_module_dummy_input,
    is_candidate_valid_for_slot,
    is_passthrough,
    load_father_model,
    load_variant_state_dict,
    replace_slot,
)


def _cosine_distance(a: torch.Tensor, b: torch.Tensor) -> float:
    """hidden cosine distance = 1 - cos(a, b)，flatten 后均值。"""
    a_f = a.reshape(-1, a.shape[-1]).float()
    b_f = b.reshape(-1, b.shape[-1]).float()
    cos = F.cosine_similarity(a_f, b_f, dim=-1)
    return float((1.0 - cos).mean().item())


def _score_variant(
    model: nn.Module,
    slot: Slot,
    variant: str,
    ckpt_path: Path,
    calib_inputs: list[torch.Tensor],
    parent_outputs: list[torch.Tensor],
    eval_kind: str,
    device: torch.device,
) -> tuple[float, bool]:
    """replace-1-block 打分；返回 (score, valid)。invalid variant → valid=False。

    - passthrough（identity）：不替换 slot，score = -0 = 0（parent 自比距离为 0）。
    - 其他 variant：载块库权重替换后 forward，算 distance = score 负值。
    """
    if is_passthrough(variant):
        # identity = 保留父块 → 输出与 parent_outputs 完全一致 → distance=0
        return 0.0, True

    # E6/E8：结构不匹配的 variant（如 bypass FFN 选 ffn_75、mask slot 选 mask-blind）
    # 标 valid=False 不打分——下游 mip_select 据 valid 字段过滤。
    if not is_candidate_valid_for_slot(variant, slot):
        return 0.0, False

    entry = get_candidate(variant)
    if slot.kind not in entry.kinds:
        return 0.0, False

    # 载入 variant 权重
    if not ckpt_path.is_file():
        raise FileNotFoundError(f"variant ckpt 不存在：{ckpt_path}")

    variant_module = entry.factory(slot).to(device).eval()
    ckpt = torch.load(ckpt_path, map_location=device, weights_only=False)
    sd = ckpt.get("state_dict", ckpt) if isinstance(ckpt, dict) else ckpt
    load_variant_state_dict(variant_module, sd, variant, strict_unexpected=True)

    original = replace_slot(model, slot.parent_module_path, variant_module)
    try:
        model.eval()
        total_dist = 0.0
        n = 0
        with torch.no_grad():
            for inp, parent_out in zip(calib_inputs, parent_outputs):
                inp = inp.to(device)
                parent_out = parent_out.to(device)
                try:
                    var_out = model(inp)
                except Exception as e:
                    raise RuntimeError(
                        f"variant {variant!r} forward 失败：{e}"
                    ) from e
                if isinstance(var_out, tuple):
                    var_out = var_out[0]
                if eval_kind == "classification":
                    from nas_agent.train.distillation import logits_kd_loss
                    d = float(logits_kd_loss(var_out, parent_out).item())
                elif eval_kind == "embedding":
                    d = _cosine_distance(var_out, parent_out)
                else:  # regression
                    d = float(F.mse_loss(var_out, parent_out).item())
                total_dist += d
                n += 1
        score = -(total_dist / max(1, n))  # score = -distance（越大越好）
        return score, True
    finally:
        # 还原（不变量：parent 模型不可被 variant 打分污染）
        replace_slot(model, slot.parent_module_path, original)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Puzzle P2.4 replace-1-block 打分")
    parser.add_argument("--block_map", required=True)
    parser.add_argument("--flat_model", required=True)
    parser.add_argument("--build_fn", required=True)
    parser.add_argument("--build_cfg", default="")
    parser.add_argument("--block_library", required=True, help="block_library 目录")
    parser.add_argument("--eval_fn", required=True)
    parser.add_argument(
        "--eval_kind",
        required=True,
        choices=["classification", "embedding", "regression"],
    )
    parser.add_argument("--output_dir", required=True)
    parser.add_argument(
        "--father_state",
        default="",
        help="预训练父模型权重 .pt 路径（expand 保存的 father_state_dict.pt）。"
        "replace-1-block 的冻结全模型 = father,必须预训练——空串回退随机 init"
        "（仅 dry-run 兼容）",
    )
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    torch.manual_seed(args.seed)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    scores_path = output_dir / "scores.jsonl"
    block_library_dir = Path(args.block_library).resolve()

    try:
        block_map = BlockMap.from_json(args.block_map)
        if not block_map.slots:
            raise ValueError("block_map 无 slot")

        # replace-1-block 的冻结全模型 = father,必须预训练
        model = load_father_model(
            args.flat_model, args.build_fn, args.build_cfg, args.father_state
        )
        device = torch.device("cpu")
        model.eval().to(device)
        for p in model.parameters():
            p.requires_grad_(False)

        dummy_meta = get_module_dummy_input(args.flat_model)
        calib_loader = build_calib_loader(model, dummy_meta, batch_size=2, device=device)
        calib_inputs: list[torch.Tensor] = []
        parent_outputs: list[torch.Tensor] = []
        with torch.no_grad():
            for batch in calib_loader:
                inp = batch[0] if isinstance(batch, (list, tuple)) else batch
                inp = inp.to(device)
                calib_inputs.append(inp)
                out = model(inp)
                if isinstance(out, tuple):
                    out = out[0]
                parent_outputs.append(out.detach())

        # score 接 --block_library 作真相，候选集 = 目录里所有 ckpt 对应 variant
        n_valid = 0
        with open(scores_path, "w", encoding="utf-8") as fout:
            for slot in block_map.slots:
                # 找该 slot 的所有 variant ckpt
                prefix = f"L{slot.layer_idx}_{slot.kind}_"
                ckpt_files = sorted(block_library_dir.glob(f"{prefix}*.pt"))
                if not ckpt_files:
                    raise FileNotFoundError(
                        f"slot {slot.parent_module_path} 在 block_library 找不到 "
                        f"ckpts（prefix={prefix}）"
                    )
                for ckpt_path in ckpt_files:
                    # variant 名 = 文件名去前缀和 .pt
                    variant = ckpt_path.stem[len(prefix):]
                    score, valid = _score_variant(
                        model=model,
                        slot=slot,
                        variant=variant,
                        ckpt_path=ckpt_path,
                        calib_inputs=calib_inputs,
                        parent_outputs=parent_outputs,
                        eval_kind=args.eval_kind,
                        device=device,
                    )
                    row = {
                        "layer": slot.layer_idx,
                        "kind": slot.kind,
                        "variant": variant,
                        "score": score,
                        "valid": valid,
                    }
                    fout.write(json.dumps(row, ensure_ascii=False) + "\n")
                    fout.flush()
                    n_valid += int(valid)

        result = {
            "status": "executed",
            "artifacts": [str(scores_path)],
            "assessment": (
                f"打分完成：{n_valid} 个 valid (layer,slot,variant)"
            ),
            "max_retries_hit": False,
            "healed_files": [],
            "fidelity_retriggered": False,
        }
        print(f"SCORES: {scores_path}")
        print(f"RESULT_JSON: {json.dumps(result, ensure_ascii=False)}")
        return 0
    except Exception as e:
        tb = traceback.format_exc()
        print(f"ERROR: score 失败 — {type(e).__name__}: {e}\n{tb}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
