"""Standard MNIST training loop for ``MnistCNN``.

Minimizes cross-entropy on torchvision MNIST (auto-download). When the dataset
cannot be obtained (offline / sandboxed CI without network), the loader degrades
to small random tensors so the script still exits cleanly for plumbing smoke.
That fallback is **smoke-only**: accuracy is meaningless on random data, so real
E2E must run on a host with network access to download MNIST.

Run:
    python train.py --epochs 2 --batch_size 128 --lr 1e-3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, TensorDataset

HERE = Path(__file__).resolve().parent
DEFAULT_DATA_ROOT = HERE / "data"

# Standard MNIST normalization statistics.
_MNIST_MEAN = (0.1307,)
_MNIST_STD = (0.3081,)


def compute_loss(logits: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    """Classification loss: cross-entropy over ``[B, num_classes]`` logits."""
    return F.cross_entropy(logits, y)


def _mnist_transform():
    from torchvision import transforms

    return transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Normalize(_MNIST_MEAN, _MNIST_STD),
        ]
    )


def _random_loader(batch_size: int, train: bool, smoke_n: int) -> DataLoader:
    """Smoke-only loader over random tensors (used when MNIST is unavailable)."""
    x = torch.randn(smoke_n, 1, 28, 28)
    # 10 classes matches MnistCNN's default num_classes; smoke-only.
    y = torch.randint(0, 10, (smoke_n,))
    return DataLoader(TensorDataset(x, y), batch_size=batch_size, shuffle=train)


def build_dataloader(
    batch_size: int = 128,
    train: bool = True,
    root: Path = DEFAULT_DATA_ROOT,
    smoke_n: int = 1024,
) -> DataLoader:
    """Build a DataLoader over real MNIST; fall back to random tensors offline.

    The fallback is loud (stderr warning) and exists only so ``python train.py``
    always exercises the full forward/backward/save path on hosts without
    network access. Real E2E runs must use real MNIST.
    """
    try:
        from torchvision import datasets

        ds = datasets.MNIST(str(root), train=train, download=True, transform=_mnist_transform())
        return DataLoader(
            ds,
            batch_size=batch_size,
            shuffle=train,
            num_workers=0,  # robust on headless / CPU
            pin_memory=False,
        )
    except Exception as exc:  # noqa: BLE001 -- any failure -> loud smoke fallback
        print(
            f"[train] WARNING: torchvision MNIST unavailable ({type(exc).__name__}: {exc}); "
            f"falling back to {smoke_n} random tensors (SMOKE ONLY, accuracy is meaningless).",
            file=sys.stderr,
        )
        return _random_loader(batch_size, train, smoke_n)


def build_optimizer(params, lr: float = 1e-3) -> torch.optim.Optimizer:
    return torch.optim.Adam(params, lr=lr)


def train_one_epoch(
    model: nn.Module,
    loader: DataLoader,
    optimizer: torch.optim.Optimizer,
    device: torch.device,
) -> float:
    """Run one training epoch; return the mean per-sample cross-entropy loss."""
    model.train()
    running, n = 0.0, 0
    for x, y in loader:
        x, y = x.to(device), y.to(device)
        optimizer.zero_grad()
        loss = compute_loss(model(x), y)
        loss.backward()
        optimizer.step()
        bs = y.size(0)
        running += loss.item() * bs
        n += bs
    if n == 0:
        raise RuntimeError("train_loader yielded no batches; cannot train")
    return running / n


def main() -> int:
    parser = argparse.ArgumentParser(description="MNIST training (small CNN)")
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--batch_size", type=int, default=128)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--data_root", default=str(DEFAULT_DATA_ROOT))
    parser.add_argument("--ckpt", default=str(HERE / "mnist_cnn.pt"))
    args = parser.parse_args()
    if args.epochs < 1:
        parser.error("--epochs must be >= 1")
    if args.batch_size < 1:
        parser.error("--batch_size must be >= 1")

    # Lazy import: the fixture is a flat directory imported by file path.
    from model import build_model
    from test import evaluate

    device = torch.device(args.device)
    model = build_model().to(device)
    optimizer = build_optimizer(model.parameters(), lr=args.lr)

    data_root = Path(args.data_root)
    train_loader = build_dataloader(batch_size=args.batch_size, train=True, root=data_root)
    test_loader = build_dataloader(batch_size=args.batch_size, train=False, root=data_root)

    for epoch in range(1, args.epochs + 1):
        loss = train_one_epoch(model, train_loader, optimizer, device)
        acc = evaluate(model, test_loader, device=device)
        print(f"epoch {epoch}/{args.epochs}  loss={loss:.4f}  test_acc={acc:.4f}")

    ckpt_path = Path(args.ckpt)
    torch.save(model.state_dict(), str(ckpt_path))
    print(f"saved checkpoint -> {ckpt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
