"""eval.py —— MNIST 测试集精度评估。

``evaluate(model, test_loader, device)`` 返回 top-1 accuracy（0..1 浮点）。
直接运行 ``python eval.py [--ckpt path]`` 会加载 checkpoint 并打印精度。
"""

from __future__ import annotations

import argparse
import os

import torch
import torch.nn as nn
from torch.utils.data import DataLoader


def evaluate(model: nn.Module, test_loader: DataLoader, device: str | torch.device = "cpu") -> float:
    """在 test set 上计算 top-1 accuracy。

    Args:
        model: 已实例化的 MNIST 模型。
        test_loader: 测试集 DataLoader（``build_dataloader(train=False)``）。
        device: 计算设备。

    Returns:
        top-1 accuracy（0..1）。
    """
    device = torch.device(device)
    model = model.to(device).eval()
    correct, total = 0, 0
    with torch.no_grad():
        for x, y in test_loader:
            x, y = x.to(device), y.to(device)
            pred = model(x).argmax(dim=1)
            correct += (pred == y).sum().item()
            total += y.size(0)
    if total == 0:
        raise RuntimeError("test_loader 为空，无法评估 accuracy")
    return correct / total


def main() -> int:
    p = argparse.ArgumentParser(description="MNIST 测试集精度评估")
    here = os.path.dirname(os.path.abspath(__file__))
    p.add_argument("--ckpt", default=os.path.join(here, "mnist_cnn.pt"))
    p.add_argument("--batch_size", type=int, default=256)
    p.add_argument("--device", default="cpu")
    args = p.parse_args()

    if not os.path.isfile(args.ckpt):
        print(f"checkpoint 不存在: {args.ckpt}（先跑 ``python train.py``）")
        return 2

    from model import build_model
    from train import build_dataloader

    model = build_model()
    state = torch.load(args.ckpt, map_location=args.device)
    model.load_state_dict(state)
    test_loader = build_dataloader(batch_size=args.batch_size, train=False)
    acc = evaluate(model, test_loader, device=args.device)
    print(f"ACCURACY: {acc:.4f}")
    print(f"ACCURACY_KIND: acc")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
