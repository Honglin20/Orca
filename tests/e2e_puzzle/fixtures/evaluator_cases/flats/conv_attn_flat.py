"""Flat variant whose ``blocks.0.attn`` is a Conv1d mixer, not attention.

Used by the conv-mislabeled-as-attention fixture: the slot is declared
``kind: attention`` but the module source contains a convolution with no QK^T
scaling and no softmax over scores. A correct evaluator must flag the kind label.
"""

from __future__ import annotations

import torch
import torch.nn as nn

DIM = 32
NUM_CLASSES = 10


class ConvMixer(nn.Module):
    """A 1-D convolution mixer that has no query/key dot product at all."""

    def __init__(self, dim: int = DIM):
        super().__init__()
        self.conv = nn.Conv1d(dim, dim, kernel_size=3, padding=1, groups=1)
        self.proj = nn.Linear(dim, dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, N, C] -> conv operates on the sequence axis as channels.
        y = self.conv(x.transpose(1, 2)).transpose(1, 2)
        return self.proj(torch.relu(y))


class FeedForward(nn.Module):
    def __init__(self, dim: int = DIM, intermediate: int = 64):
        super().__init__()
        self.fc1 = nn.Linear(dim, intermediate)
        self.act = nn.GELU()
        self.fc2 = nn.Linear(intermediate, dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.fc2(self.act(self.fc1(x)))


class TinyBlock(nn.Module):
    def __init__(self, dim: int = DIM):
        super().__init__()
        self.attn = ConvMixer(dim)
        self.ffn = FeedForward(dim)
        self.norm = nn.LayerNorm(dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.attn(self.norm(x))
        x = x + self.ffn(self.norm(x))
        return x


class TinyTransformer(nn.Module):
    def __init__(self, dim: int = DIM, num_classes: int = NUM_CLASSES):
        super().__init__()
        self.blocks = nn.ModuleList([TinyBlock(dim), TinyBlock(dim)])
        self.norm = nn.LayerNorm(dim)
        self.head = nn.Linear(dim, num_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        for blk in self.blocks:
            x = blk(x)
        x = self.norm(x)
        return self.head(x.mean(dim=1))


def build_model() -> nn.Module:
    return TinyTransformer()


DUMMY_INPUT = {"shape": [2, 8, DIM], "dtype": "float32"}


if __name__ == "__main__":
    m = build_model()
    out = m(torch.randn(*DUMMY_INPUT["shape"]))
    print(f"OK out={tuple(out.shape)}")
