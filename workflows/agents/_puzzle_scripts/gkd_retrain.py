"""gkd_retrain.py —— Puzzle 末段全局 KD 重训（U6 适配器架构）。

U6 改造（root cause A/K/D）：
  - 删 ``_flatten_model_output`` / ``is_classification`` / 写死 ``cross_entropy`` /
    ``logits_kd_loss`` 分支：KD loss 走 ``adapters.kd_loss(s_out, t_out, labels)``，
    硬标签监督走 ``adapters.task_loss(s_out, labels)``（None 则无）。
  - forward 走 ``adapters.forward_model(model, batch)``（不再假设单 tensor）。
  - 训练数据走 ``adapters.train_iter()``；labels 走 ``adapters.extract_labels(batch)``。

读 selected_model + flat/adapters（teacher，冻结）：
  - 端到端 KD：``adapters.kd_loss``（agent 按任务移植正确 KD：cosine/KL/MSE/任务 loss）。
  - 可选硬标签监督：``adapters.task_loss``（agent 移植用户任务 loss；非监督返 None）。

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
    BlockMap,
    build_pretrained_model,
    build_student_from_arch,
    load_puzzle_adapters,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Puzzle U6 GKD 末段重训")
    parser.add_argument("--selected_model", required=True, help="selected_model.pt 路径")
    parser.add_argument("--flat_model", required=True, help="flat_model.py（架构源）")
    parser.add_argument("--build_fn", required=True)
    parser.add_argument("--build_cfg", default="")
    parser.add_argument("--block_map", required=True)
    parser.add_argument("--block_library", required=True)
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

        block_map = BlockMap.from_json(args.block_map)
        device = torch.device("cpu")

        # teacher = father（预训练，冻结）—— U6：adapters.build_model + load_pretrained
        teacher = build_pretrained_model(adapters)
        teacher.eval().to(device)
        for p in teacher.parameters():
            p.requires_grad_(False)

        # student = 共享 helper 重建异构架构（U6：经 adapters 注入 father 权重）。
        student = build_student_from_arch(
            adapters=adapters,
            block_map=block_map,
            selected_arch=selected_ckpt,
            block_library_dir=Path(args.block_library).resolve(),
            device=device,
            flat_model_path=args.flat_model,
            build_fn=args.build_fn,
            build_cfg=args.build_cfg,
        )
        # 载 selected_model.pt 的整体 state_dict（含未被替换模块的父权重）
        if selected_state_dict:
            missing, unexpected = student.load_state_dict(
                selected_state_dict, strict=False
            )
            if unexpected:
                raise RuntimeError(
                    f"selected_model state_dict 有 {len(unexpected)} 个 unexpected "
                    f"key（schema 不一致）：{list(unexpected)[:5]}"
                )
            if missing:
                print(
                    f"WARN: load_state_dict missing {len(missing)} keys "
                    f"(variant factory init): {list(missing)[:5]}",
                    file=sys.stderr,
                )
        student.to(device).train()

        # U6 root cause D：KD loss + 硬标签监督全走适配器（删 is_classification / 写死 CE）。
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
                "epochs": args.epochs,
                "final_step": step,
            },
            final_model_path,
        )

        result = {
            "status": "executed",
            "artifacts": [str(final_model_path)],
            "assessment": f"GKD 完成：{args.epochs} epoch / {step} step",
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
