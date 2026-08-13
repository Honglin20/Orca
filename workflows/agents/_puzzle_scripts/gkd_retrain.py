"""gkd_retrain.py —— Puzzle 末段全局 KD 重训（optimized_flat 执行基底）。

materialize 改造（docs/plans/2026-08-13-puzzle-materialize-optimized-flat.md）：
  - student 构造**严格走 optimized_flat**：``load_optimized_flat(path).build_model()``，
    再 strict 载 selected_model.pt（= 父⊕BLD 合成权重）——不再 ``build_student_from_arch``
    运行时重建。optimized_flat 是 GKD/gate/交付的唯一执行基底（正确性由构造保证）。
  - KD loss / task loss / forward / 数据 全走 ``adapters``（U6 root cause D 不变）。

读 optimized_flat + selected_model + adapters（teacher，冻结）→ 端到端 KD → final_model.pt。
final_model.pt 与 optimized_flat.build_model() 同结构（strict 可载）——交付即此文件 + 权重。

  - 写 ``runs/retrain/progress.jsonl``：``{"step":N,"metrics":{...}}``
  - 输出 ``runs/retrain/final_model.pt``（= retrain_best.pth 契约路径）

stdout：``GKD_COMPLETE: <path>`` / ``RESULT_JSON: {...}``。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

import torch

from puzzle_common import (
    build_pretrained_model,
    load_optimized_flat,
    load_puzzle_adapters,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Puzzle GKD 末段重训（optimized_flat 基底）")
    parser.add_argument("--selected_model", required=True, help="selected_model.pt 路径（父⊕BLD）")
    parser.add_argument(
        "--optimized_flat", required=True,
        help="<base>_optimized_flat.py（pz_materialize 产出；student 执行基底）",
    )
    parser.add_argument(
        "--adapters", required=True,
        help="puzzle_adapters.py 路径（U6 §2.1：脚本唯一项目接口）",
    )
    parser.add_argument(
        "--manifest", default="",
        help="manifest.yaml 路径（metadata 用；脚本不解析）",
    )
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument(
        "--task_loss_weight", type=float, default=1.0,
        help="硬标签监督权重（与 KD 并列；0 = 纯 KD；adapters.task_loss 返 None 时无影响）",
    )
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    torch.manual_seed(args.seed)
    output_dir = Path(args.output_dir).resolve()
    runs_dir = output_dir / "runs" / "retrain"
    runs_dir.mkdir(parents=True, exist_ok=True)
    progress_path = runs_dir / "progress.jsonl"
    final_model_path = runs_dir / "final_model.pt"

    try:
        adapters = load_puzzle_adapters(args.adapters)
        selected_ckpt = torch.load(
            args.selected_model, map_location="cpu", weights_only=False
        )
        if not isinstance(selected_ckpt, dict) or not selected_ckpt.get("selected_arch"):
            raise ValueError(f"{args.selected_model} 缺 selected_arch 字段")
        selected_arch = selected_ckpt["selected_arch"]
        selected_state_dict = selected_ckpt.get("state_dict", {})
        if not selected_state_dict:
            raise RuntimeError(
                f"{args.selected_model} state_dict 为空——无从 GKD（缺合成权重）"
            )

        device = torch.device("cpu")

        # teacher = father（预训练，冻结）—— adapters.build_model + load_pretrained
        teacher = build_pretrained_model(adapters)
        teacher.eval().to(device)
        for p in teacher.parameters():
            p.requires_grad_(False)

        # student = optimized_flat.build_model()（唯一执行基底）+ strict 载 selected_model.pt。
        # key 对齐已由 pz_materialize 自检保证（optimized_flat vs build_student_from_arch）。
        opt_flat = load_optimized_flat(args.optimized_flat)
        student = opt_flat.build_model()
        student.load_state_dict(selected_state_dict, strict=True)
        student.to(device).train()

        # KD loss + 硬标签监督全走适配器（U6 root cause D 不变）。
        kd_loss_fn = adapters.kd_loss
        task_loss_fn = adapters.task_loss
        forward_fn = adapters.forward_model
        extract_labels_fn = adapters.extract_labels

        opt = torch.optim.Adam(
            (p for p in student.parameters() if p.requires_grad), lr=args.lr
        )

        step = 0
        with open(progress_path, "a", encoding="utf-8") as flog:
            for ep in range(args.epochs):
                for batch in adapters.train_iter(device=device):
                    with torch.no_grad():
                        t_out = forward_fn(teacher, batch)
                    s_out = forward_fn(student, batch)
                    labels = extract_labels_fn(batch)
                    loss_kd = kd_loss_fn(s_out, t_out, labels=labels)
                    loss = loss_kd
                    loss_task_val = 0.0
                    # 硬标签监督（适配器自决适用性；task_loss 返 None 则跳过）
                    if labels is not None and args.task_loss_weight > 0:
                        task_loss = task_loss_fn(s_out, labels)
                        if task_loss is not None:
                            loss = loss + args.task_loss_weight * task_loss
                            loss_task_val = float(task_loss.item())
                    opt.zero_grad(set_to_none=True)
                    loss.backward()
                    opt.step()
                    step += 1
                    metrics = {
                        "loss": float(loss.item()),
                        "kd": float(loss_kd.item()),
                    }
                    if loss_task_val > 0:
                        metrics["task"] = loss_task_val
                    flog.write(
                        json.dumps({"step": step, "metrics": metrics}) + "\n"
                    )
                    flog.flush()

        torch.save(
            {
                "state_dict": {k: v.cpu() for k, v in student.state_dict().items()},
                "selected_arch": selected_arch,
                "optimized_flat": str(Path(args.optimized_flat).resolve().name),
                "epochs": args.epochs,
                "final_step": step,
            },
            final_model_path,
        )

        result = {
            "status": "executed",
            "artifacts": [str(final_model_path)],
            "assessment": f"GKD 完成：{args.epochs} epoch / {step} step（optimized_flat 基底）",
            "max_retries_hit": False,
            "healed_files": [],
            "fidelity_retriggered": False,
        }
        print(f"GKD_COMPLETE: {final_model_path}")
        print(f"RESULT_JSON: {json.dumps(result, ensure_ascii=False)}")
        return 0
    except Exception as e:
        tb = traceback.format_exc()
        print(f"ERROR: gkd_retrain 失败 — {type(e).__name__}: {e}\n{tb}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
