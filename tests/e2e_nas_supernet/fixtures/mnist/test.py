"""Evaluate ``MnistCNN`` on the MNIST test set; print top-1 accuracy.

``evaluate(model, test_loader, device)`` returns accuracy in ``[0, 1]``. Running
this file directly loads a checkpoint produced by ``train.py`` and prints the
contract lines::

    ACCURACY: <float>
    ACCURACY_KIND: acc
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import torch
import torch.nn as nn
from torch.utils.data import DataLoader

HERE = Path(__file__).resolve().parent


def evaluate(
    model: nn.Module,
    test_loader: DataLoader,
    device: str | torch.device = "cpu",
) -> float:
    """Top-1 accuracy on the test set (returns ``correct / total`` in 0..1)."""
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
        raise RuntimeError("test_loader is empty; cannot evaluate accuracy")
    return correct / total


def main() -> int:
    parser = argparse.ArgumentParser(description="MNIST test-set accuracy evaluation")
    parser.add_argument("--ckpt", default=str(HERE / "mnist_cnn.pt"))
    parser.add_argument("--batch_size", type=int, default=256)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--data_root", default=str(HERE / "data"))
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch_size must be >= 1")

    ckpt = Path(args.ckpt)
    if not ckpt.is_file():
        print(
            f"checkpoint not found: {ckpt} (run `python train.py` first)",
            file=sys.stderr,
        )
        return 2

    # Lazy import: avoids a train<->test circular import at module load time.
    from model import build_model
    from train import build_dataloader

    model = build_model()
    state = torch.load(str(ckpt), map_location=args.device, weights_only=True)
    model.load_state_dict(state)

    test_loader = build_dataloader(
        batch_size=args.batch_size,
        train=False,
        root=Path(args.data_root),
    )
    acc = evaluate(model, test_loader, device=args.device)
    print(f"ACCURACY: {acc:.4f}")
    print("ACCURACY_KIND: acc")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
