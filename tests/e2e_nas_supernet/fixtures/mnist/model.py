"""MNIST CNN -- a small, supernet-expandable image classifier.

A plain PyTorch LeNet-style CNN intended as the **input user project** for the
``nas-supernet`` workflow E2E test. The architecture is a standard stack of
parameterized conv blocks + a fully-connected head, so the expand-to-supernet
agent can discover searchable dimensions (conv channel widths, FC hidden width)
straight from the constructor parameters -- no exotic custom ops.

Input : ``[B, 1, 28, 28]`` grayscale
Output: ``[B, num_classes]`` logits

This file deliberately stays a *normal user project*: it does NOT expose any
kd-nas / nas-agent-specific contract (no KNOBS, no feature hooks, no DUMMY_INPUT
descriptor). It is just a model class + a convenience factory.
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F


class MnistCNN(nn.Module):
    """Two conv blocks (Conv-BN-ReLU-MaxPool) + a two-layer FC head.

    Spatial flow: 28x28 --conv1--> 14x14 --conv2--> 7x7, then flatten to
    ``7*7*conv2_channels`` and through ``fc1 -> ReLU -> Dropout -> fc2``.

    Args:
        conv1_channels: width of the first conv block.
        conv2_channels: width of the second conv block.
        fc_hidden: width of the penultimate FC layer.
        num_classes: number of output logits.
        dropout: dropout probability on the FC hidden layer.
    """

    def __init__(
        self,
        conv1_channels: int = 16,
        conv2_channels: int = 32,
        fc_hidden: int = 64,
        num_classes: int = 10,
        dropout: float = 0.25,
    ) -> None:
        super().__init__()
        self.conv1 = nn.Sequential(
            nn.Conv2d(1, conv1_channels, kernel_size=3, padding=1),
            nn.BatchNorm2d(conv1_channels),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),  # 28 -> 14
        )
        self.conv2 = nn.Sequential(
            nn.Conv2d(conv1_channels, conv2_channels, kernel_size=3, padding=1),
            nn.BatchNorm2d(conv2_channels),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),  # 14 -> 7
        )
        self.flat_dim = 7 * 7 * conv2_channels
        self.fc1 = nn.Linear(self.flat_dim, fc_hidden)
        self.dropout = nn.Dropout(dropout)
        self.fc2 = nn.Linear(fc_hidden, num_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        z1 = self.conv1(x)
        z2 = self.conv2(z1)
        h = z2.flatten(1)
        h = F.relu(self.fc1(h))
        h = self.dropout(h)
        return self.fc2(h)


def build_model(**kwargs) -> MnistCNN:
    """Instantiate ``MnistCNN``; forwards keyword args to the constructor."""
    return MnistCNN(**kwargs)


def count_parameters(model: nn.Module) -> int:
    """Number of trainable parameters (used by supernet before/after compare)."""
    return sum(p.numel() for p in model.parameters())


if __name__ == "__main__":
    # Forward smoke: verify the output shape on a single dummy sample.
    model = build_model().eval()
    sample = torch.randn(1, 1, 28, 28)
    with torch.no_grad():
        out = model(sample)
    assert out.shape == (1, 10), out.shape
    print(
        "OK MnistCNN "
        f"conv1={model.conv1[0].out_channels} "
        f"conv2={model.conv2[0].out_channels} "
        f"fc={model.fc1.out_features} "
        f"params={count_parameters(model)} "
        f"out={tuple(out.shape)}"
    )
