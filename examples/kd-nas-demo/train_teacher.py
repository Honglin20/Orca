"""train_teacher.py —— 训 10 层 teacher（teacher_model.py 的架构），随机数据，存 ckpt。

[RETIRED v4] 本脚本在 v4 嵌入（2026-07-31）前是 kd-nas workflow 的 ``teacher_train_command``
（setup 节点 ``cd $PROJECT_ROOT && <本命令>`` 原样执行）。v4 起 teacher 训练改由 train-script-gen
产出的 ``train_pipeline.py --mode teacher`` 驱动（固定 ``--out_ckpt``，自包含搬用户 loss/dataloader），
本脚本不再是 workflow 入口。保留作历史参考 + teacher_model.py 架构的独立训练 demo。

命令形态::

    python train_teacher.py --out <ckpt_path> [--epochs N] [--batch-size N] [--n-batches N] [--seed N]

teacher 只作 KD 软标签源（精度基线用户另给），故**随机数据 + 1 epoch** 即可——目的是产出一个能被
``teacher_model.build_model()`` 加载的 ckpt，让 teacher_setup 生成 teacher_cache.pt。

**绝不伪造**：真实前向 + 真实 MSE 反传 + 真实 ckpt 落盘；不求收敛（随机数据无意义）。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys

import torch
import torch.nn as nn


def _find_repo_root() -> str:
    """从本文件向上找 .git / pyproject.toml → repo 根（teacher_model.py 在 repo 内）。"""
    p = os.path.dirname(os.path.abspath(__file__))
    while p and p != os.path.dirname(p):
        if os.path.exists(os.path.join(p, ".git")) or os.path.isfile(os.path.join(p, "pyproject.toml")):
            return p
        p = os.path.dirname(p)
    raise RuntimeError("找不到 repo 根（.git / pyproject.toml）—— train_teacher.py 必须在 Orca 仓库内")


def _load_teacher_factory():
    """按路径 import 仓库的 teacher_model.py，返回其 build_model（10 层 t1/t2 交替架构）。"""
    repo = _find_repo_root()
    teacher_model_path = os.path.join(repo, "workflows", "agents", "_kd_scripts", "teacher_model.py")
    if not os.path.isfile(teacher_model_path):
        raise FileNotFoundError(
            f"teacher_model.py 不存在: {teacher_model_path}\n"
            "（demo 必须在 Orca 仓库内运行；teacher 架构由 workflow 提供于 _kd_scripts/）"
        )
    spec = importlib.util.spec_from_file_location("_demo_teacher_model", teacher_model_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    factory = getattr(mod, "build_model", None)
    if not callable(factory):
        raise AttributeError(f"{teacher_model_path} 无 callable build_model")
    return factory


def main() -> int:
    p = argparse.ArgumentParser(description="kd-nas-demo teacher 训练（随机数据，产 ckpt）")
    p.add_argument("--out", required=True, help="teacher ckpt 输出路径（state_dict 格式）")
    p.add_argument("--epochs", type=int, default=1, help="训练 epoch（默认 1，demo 不求收敛）")
    p.add_argument("--batch-size", type=int, default=2, help="随机数据 batch size（默认 2）")
    p.add_argument("--n-batches", type=int, default=4, help="每 epoch batch 数（默认 4）")
    p.add_argument("--seed", type=int, default=0, help="复现种子（默认 0）")
    args = p.parse_args()

    torch.manual_seed(args.seed)
    # 确定性（best-effort；CPU 上无 cudnen 影响）。
    try:
        torch.use_deterministic_algorithms(True, warn_only=True)
    except Exception:
        pass

    build = _load_teacher_factory()
    teacher = build()  # 10 层 t1/t2 交替（teacher_model.build_model 默认）
    if not isinstance(teacher, torch.nn.Module):
        raise TypeError(f"teacher_model.build_model() 返回 {type(teacher).__name__}，期望 nn.Module")

    device = torch.device("cpu")  # demo 默认 CPU；GPU 机可改 --device 扩展（当前不暴露）。
    teacher = teacher.to(device).train()
    optimizer = torch.optim.Adam(teacher.parameters(), lr=1e-3)
    loss_fn = nn.MSELoss()

    epochs = max(args.epochs, 1)
    batches = max(args.n_batches, 1)
    bs = max(args.batch_size, 1)
    shape = (bs, 4, 48, 64, 1)

    last_loss = float("nan")
    for epoch in range(epochs):
        epoch_loss = 0.0
        for _ in range(batches):
            x = torch.randn(*shape, device=device)
            y = torch.randn(*shape, device=device)  # 随机目标（demo：不求收敛，只撑训练流水）
            out = teacher(x)
            target = y.view_as(out)
            loss = loss_fn(out, target)
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()
            epoch_loss += float(loss.detach())
        last_loss = epoch_loss / batches
        print(f"[train_teacher] epoch {epoch} loss_avg={last_loss:.6f}", flush=True)

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    torch.save(
        {
            "state_dict": teacher.state_dict(),
            "build_cfg": {},  # teacher_model.build_model() 零参；teacher_setup 用默认架构重建
            "format": "kd-nas-demo teacher (10-layer t1/t2交替)",
            "epochs": epochs,
            "final_loss": last_loss,
        },
        out_path,
    )
    print(f"TEACHER_CKPT: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
