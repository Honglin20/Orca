"""Flat variant whose output is a hidden vector (embedding), not class logits.

Used by the eval-kind-mislabel fixture: the manifest declares
``eval_kind: classification`` but the model emits a per-sample embedding vector
(consumed by a k-NN / cosine retrieval metric). A correct search-space-evaluator
must flag the paradigm mismatch.
"""

from __future__ import annotations

import torch
import torch.nn as nn

DIM = 32
EMBED_DIM = 16
NUM_CLASSES = 10


class TinyAttention(nn.Module):
    def __init__(self, dim: int = DIM, num_heads: int = 4):
        super().__init__()
        self.num_heads = num_heads
        self.head_dim = dim // num_heads
        self.qkv = nn.Linear(dim, dim * 3)
        self.proj = nn.Linear(dim, dim)
        self.scale = self.head_dim ** -0.5

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, self.head_dim).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]
        scores = torch.matmul(q, k.transpose(-2, -1)) * self.scale
        attn = torch.softmax(scores, dim=-1)
        out = torch.matmul(attn, v).transpose(1, 2).reshape(B, N, C)
        return self.proj(out)


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
        self.attn = TinyAttention(dim)
        self.ffn = FeedForward(dim)
        self.norm = nn.LayerNorm(dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.attn(self.norm(x))
        x = x + self.ffn(self.norm(x))
        return x


class EmbeddingTransformer(nn.Module):
    """Emits a per-sample embedding vector — there is no classification head."""

    def __init__(self, dim: int = DIM, embed_dim: int = EMBED_DIM):
        super().__init__()
        self.blocks = nn.ModuleList([TinyBlock(dim), TinyBlock(dim)])
        self.norm = nn.LayerNorm(dim)
        self.embed = nn.Linear(dim, embed_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        for blk in self.blocks:
            x = blk(x)
        x = self.norm(x)
        return self.embed(x.mean(dim=1))


def build_model() -> nn.Module:
    return EmbeddingTransformer()


DUMMY_INPUT = {"shape": [2, 8, DIM], "dtype": "float32"}


if __name__ == "__main__":
    m = build_model()
    out = m(torch.randn(*DUMMY_INPUT["shape"]))
    print(f"OK out={tuple(out.shape)}")
