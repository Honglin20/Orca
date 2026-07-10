"""ConfigurableMLP for sklearn digits classification (8x8=64 features, 10 classes).

Real PyTorch nn.Module —— bitx 量化分析的目标模型。

Architecture (in_dim=64, hidden_dim=64, num_classes=10, num_layers=2, relu, no bn):
    Flatten -> Linear(64, 64) -> ReLU
            -> Linear(64, 64) -> ReLU
            -> Linear(64, 10)

三层全连接，便于 bitx 跑出有意义的 per-layer QSNR。
"""
from __future__ import annotations

import torch
import torch.nn as nn


class ConfigurableMLP(nn.Module):
    """MLP configurable via init args.

    Architecture:
        Flatten -> [Linear(in_dim, hidden_dim) -> Act] x num_layers -> Linear(hidden_dim, num_classes)

    All hidden layers keep the same width ``hidden_dim`` (no //2 shrink) so checkpoint
    shapes are predictable and bitx per-layer QSNR has consistent layer widths.
    """

    def __init__(
        self,
        in_dim: int = 64,
        num_classes: int = 10,
        hidden_dim: int = 64,
        num_layers: int = 2,
        activation: str = "relu",
        use_batchnorm: bool = False,
    ) -> None:
        super().__init__()
        act_map = {"relu": nn.ReLU, "gelu": nn.GELU, "tanh": nn.Tanh, "silu": nn.SiLU}
        Act = act_map.get(activation, nn.ReLU)

        layers: list[nn.Module] = [nn.Flatten()]
        # First hidden layer: in_dim -> hidden_dim
        layers.append(nn.Linear(in_dim, hidden_dim))
        if use_batchnorm:
            layers.append(nn.BatchNorm1d(hidden_dim))
        layers.append(Act())
        # Remaining hidden layers: hidden_dim -> hidden_dim (constant width)
        for _ in range(num_layers - 1):
            layers.append(nn.Linear(hidden_dim, hidden_dim))
            if use_batchnorm:
                layers.append(nn.BatchNorm1d(hidden_dim))
            layers.append(Act())
        # Output layer: hidden_dim -> num_classes
        layers.append(nn.Linear(hidden_dim, num_classes))
        self.net = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


def count_parameters(model: nn.Module) -> int:
    """Count trainable parameters."""
    return sum(p.numel() for p in model.parameters())


def dummy_inputs(batch_size: int = 1) -> torch.Tensor:
    """Construct dummy inputs for ONNX export / latency benchmarking."""
    return torch.randn(batch_size, 1, 8, 8)
