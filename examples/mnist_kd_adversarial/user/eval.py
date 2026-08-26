"""eval.py — adversarial fixture leaf (intentionally hand-authored).

See examples/mnist_kd_adversarial/README.md. Ports the user's top-1 accuracy
eval verbatim.
"""

import os

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torchvision import datasets, transforms

_MNIST_MEAN = (0.1307,)
_MNIST_STD = (0.3081,)
_DATA_ROOT = os.environ.get(
    "MNIST_DATA_ROOT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"),
)


def _eval_loader(batch_size: int = 256) -> DataLoader:
    tfm = transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Normalize(_MNIST_MEAN, _MNIST_STD),
        ]
    )
    ds = datasets.MNIST(
        _DATA_ROOT, train=False, download=True, transform=tfm
    )
    return DataLoader(
        ds, batch_size=batch_size, shuffle=False, num_workers=0
    )


def eval_metric(student: nn.Module, device) -> tuple:
    """Top-1 accuracy on the MNIST test set. Returns (accuracy, "acc")."""
    dev = torch.device(device)
    student = student.to(dev).eval()
    loader = _eval_loader()
    correct, total = 0, 0
    with torch.no_grad():
        for x, y in loader:
            x = x.to(dev)
            y = y.to(dev)
            pred = student(x).argmax(dim=1)
            correct += (pred == y).sum().item()
            total += y.size(0)
    if total == 0:
        raise RuntimeError("test_loader 为空，无法评估 accuracy")
    return correct / total, "acc"
