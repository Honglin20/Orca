"""gkd_retrain.py —— Puzzle P2.8：末段全局 KD 重训。

读 selected_model + flat_model（teacher，冻结）：
  - 端到端 KD：``cosine_kd_loss``（hidden）+ ``logits_kd_loss``（分类才有，
    ``KDWeightScheduler`` warmup）

注：SPEC ``phase-puzzle-impl.md:97`` 提"逐层 cosine KD"，但权威设计草稿
``puzzle-design-draft.md:126`` §4(g) 仅写 ``cosine(hidden)``——两份 SPEC 自相
矛盾（Rule 7）。选 design-draft 路径：对模型最终输出做 cosine + (分类) logits
KD。理由：(a) design-draft 是跨阶段权威 BUILD vs REUSE 表；(b) 嵌入族模型最终
输出本身就是 hidden，cosine 末层即可修块间失配；(c) 逐层 hook 显著复杂化，
YAGNI。

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
    build_calib_loader,
    build_student_from_arch,
    get_module_dummy_input,
    load_father_model,
    load_variant_state_dict,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Puzzle P2.8 GKD 末段重训")
    parser.add_argument("--selected_model", required=True, help="selected_model.pt 路径")
    parser.add_argument("--flat_model", required=True, help="flat_model.py（teacher）")
    parser.add_argument("--build_fn", required=True)
    parser.add_argument("--build_cfg", default="")
    parser.add_argument("--block_map", required=True)
    parser.add_argument("--block_library", required=True)
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
        "GKD teacher 必须预训练；identity slot 的 student 基座也走 father 权重"
        "——空串回退随机 init（仅 dry-run 兼容）",
    )
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument(
        "--train_loader_fn", default="",
        help="真实训练数据 loader 的 path::func(如 proj/train.py::build_dataloader),"
        "零参调用返回 re-iterable DataLoader。GKD **必须用真实训练数据**才能恢复精度——"
        "空串回退合成 calib(仅 dry-run 兼容,acc 恢复弱)",
    )
    parser.add_argument("--batch_size", type=int, default=128)
    parser.add_argument("--hard_label_weight", type=float, default=1.0,
                        help="分类任务 CE hard-label 权重(与 KD 并列;0=纯 KD)")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    torch.manual_seed(args.seed)
    output_dir = Path(args.output_dir).resolve()
    runs_dir = output_dir / "runs" / "retrain"
    runs_dir.mkdir(parents=True, exist_ok=True)
    progress_path = runs_dir / "progress.jsonl"
    final_model_path = runs_dir / "final_model.pt"

    try:
        selected_ckpt = torch.load(
            args.selected_model, map_location="cpu", weights_only=False
        )
        if not isinstance(selected_ckpt, dict) or not selected_ckpt.get("selected_arch"):
            raise ValueError(f"{args.selected_model} 缺 selected_arch 字段")
        selected_arch = selected_ckpt["selected_arch"]
        selected_state_dict = selected_ckpt.get("state_dict", {})

        block_map = BlockMap.from_json(args.block_map)
        device = torch.device("cpu")

        # teacher = father（预训练,冻结）
        teacher = load_father_model(
            args.flat_model, args.build_fn, args.build_cfg, args.father_state
        )
        teacher.eval().to(device)
        for p in teacher.parameters():
            p.requires_grad_(False)

        # student = 共享 helper 重建异构架构。father_state 注入使 identity slot
        # 基座为预训练父权重（路径 A，兜底）；非 identity slot 由 factory + variant
        # ckpt 覆盖。下方 selected_state_dict 覆盖是主路径（路径 B）——build_selected
        # 已注入 father 权重并保存进 selected_model.pt,这里 load 回来覆盖 identity slot
        # 的训练续承（GKD 续训的起点）。
        student = build_student_from_arch(
            flat_model_path=args.flat_model,
            build_fn=args.build_fn,
            build_cfg=args.build_cfg,
            block_map=block_map,
            selected_arch=selected_ckpt,
            block_library_dir=Path(args.block_library).resolve(),
            device=device,
            father_state_path=args.father_state,
        )
        # 载 selected_model.pt 的整体 state_dict（含未被替换模块的父权重）
        if selected_state_dict:
            missing, unexpected = student.load_state_dict(
                selected_state_dict, strict=False
            )
            # 允许少量 missing（factory 重建的 variant 块参数已在 build 时载入），
            # 但 unexpected 必须为 0（否则 selected_state_dict 与 student schema 不一致）。
            if unexpected:
                raise RuntimeError(
                    f"selected_model state_dict 有 {len(unexpected)} 个 unexpected "
                    f"key（schema 不一致）：{list(unexpected)[:5]}"
                )
            if missing:
                # 不 raise（variant 块的 factory 初始化权重在 build_selected 时已载入），
                # 但记录到 stderr 供 fidelity-verifier 审计追溯。
                print(
                    f"WARN: load_state_dict missing {len(missing)} keys "
                    f"(variant factory init): {list(missing)[:5]}",
                    file=sys.stderr,
                )
        # GKD 阶段 student.train():student 是被优化对象,BN 统计需更新 + dropout 提供正则;
        # teacher 才是 eval(冻结)。纯 eval 会让 BN 用随机 running stats,放大 cosine loss 噪声。
        student.to(device).train()

        dummy_meta = get_module_dummy_input(args.flat_model)
        # 优先用真实训练数据(GKD 恢复精度的关键);空串回退合成 calib(仅 dry-run)
        train_loader = None
        if args.train_loader_fn:
            from puzzle_common import load_external_callable
            loader_fn = load_external_callable(args.train_loader_fn)
            train_loader = loader_fn()
            if not hasattr(train_loader, "__iter__"):
                raise TypeError(
                    f"train_loader_fn {args.train_loader_fn!r} 未返回可迭代 DataLoader"
                )
        if train_loader is None:
            print(
                "WARN: 未提供 --train_loader_fn,GKD 回退合成 calib 数据——"
                "acc 恢复会显著偏弱(仅 dry-run 用)",
                file=sys.stderr,
            )
            train_loader = build_calib_loader(
                teacher, dummy_meta, batch_size=2, device=device
            )

        from nas_agent.train.distillation import (
            KDWeightScheduler,
            cosine_kd_loss,
            logits_kd_loss,
        )

        is_classification = args.eval_kind == "classification"
        kd_sched = KDWeightScheduler(
            target_weight=1.0, start=0, warmup_length=max(1, args.epochs // 2)
        )
        opt = torch.optim.Adam(
            (p for p in student.parameters() if p.requires_grad), lr=args.lr
        )

        step = 0
        import torch.nn.functional as _F
        with open(progress_path, "a", encoding="utf-8") as flog:
            for ep in range(args.epochs):
                kd_w = kd_sched.get_weight(ep)
                for batch in train_loader:
                    if isinstance(batch, (list, tuple)):
                        inp = batch[0]
                        labels = batch[1] if len(batch) > 1 else None
                    else:
                        inp = batch
                        labels = None
                    inp = inp.to(device)
                    if labels is not None:
                        labels = labels.to(device)
                    with torch.no_grad():
                        t_out = teacher(inp)
                        if isinstance(t_out, tuple):
                            t_out = t_out[0]
                    s_out = student(inp)
                    if isinstance(s_out, tuple):
                        s_out = s_out[0]
                    loss_cos = cosine_kd_loss(s_out, t_out)
                    loss = loss_cos
                    loss_logits_val = 0.0
                    if is_classification:
                        loss_logits = logits_kd_loss(s_out, t_out)
                        loss = loss + kd_w * loss_logits
                        loss_logits_val = float(loss_logits.item())
                        # hard-label CE(真实标签,分类任务 acc 恢复的关键监督)
                        if labels is not None and args.hard_label_weight > 0:
                            loss_ce = _F.cross_entropy(s_out, labels)
                            loss = loss + args.hard_label_weight * loss_ce
                    opt.zero_grad(set_to_none=True)
                    loss.backward()
                    opt.step()
                    step += 1
                    metrics = {
                        "loss": float(loss.item()),
                        "kd_cos": float(loss_cos.item()),
                    }
                    if is_classification:
                        metrics["kd_logits"] = loss_logits_val
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
