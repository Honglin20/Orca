"""Flat variant whose attention module returns a (out, weights) tuple.

Used by the return-arity-violation fixture: the slot is declared
``return_arity: multi`` (the parent forward consumes both tensors), but the
offered candidates are single-output blocks. A correct evaluator must flag that
single-output candidates cannot replace a multi-return slot.
"""

from __future__ import annotations

import torch
import torch.nn as nn

DIM = 32
NUM_CLASSES = 10


class MultiReturnAttention(nn.Module):
    """Attention that returns both the output and the attention weights."""

    def __init__(self, dim: int = DIM, num_heads: int = 4):
        super().__init__()
        self.num_heads = num_heads
        self.head_dim = dim // num_heads
        self.qkv = nn.Linear(dim, dim * 3)
        self.proj = nn.Linear(dim, dim)
        self.scale = self.head_dim ** -0.5

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, self.head_dim).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]
        scores = torch.matmul(q, k.transpose(-2, -1)) * self.scale
        attn = torch.softmax(scores, dim=-1)
        out = torch.matmul(attn, v).transpose(1, 2).reshape(B, N, C)
        return self.proj(out), attn


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
        self.attn = MultiReturnAttention(dim)
        self.ffn = FeedForward(dim)
        self.norm = nn.LayerNorm(dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out, _weights = self.attn(self.norm(x))
        x = x + out
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
