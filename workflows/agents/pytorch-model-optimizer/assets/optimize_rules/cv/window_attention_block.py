"""Default hierarchical Transformer block: windowed self-attention with RPB + FFN.

This module provides the default building block for converting an isotropic
(flat) vision Transformer into a hierarchical multi-stage backbone. It uses
non-overlapping window attention with mandatory relative position bias (RPB)
and a standard pre-norm Transformer block structure.

I/O: (B, H, W, C) -> (B, H, W, C)  [BHWC layout]

No cross-window mechanism (shifted window, dilated window, etc.) is included.
Those are block-level variants provided by NAS prebuilt blocks during supernet
generation (Step 5).
"""

import torch
import torch.nn as nn
import torch.nn.functional as F


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def window_partition(x: torch.Tensor, window_size: int) -> torch.Tensor:
    """Partition BHWC feature map into non-overlapping windows.

    Args:
        x: input tensor of shape `(B, H, W, C)`.
            H and W must be divisible by `window_size`
            (pad beforehand if needed).
        window_size: side length of each square window.

    Returns:
        Tensor of shape `(B * num_windows, Ws*Ws, C)`.
    """
    B, H, W, C = x.shape
    x = x.view(B, H // window_size, window_size, W // window_size, window_size, C)
    return x.permute(0, 1, 3, 2, 4, 5).contiguous().view(-1, window_size * window_size, C)


def window_reverse(
    windows: torch.Tensor, window_size: int, H: int, W: int, B: int
) -> torch.Tensor:
    """Merge windows back to BHWC feature map.

    Args:
        windows: tensor of shape `(B * num_windows, Ws*Ws, C)`.
        window_size: side length of each square window.
        H: padded height (divisible by `window_size`).
        W: padded width  (divisible by `window_size`).
        B: batch size.

    Returns:
        Tensor of shape `(B, H, W, C)`.
    """
    x = windows.view(B, H // window_size, W // window_size, window_size, window_size, -1)
    return x.permute(0, 1, 3, 2, 4, 5).contiguous().view(B, H, W, -1)


def pad_to_window_size(
    x: torch.Tensor, window_size: int
) -> tuple[torch.Tensor, int, int]:
    """Pad BHWC tensor so that H and W are divisible by `window_size`.

    Padding is applied on the right and bottom edges only. The padding values
    are zero.

    Returns:
        Tuple of (padded tensor, padded H, padded W).
    """
    _, H, W, _ = x.shape
    pad_h = (window_size - H % window_size) % window_size
    pad_w = (window_size - W % window_size) % window_size
    if pad_h == 0 and pad_w == 0:
        return x, H, W
    # Permute to BCHW for F.pad, then back to BHWC
    x = F.pad(x.permute(0, 3, 1, 2), (0, pad_w, 0, pad_h))
    x = x.permute(0, 2, 3, 1).contiguous()
    return x, H + pad_h, W + pad_w


def _build_relative_position_index(window_size: int) -> torch.Tensor:
    """Build pairwise relative position index for RPB table lookup.

    Returns:
        Tensor of shape `(Ws*Ws, Ws*Ws)` containing indices into the
        RPB table of size `(2*Ws-1)^2`.
    """
    coords = torch.stack(torch.meshgrid(
        torch.arange(window_size),
        torch.arange(window_size),
        indexing="ij",
    ))
    coords_flatten = torch.flatten(coords, 1)  # (2, Ws*Ws)
    # (2, Ws*Ws, Ws*Ws) pairwise differences
    relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]
    relative_coords = relative_coords.permute(1, 2, 0).contiguous()
    relative_coords[:, :, 0] += window_size - 1
    relative_coords[:, :, 1] += window_size - 1
    relative_coords[:, :, 0] *= 2 * window_size - 1
    return relative_coords.sum(-1)  # (Ws*Ws, Ws*Ws)


# ---------------------------------------------------------------------------
# Drop path (stochastic depth)
# ---------------------------------------------------------------------------

class DropPath(nn.Module):
    """Drop paths (Stochastic Depth) per sample."""

    def __init__(self, drop_prob: float = 0.0, scale_by_keep: bool = True):
        super().__init__()
        self.drop_prob = drop_prob
        self.scale_by_keep = scale_by_keep

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.drop_prob == 0.0 or not self.training:
            return x
        keep_prob = 1 - self.drop_prob
        shape = (x.shape[0],) + (1,) * (x.ndim - 1)
        random_tensor = x.new_empty(shape).bernoulli_(keep_prob)
        if keep_prob > 0.0 and self.scale_by_keep:
            random_tensor.div_(keep_prob)
        return x * random_tensor


# ---------------------------------------------------------------------------
# Window Attention with mandatory RPB
# ---------------------------------------------------------------------------

class WindowAttention(nn.Module):
    """Multi-head self-attention within local windows, with relative position bias.

    Input/Output: `(num_windows * B, Ws*Ws, C)`
    """

    def __init__(self, dim: int, num_heads: int, window_size: int = 7):
        super().__init__()
        if dim % num_heads != 0:
            raise ValueError(f"dim ({dim}) must be divisible by num_heads ({num_heads}).")
        self.num_heads = num_heads
        self.head_dim = dim // num_heads
        self.scale = self.head_dim ** -0.5
        self.window_size = window_size

        self.qkv = nn.Linear(dim, dim * 3)
        self.proj = nn.Linear(dim, dim)

        # RPB table: (2*Ws-1)^2 entries, one bias per head
        num_rpb_entries = (2 * window_size - 1) ** 2
        self.relative_position_bias_table = nn.Parameter(
            torch.zeros(num_rpb_entries, num_heads)
        )
        nn.init.trunc_normal_(self.relative_position_bias_table, std=0.02)

        # Pre-computed index (fixed for given window_size) — registered as buffer
        self.register_buffer(
            "relative_position_index",
            _build_relative_position_index(window_size),
            persistent=False,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B_, N, C = x.shape  # B_ = num_windows * B, N = Ws*Ws

        qkv = (
            self.qkv(x)
            .reshape(B_, N, 3, self.num_heads, self.head_dim)
            .permute(2, 0, 3, 1, 4)
        )
        q, k, v = qkv.unbind(0)  # each: (B_, num_heads, N, head_dim)

        attn = (q @ k.transpose(-2, -1)) * self.scale  # (B_, num_heads, N, N)

        # Add relative position bias from pre-computed index
        rpb = self.relative_position_bias_table[
            self.relative_position_index.view(-1)
        ].view(N, N, -1)
        rpb = rpb.permute(2, 0, 1).contiguous()  # (num_heads, N, N)
        attn = attn + rpb.unsqueeze(0)

        attn = F.softmax(attn, dim=-1)
        x = (attn @ v).transpose(1, 2).contiguous().view(B_, N, C)
        return self.proj(x)


# ---------------------------------------------------------------------------
# Default Hierarchical Block
# ---------------------------------------------------------------------------

class WindowAttentionBlock(nn.Module):
    """Default hierarchical Transformer block: windowed self-attention with RPB + FFN.

    I/O: `(B, H, W, C) -> (B, H, W, C)`

    Structure (pre-norm):
        - `LayerNorm -> Window Attention (RPB) -> DropPath -> Residual`
        - `LayerNorm -> FFN -> DropPath -> Residual`
    """

    def __init__(
        self,
        embed_dim: int,
        num_heads: int,
        ffn_dim: int,
        window_size: int = 7,
        drop_path_rate: float = 0.0,
    ):
        super().__init__()
        self.window_size = window_size

        self.norm1 = nn.LayerNorm(embed_dim)
        self.attn = WindowAttention(embed_dim, num_heads=num_heads, window_size=window_size)

        self.norm2 = nn.LayerNorm(embed_dim)
        self.mlp = nn.Sequential(
            nn.Linear(embed_dim, ffn_dim),
            nn.GELU(),
            nn.Linear(ffn_dim, embed_dim),
        )

        self.drop_path = DropPath(drop_path_rate) if drop_path_rate > 0.0 else nn.Identity()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, H, W, C)
        B, H, W, C = x.shape

        # --- Attention path ---
        shortcut = x
        x = self.norm1(x)  # LayerNorm on last dim C

        # Pad to window_size multiple, partition, attend, merge, crop
        x, Hp, Wp = pad_to_window_size(x, self.window_size)
        x_windows = window_partition(x, self.window_size)   # (B*nW, Ws*Ws, C)
        attn_windows = self.attn(x_windows)                 # (B*nW, Ws*Ws, C)
        x = window_reverse(attn_windows, self.window_size, Hp, Wp, B)  # (B, Hp, Wp, C)
        x = x[:, :H, :W, :].contiguous()                   # crop padding

        x = shortcut + self.drop_path(x)

        # --- FFN path ---
        x = x + self.drop_path(self.mlp(self.norm2(x)))     # Linear on last dim C

        return x  # (B, H, W, C)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    B, C, H, W = 2, 96, 56, 56
    num_heads = 3  # head_dim = 32
    ffn_dim = C * 4  # 384
    window_size = 7

    block = WindowAttentionBlock(
        embed_dim=C, num_heads=num_heads, ffn_dim=ffn_dim,
        window_size=window_size, drop_path_rate=0.1,
    ).eval()

    x = torch.randn(B, H, W, C)
    with torch.no_grad():
        y = block(x)

    assert y.shape == (B, H, W, C), f"Expected {(B, H, W, C)}, got {y.shape}"
    print(f"[Pass] Shape: {y.shape}")

    # Test with non-divisible spatial size (padding required)
    H2, W2 = 55, 53
    x2 = torch.randn(B, H2, W2, C)
    with torch.no_grad():
        y2 = block(x2)

    assert y2.shape == (B, H2, W2, C), f"Expected {(B, H2, W2, C)}, got {y2.shape}"
    print(f"[Pass] Non-divisible shape: {y2.shape}")

    # Parameter count
    total_params = sum(p.numel() for p in block.parameters())
    print(f"[Info] Total params: {total_params:,}")

    print(">>> All WindowAttentionBlock tests passed!")
