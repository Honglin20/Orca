"""score.py —— Puzzle replace-1-block 打分（U6 适配器架构）。

对每 (layer, kind, variant)：把该 slot 替换成 variant（载块库权重），其余冻结
父模型，calibration set 上算 block-distance 分。U6 改造（root cause A/K/D）：
  - forward 走 ``adapters.forward_model(model, batch)``（不再假设单 tensor）。
  - distance 统一走 ``adapters.kd_loss(s_out, t_out)`` —— agent 据任务移植正确 KD
    （KL/cosine/MSE/任务 loss），脚本删 ``eval_kind`` 分支与硬编码 logits_kd_loss。
  - calib 数据走 ``adapters.calib_iter()``。
  - score = -distance（越大越好）。

输出 ``scores.jsonl``：``{layer, kind, variant, score, valid}``。
stdout：``SCORES: <path>`` / ``RESULT_JSON: {...}``。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

import torch

from puzzle_common import (
    BlockMap,
    Slot,
    build_pretrained_model,
    get_candidate,
    is_candidate_valid_for_slot,
    is_passthrough,
    load_puzzle_adapters,
    load_variant_state_dict,
    replace_slot,
)


def _score_variant(
    model: nn.Module,
    slot: Slot,
    variant: str,
    ckpt_path: Path,
    calib_batches: list,
    parent_outputs: list[torch.Tensor],
    forward_fn,
    kd_loss_fn,
    device: torch.device,
) -> tuple[float, bool]:
    """replace-1-block 打分；返回 (score, valid)。invalid variant → valid=False。

    - passthrough（identity）：不替换 slot，score = 0（parent 自比 distance 为 0）。
    - 其他 variant：载块库权重替换后 forward，distance = ``adapters.kd_loss(s_out, t_out)``。
    """
    if is_passthrough(variant):
        return 0.0, True

    if not is_candidate_valid_for_slot(variant, slot):
        return 0.0, False

    entry = get_candidate(variant)
    if slot.kind not in entry.kinds:
        return 0.0, False

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
            for batch, parent_out in zip(calib_batches, parent_outputs):
                try:
                    var_out = forward_fn(model, batch)
                except Exception as e:
                    raise RuntimeError(
                        f"variant {variant!r} forward 失败：{e}"
                    ) from e
                # kd_loss 处理 model 输出形态（tuple/dict/单 tensor）——agent 移植时消化
                d = float(kd_loss_fn(var_out, parent_out).item())
                total_dist += d
                n += 1
        score = -(total_dist / max(1, n))  # score = -distance（越大越好）
        return score, True
    finally:
        replace_slot(model, slot.parent_module_path, original)


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Puzzle U6 replace-1-block 打分")
    p.add_argument("--block_map", required=True)
    p.add_argument("--flat_model", required=True, help="flat model .py 路径（架构源）")
    p.add_argument("--build_fn", required=True)
    p.add_argument("--build_cfg", default="")
    p.add_argument("--block_library", required=True, help="block_library 目录")
    p.add_argument(
        "--adapters", required=True,
        help="puzzle_adapters.py 路径（U6 §2.1：脚本唯一项目接口）",
    )
    p.add_argument(
        "--manifest", default="",
        help="manifest.yaml 路径（metadata 用；脚本不解析）",
    )
    p.add_argument("--output_dir", required=True)
    p.add_argument("--seed", type=int, default=0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    torch.manual_seed(args.seed)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    scores_path = output_dir / "scores.jsonl"
    block_library_dir = Path(args.block_library).resolve()

    try:
        adapters = load_puzzle_adapters(args.adapters)
        block_map = BlockMap.from_json(args.block_map)
        if not block_map.slots:
            raise ValueError("block_map 无 slot")

        # replace-1-block 的冻结全模型 = father（预训练）
        model = build_pretrained_model(adapters)
        device = torch.device("cpu")
        model.eval().to(device)
        for p in model.parameters():
            p.requires_grad_(False)

        # U6 root cause A/K：calib 走 adapters.calib_iter（不再假设单 tensor batch）
        calib_batches: list = []
        parent_outputs: list[torch.Tensor] = []
        with torch.no_grad():
            for batch in adapters.calib_iter(device=device):
                calib_batches.append(batch)
                out = adapters.forward_model(model, batch)
                # kd_loss 期望与 student 输出同形态；parent_out 用 forward_model 原始返回
                # （若返回 tuple/list，kd_loss 内部由 agent 移植处理）
                if isinstance(out, (tuple, list)):
                    out = out[0] if out else out
                parent_outputs.append(out.detach())
                if len(calib_batches) >= 2:
                    break  # 两个 batch 够打分
        if not calib_batches:
            raise RuntimeError(
                "adapters.calib_iter() 返回空——score 无 calib 数据（检 manifest 数据入口）"
            )

        n_valid = 0
        with open(scores_path, "w", encoding="utf-8") as fout:
            for slot in block_map.slots:
                prefix = f"L{slot.layer_idx}_{slot.kind}_"
                ckpt_files = sorted(block_library_dir.glob(f"{prefix}*.pt"))
                if not ckpt_files:
                    raise FileNotFoundError(
                        f"slot {slot.parent_module_path} 在 block_library 找不到 "
                        f"ckpts（prefix={prefix}）"
                    )
                for ckpt_path in ckpt_files:
                    variant = ckpt_path.stem[len(prefix):]
                    score, valid = _score_variant(
                        model=model,
                        slot=slot,
                        variant=variant,
                        ckpt_path=ckpt_path,
                        calib_batches=calib_batches,
                        parent_outputs=parent_outputs,
                        forward_fn=adapters.forward_model,
                        kd_loss_fn=adapters.kd_loss,
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
            "assessment": f"打分完成：{n_valid} 个 valid (layer,kind,variant)",
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
