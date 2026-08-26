"""train.py —— MNIST 训练任务：loss / dataloader / optimizer / scheduler。

约定（被下游工具消费）：
    compute_loss(out, y)        —— 任务损失（分类用 cross-entropy）。
    build_dataloader(batch_size)—— 返回 re-iterable 的训练 DataLoader（torchvision MNIST）。
    build_optimizer / build_scheduler —— 自然用户实现。

直接运行（``python train.py --epochs N``）会真跑训练并在 test set 上打印精度。
数据自动下载到 ``./data``。
"""

from __future__ import annotations

import argparse
import os

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torchvision import datasets, transforms

# 常量：MNIST 标准归一化统计量 + 数据根目录。
_MNIST_MEAN = (0.1307,)
_MNIST_STD = (0.3081,)
_DATA_ROOT = os.environ.get("MNIST_DATA_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))


def compute_loss(logits: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    """分类任务 loss：cross-entropy。logits=[B,10]，y=[B] long。"""
    return F.cross_entropy(logits, y)


def _transform() -> transforms.Compose:
    return transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize(_MNIST_MEAN, _MNIST_STD),
    ])


def _load_dataset(train: bool, batch_size: int, root: str = _DATA_ROOT) -> DataLoader:
    """构造 torchvision MNIST DataLoader（re-iterable：每个 epoch 自动重启迭代）。"""
    ds = datasets.MNIST(root, train=train, download=True, transform=_transform())
    return DataLoader(
        ds,
        batch_size=batch_size,
        shuffle=train,   # 训练集打乱，测试集不打乱
        num_workers=0,   # headless / CPU 环境稳健
        pin_memory=False,
    )


def build_dataloader(batch_size: int = 128, train: bool = True) -> DataLoader:
    """构造 DataLoader。默认返回训练集（蒸馏/训练流水消费）。"""
    return _load_dataset(train=train, batch_size=batch_size)


def build_optimizer(params, lr: float = 1e-3) -> torch.optim.Optimizer:
    return torch.optim.Adam(params, lr=lr)


def build_scheduler(optimizer: torch.optim.Optimizer, epochs: int = 10):
    return torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(epochs, 1))


def train_one_epoch(
    model: nn.Module,
    loader: DataLoader,
    optimizer: torch.optim.Optimizer,
    device: torch.device,
) -> float:
    model.train()
    running, n = 0.0, 0
    for x, y in loader:
        x, y = x.to(device), y.to(device)
        optimizer.zero_grad()
        logits = model(x)
        loss = compute_loss(logits, y)
        loss.backward()
        optimizer.step()
        bs = y.size(0)
        running += loss.item() * bs
        n += bs
    if n == 0:
        raise RuntimeError("train_loader 为空：未产出任何 batch")
    return running / n


def main() -> int:
    p = argparse.ArgumentParser(description="MNIST 训练（LeNet 风格 CNN）")
    p.add_argument("--epochs", type=int, default=10)
    p.add_argument("--batch_size", type=int, default=128)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--device", default="cpu")
    p.add_argument("--ckpt", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "mnist_cnn.pt"))
    args = p.parse_args()

    if args.epochs < 1:
        p.error("--epochs 必须 >= 1")
    if args.batch_size < 1:
        p.error("--batch_size 必须 >= 1")

    # 延迟导入：仅直接运行时依赖 model.py。
    from model import build_model

    device = torch.device(args.device)
    model = build_model().to(device)
    optimizer = build_optimizer(model.parameters(), lr=args.lr)
    scheduler = build_scheduler(optimizer, epochs=args.epochs)

    train_loader = build_dataloader(batch_size=args.batch_size, train=True)
    test_loader = build_dataloader(batch_size=args.batch_size, train=False)

    # 延迟导入 eval：避免循环依赖。
    from eval import evaluate

    for ep in range(1, args.epochs + 1):
        loss = train_one_epoch(model, train_loader, optimizer, device)
        scheduler.step()
        acc = evaluate(model, test_loader, device=device)
        print(f"epoch {ep}/{args.epochs}  loss={loss:.4f}  test_acc={acc:.4f}")

    torch.save(model.state_dict(), args.ckpt)
    print(f"saved checkpoint -> {args.ckpt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
