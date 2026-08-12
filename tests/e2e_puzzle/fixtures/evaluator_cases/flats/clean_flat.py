"""Canonical clean tiny transformer for evaluator fixtures.

Self-contained flat model: build_model() -> nn.Module, DUMMY_INPUT, __main__.
Two TinyBlocks, each with a real scaled-dot-product attention (QK^T + softmax)
and a standard FFN (Linear -> GELU -> Linear). Used as the ground truth by the
clean-baseline fixture and by fixtures whose seeded error lives in search_space /
manifest rather than in the model source.
"""

from __future__ import annotations

import torch
import torch.nn as nn

DIM = 32
NUM_HEADS = 4
HEAD_DIM = 8
FFN_INTERMEDIATE = 64
NUM_CLASSES = 10


class TinyAttention(nn.Module):
    """Real scaled-dot-product attention over a single sequence."""

    def __init__(self, dim: int = DIM, num_heads: int = NUM_HEADS):
        super().__init__()
        assert dim % num_heads == 0
        self.num_heads = num_heads
        self.head_dim = dim // num_heads
        self.qkv = nn.Linear(dim, dim * 3)
        self.proj = nn.Linear(dim, dim)
        self.scale = self.head_dim ** -0.5

    def forward(self, x: torch.Tensor, attention_mask: torch.Tensor | None = None) -> torch.Tensor:
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, self.head_dim)
        qkv = qkv.permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]
        # scaled dot product: QK^T * scale, then softmax over the key axis.
        scores = torch.matmul(q, k.transpose(-2, -1)) * self.scale
        if attention_mask is not None:
            scores = scores + attention_mask
        attn = torch.softmax(scores, dim=-1)
        out = torch.matmul(attn, v).transpose(1, 2).reshape(B, N, C)
        return self.proj(out)


class FeedForward(nn.Module):
    """Standard FFN: Linear -> GELU -> Linear."""

    def __init__(self, dim: int = DIM, intermediate: int = FFN_INTERMEDIATE):
        super().__init__()
        self.fc1 = nn.Linear(dim, intermediate)
        self.act = nn.GELU()
        self.fc2 = nn.Linear(intermediate, dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.fc2(self.act(self.fc1(x)))


class TinyBlock(nn.Module):
    def __init__(self, dim: int = DIM):
        super().__init__()
        self.attn = TinyAttention(dim)
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
