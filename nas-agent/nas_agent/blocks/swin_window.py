from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F

from .primitive_blocks import ElasticLayerNorm, ElasticLinear, ElasticMHSAQKVProjector


def _window_partition(x: torch.Tensor, window_size: int):
    b, h, w, c = x.shape
    if h % window_size != 0 or w % window_size != 0:
        raise ValueError("padded H and W must be divisible by window_size.")
    x = x.view(b, h // window_size, window_size, w // window_size, window_size, c)
    return (
        x.permute(0, 1, 3, 2, 4, 5).contiguous().view(-1, window_size * window_size, c)
    )


def _window_reverse(windows: torch.Tensor, window_size: int, h: int, w: int, b: int):
    x = windows.view(
        b, h // window_size, w // window_size, window_size, window_size, -1
    )
    return x.permute(0, 1, 3, 2, 4, 5).contiguous().view(b, h, w, -1)


def _pad_to_window_size(
    x: torch.Tensor, window_size: int
) -> tuple[torch.Tensor, int, int]:
    _, h, w, _ = x.shape
    pad_h = (window_size - h % window_size) % window_size
    pad_w = (window_size - w % window_size) % window_size
    if pad_h == 0 and pad_w == 0:
        return x, h, w
    x = F.pad(x.permute(0, 3, 1, 2), (0, pad_w, 0, pad_h))
    x = x.permute(0, 2, 3, 1).contiguous()
    return x, h + pad_h, w + pad_w


def _relative_position_index(
    window_size: int, table_window_size: int, device: torch.device
) -> torch.Tensor:
    coords_h = torch.arange(window_size, device=device)
    coords_w = torch.arange(window_size, device=device)
    coords = torch.stack(torch.meshgrid(coords_h, coords_w, indexing="ij"))
    coords_flatten = torch.flatten(coords, 1)
    relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]
    relative_coords = relative_coords.permute(1, 2, 0).contiguous()
    relative_coords[:, :, 0] += table_window_size - 1
    relative_coords[:, :, 1] += table_window_size - 1
    relative_coords[:, :, 0] *= 2 * table_window_size - 1
    return relative_coords.sum(-1)


def _slice_relative_position_bias_table(
    table: torch.Tensor,
    *,
    table_window_size: int,
    sample_window_size: int,
    sample_num_heads: int,
) -> torch.Tensor:
    if sample_window_size == table_window_size:
        return table[:, :sample_num_heads].detach().clone()

    offsets = torch.arange(
        -(sample_window_size - 1),
        sample_window_size,
        device=table.device,
    )
    rel_h, rel_w = torch.meshgrid(offsets, offsets, indexing="ij")
    source_index = (rel_h + table_window_size - 1) * (2 * table_window_size - 1)
    source_index = source_index + rel_w + table_window_size - 1
    return table[source_index.reshape(-1), :sample_num_heads].detach().clone()


def _shifted_window_attention_mask(
    *,
    h: int,
    w: int,
    window_size: int,
    shift_size: int,
    device: torch.device,
    dtype: torch.dtype,
) -> torch.Tensor:
    h_coords = torch.arange(h, device=device)
    w_coords = torch.arange(w, device=device)
    h_regions = (h_coords >= h - window_size).to(dtype) + (
        h_coords >= h - shift_size
    ).to(dtype)
    w_regions = (w_coords >= w - window_size).to(dtype) + (
        w_coords >= w - shift_size
    ).to(dtype)
    img_mask = h_regions[:, None] * 3 + w_regions[None, :]
    mask_windows = _window_partition(img_mask.view(1, h, w, 1), window_size)
    mask_windows = mask_windows.view(-1, window_size * window_size)
    attn_mask = mask_windows.unsqueeze(1) - mask_windows.unsqueeze(2)
    return attn_mask.masked_fill(attn_mask != 0, -100.0).masked_fill(
        attn_mask == 0, 0.0
    )


class WindowAttentionCore(nn.Module):
    """Materialized active Swin window-attention core."""

    def __init__(
        self,
        qkv_proj: nn.Module,
        proj: nn.Linear,
        relative_position_bias_table: torch.Tensor,
        *,
        num_heads: int,
        head_dim: int,
        window_size: int,
        shifted: bool,
    ):
        super().__init__()
        self.qkv_proj = qkv_proj
        self.proj = proj
        self.relative_position_bias_table = nn.Parameter(
            relative_position_bias_table
        )
        self.num_heads = num_heads
        self.head_dim = head_dim
        self.window_size = window_size
        self.shifted = shifted
        self.attn_dim = num_heads * head_dim

    def _relative_position_bias(
        self, window_size: int, device: torch.device, dtype: torch.dtype
    ) -> torch.Tensor:
        relative_position_index = _relative_position_index(
            window_size, self.window_size, device
        )
        relative_position_bias = self.relative_position_bias_table[
            relative_position_index.view(-1)
        ].view(
            window_size * window_size,
            window_size * window_size,
            self.num_heads,
        )
        return relative_position_bias.permute(2, 0, 1).contiguous().to(dtype=dtype)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        batch, height, width, _ = x.shape
        window_size = min(self.window_size, height, width)
        shift = (
            window_size // 2
            if self.shifted and min(height, width) > window_size
            else 0
        )
        x_2d, padded_h, padded_w = _pad_to_window_size(x, window_size)
        attention_mask = None
        if shift > 0:
            x_2d = torch.roll(x_2d, shifts=(-shift, -shift), dims=(1, 2))
            attention_mask = _shifted_window_attention_mask(
                h=padded_h,
                w=padded_w,
                window_size=window_size,
                shift_size=shift,
                device=x.device,
                dtype=x.dtype,
            )
        windows = _window_partition(x_2d, window_size)
        num_windows_batch, num_tokens, _ = windows.shape
        q, k, v = self.qkv_proj(windows)
        attention = (q @ k.transpose(-2, -1)) * (self.head_dim**-0.5)
        attention = attention + self._relative_position_bias(
            window_size, attention.device, attention.dtype
        ).unsqueeze(0)
        if attention_mask is not None:
            num_windows = attention_mask.shape[0]
            attention = attention.view(
                num_windows_batch // num_windows,
                num_windows,
                self.num_heads,
                num_tokens,
                num_tokens,
            )
            attention = attention + attention_mask.unsqueeze(1).unsqueeze(0)
            attention = attention.view(
                -1, self.num_heads, num_tokens, num_tokens
            )
        attention = F.softmax(attention, dim=-1)
        out = (attention @ v).transpose(1, 2).contiguous().view(
            num_windows_batch, num_tokens, self.attn_dim
        )
        out = self.proj(out)
        x = _window_reverse(
            out, window_size, padded_h, padded_w, batch
        )
        if shift > 0:
            x = torch.roll(x, shifts=(shift, shift), dims=(1, 2))
        return x[:, :height, :width, :].contiguous()


class ElasticWindowAttentionCore(nn.Module):
    """Elastic Swin window attention with relative position bias and optional shift."""

    def __init__(
        self,
        *,
        super_num_heads: int,
        global_dim: int,
        head_dim: int,
        window_size: int = 7,
        super_window_size: int | None = None,
        shifted: bool = False,
    ):
        super().__init__()
        if super_window_size is None:
            super_window_size = window_size
        if super_num_heads <= 0:
            raise ValueError("super_num_heads must be positive.")
        if window_size <= 0:
            raise ValueError("window_size must be positive.")
        if super_window_size <= 0:
            raise ValueError("super_window_size must be positive.")
        if window_size > super_window_size:
            raise ValueError("window_size cannot exceed super_window_size.")
        self.global_dim = global_dim
        self.super_num_heads = super_num_heads
        self.head_dim = head_dim
        self.super_attn_dim = super_num_heads * head_dim
        self.window_size = window_size
        self.super_window_size = super_window_size
        self.sample_window_size = window_size
        self.shifted = shifted
        self.qkv_proj = ElasticMHSAQKVProjector(
            super_in_dim=self.global_dim,
            super_out_dim=self.super_attn_dim,
            head_dim=self.head_dim,
        )
        self.proj = ElasticLinear(
            super_in_dim=self.super_attn_dim,
            super_out_dim=self.global_dim,
        )
        self.relative_position_bias_table = nn.Parameter(
            torch.zeros((2 * self.super_window_size - 1) ** 2, super_num_heads)
        )
        nn.init.trunc_normal_(self.relative_position_bias_table, std=0.02)
        self.sample_num_heads = super_num_heads
        self.sample_attn_dim = self.super_attn_dim

    def set_sample_config(
        self, *, sample_num_heads: int, sample_window_size: int | None = None
    ):
        if sample_num_heads <= 0:
            raise ValueError("sample_num_heads must be positive.")
        if sample_num_heads > self.super_num_heads:
            raise ValueError("sample_num_heads cannot exceed super_num_heads.")
        if sample_window_size is None:
            sample_window_size = self.sample_window_size
        if sample_window_size <= 0:
            raise ValueError("sample_window_size must be positive.")
        if sample_window_size > self.super_window_size:
            raise ValueError("sample_window_size cannot exceed super_window_size.")
        self.sample_num_heads = sample_num_heads
        self.sample_attn_dim = sample_num_heads * self.head_dim
        self.sample_window_size = sample_window_size
        self.qkv_proj.set_sample_config(
            sample_in_dim=self.global_dim,
            sample_out_dim=self.sample_attn_dim,
        )
        self.proj.set_sample_config(
            sample_in_dim=self.sample_attn_dim,
            sample_out_dim=self.global_dim,
        )

    def _relative_position_bias(
        self, window_size: int, device: torch.device, dtype: torch.dtype
    ) -> torch.Tensor:
        relative_position_index = _relative_position_index(
            window_size, self.super_window_size, device
        )
        relative_position_bias = self.relative_position_bias_table[
            relative_position_index.view(-1), : self.sample_num_heads
        ].view(
            window_size * window_size, window_size * window_size, self.sample_num_heads
        )
        return relative_position_bias.permute(2, 0, 1).contiguous().to(dtype=dtype)

    def _attend_windows(
        self, x_windows: torch.Tensor, attn_mask: torch.Tensor | None, window_size: int
    ) -> torch.Tensor:
        b_, n, _ = x_windows.shape
        q, k, v = self.qkv_proj(x_windows)
        attn = (q @ k.transpose(-2, -1)) * (self.head_dim**-0.5)
        attn = attn + self._relative_position_bias(
            window_size, attn.device, attn.dtype
        ).unsqueeze(0)
        if attn_mask is not None:
            n_w = attn_mask.shape[0]
            attn = attn.view(b_ // n_w, n_w, self.sample_num_heads, n, n)
            attn = attn + attn_mask.unsqueeze(1).unsqueeze(0)
            attn = attn.view(-1, self.sample_num_heads, n, n)
        attn = F.softmax(attn, dim=-1)
        out = (attn @ v).transpose(1, 2).contiguous().view(b_, n, self.sample_attn_dim)
        return self.proj(out)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        b, h, w, c = x.shape
        ws = min(self.sample_window_size, h, w)
        shift = ws // 2 if self.shifted and min(h, w) > ws else 0
        x_2d, hp, wp = _pad_to_window_size(x, ws)
        attn_mask = None
        if shift > 0:
            x_2d = torch.roll(x_2d, shifts=(-shift, -shift), dims=(1, 2))
            attn_mask = _shifted_window_attention_mask(
                h=hp,
                w=wp,
                window_size=ws,
                shift_size=shift,
                device=x.device,
                dtype=x.dtype,
            )
        windows = _window_partition(x_2d, ws)
        attn_windows = self._attend_windows(windows, attn_mask, ws)
        x = _window_reverse(attn_windows, ws, hp, wp, b)
        if shift > 0:
            x = torch.roll(x, shifts=(shift, shift), dims=(1, 2))
        x = x[:, :h, :w, :].contiguous()
        return x

    def get_active_subnet(self) -> nn.Module:
        return WindowAttentionCore(
            self.qkv_proj.get_active_subnet(),
            self.proj.get_active_subnet(),
            _slice_relative_position_bias_table(
                self.relative_position_bias_table,
                table_window_size=self.super_window_size,
                sample_window_size=self.sample_window_size,
                sample_num_heads=self.sample_num_heads,
            ),
            num_heads=self.sample_num_heads,
            head_dim=self.head_dim,
            window_size=self.sample_window_size,
            shifted=self.shifted,
        )

    @property
    def elastic_num_params(self):
        relative_bias_params = (
            2 * self.sample_window_size - 1
        ) ** 2 * self.sample_num_heads
        return (
            self.qkv_proj.elastic_num_params
            + self.proj.elastic_num_params
            + relative_bias_params
        )


class ElasticSwinWindowBlock(nn.Module):
    """Two complete Swin blocks stacked inside one NAS block.

    The first sub-block is regular W-MSA + MLP. The second sub-block is
    shifted-window SW-MSA + MLP. This matches the standard Swin alternating
    pattern while keeping this repository's one-candidate-block interface.
    """

    def __init__(
        self,
        *,
        super_num_heads: int,
        super_ffn_dim: int,
        global_dim: int,
        head_dim: int,
        window_size: int = 7,
    ):
        super().__init__()
        self.global_dim = global_dim
        self.head_dim = head_dim
        self.super_ffn_dim = super_ffn_dim

        self.norm1 = ElasticLayerNorm(super_hidden_size=self.global_dim)
        self.attn_window = ElasticWindowAttentionCore(
            super_num_heads=super_num_heads,
            global_dim=self.global_dim,
            head_dim=head_dim,
            window_size=window_size,
            shifted=False,
        )
        self.norm2 = ElasticLayerNorm(super_hidden_size=self.global_dim)
        self.mlp_fc1 = ElasticLinear(
            super_in_dim=self.global_dim,
            super_out_dim=self.super_ffn_dim,
        )
        self.mlp_act = nn.GELU()
        self.mlp_fc2 = ElasticLinear(
            super_in_dim=self.super_ffn_dim,
            super_out_dim=self.global_dim,
        )

        self.norm_shift = ElasticLayerNorm(super_hidden_size=self.global_dim)
        self.attn_shifted = ElasticWindowAttentionCore(
            super_num_heads=super_num_heads,
            global_dim=self.global_dim,
            head_dim=head_dim,
            window_size=window_size,
            shifted=True,
        )
        self.norm_shift_mlp = ElasticLayerNorm(super_hidden_size=self.global_dim)
        self.shift_mlp_fc1 = ElasticLinear(
            super_in_dim=self.global_dim,
            super_out_dim=self.super_ffn_dim,
        )
        self.shift_mlp_act = nn.GELU()
        self.shift_mlp_fc2 = ElasticLinear(
            super_in_dim=self.super_ffn_dim,
            super_out_dim=self.global_dim,
        )

        self.sample_num_heads = super_num_heads
        self.sample_ffn_dim = super_ffn_dim

    def set_sample_config(self, *, sample_num_heads: int, sample_ffn_dim: int):
        if sample_num_heads <= 0:
            raise ValueError("sample_num_heads must be positive.")
        if sample_ffn_dim <= 0:
            raise ValueError("sample_ffn_dim must be positive.")
        if sample_ffn_dim > self.super_ffn_dim:
            raise ValueError("sample_ffn_dim cannot exceed super_ffn_dim.")
        self.sample_num_heads = sample_num_heads
        self.sample_ffn_dim = sample_ffn_dim

        self.norm1.set_sample_config(sample_hidden_size=self.global_dim)
        self.attn_window.set_sample_config(sample_num_heads=sample_num_heads)
        self.norm2.set_sample_config(sample_hidden_size=self.global_dim)
        self.mlp_fc1.set_sample_config(
            sample_in_dim=self.global_dim,
            sample_out_dim=self.sample_ffn_dim,
        )
        self.mlp_fc2.set_sample_config(
            sample_in_dim=self.sample_ffn_dim,
            sample_out_dim=self.global_dim,
        )

        self.norm_shift.set_sample_config(sample_hidden_size=self.global_dim)
        self.attn_shifted.set_sample_config(sample_num_heads=sample_num_heads)
        self.norm_shift_mlp.set_sample_config(sample_hidden_size=self.global_dim)
        self.shift_mlp_fc1.set_sample_config(
            sample_in_dim=self.global_dim,
            sample_out_dim=self.sample_ffn_dim,
        )
        self.shift_mlp_fc2.set_sample_config(
            sample_in_dim=self.sample_ffn_dim,
            sample_out_dim=self.global_dim,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.attn_window(self.norm1(x))
        x = x + self.mlp_fc2(self.mlp_act(self.mlp_fc1(self.norm2(x))))
        x = x + self.attn_shifted(self.norm_shift(x))
        x = x + self.shift_mlp_fc2(
            self.shift_mlp_act(self.shift_mlp_fc1(self.norm_shift_mlp(x)))
        )
        return x

    def get_active_subnet(self) -> nn.Module:
        class SwinWindowBlock(nn.Module):
            def __init__(
                self, norm1, attn_w, norm2, mlp_w, norm_s, attn_s, norm_s_mlp, mlp_s
            ):
                super().__init__()
                self.norm1 = norm1
                self.attn_window = attn_w
                self.norm2 = norm2
                self.mlp_window = mlp_w
                self.norm_shift = norm_s
                self.attn_shifted = attn_s
                self.norm_shift_mlp = norm_s_mlp
                self.mlp_shifted = mlp_s

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                x = x + self.attn_window(self.norm1(x))
                x = x + self.mlp_window(self.norm2(x))
                x = x + self.attn_shifted(self.norm_shift(x))
                x = x + self.mlp_shifted(self.norm_shift_mlp(x))
                return x

        return SwinWindowBlock(
            self.norm1.get_active_subnet(),
            self.attn_window.get_active_subnet(),
            self.norm2.get_active_subnet(),
            nn.Sequential(
                self.mlp_fc1.get_active_subnet(),
                self.mlp_act,
                self.mlp_fc2.get_active_subnet(),
            ),
            self.norm_shift.get_active_subnet(),
            self.attn_shifted.get_active_subnet(),
            self.norm_shift_mlp.get_active_subnet(),
            nn.Sequential(
                self.shift_mlp_fc1.get_active_subnet(),
                self.shift_mlp_act,
                self.shift_mlp_fc2.get_active_subnet(),
            ),
        )

    @property
    def elastic_num_params(self):
        return (
            self.norm1.elastic_num_params
            + self.attn_window.elastic_num_params
            + self.norm2.elastic_num_params
            + self.mlp_fc1.elastic_num_params
            + self.mlp_fc2.elastic_num_params
            + self.norm_shift.elastic_num_params
            + self.attn_shifted.elastic_num_params
            + self.norm_shift_mlp.elastic_num_params
            + self.shift_mlp_fc1.elastic_num_params
            + self.shift_mlp_fc2.elastic_num_params
        )


def is_valid_swin_window_block(config: dict[str, Any]) -> bool:
    num_heads = config.get("num_heads")
    ffn_dim = config.get("ffn_dim")
    return (
        isinstance(num_heads, int)
        and num_heads > 0
        and isinstance(ffn_dim, int)
        and ffn_dim > 0
    )


if __name__ == "__main__":
    B, C = 2, 96
    H, W = 14, 16

    # 1) Initialize Supernet
    super_block = ElasticSwinWindowBlock(
        super_num_heads=4,
        super_ffn_dim=384,
        global_dim=C,
        head_dim=16,
        window_size=7,
    ).eval()

    print(f"[Init] SwinWindowBlock Global={C}, HeadDim=16, MaxHeads=4")

    # 2) Verify Subnet Consistency
    torch.manual_seed(42)
    test_configs = [
        {"num_heads": 2, "ffn_dim": 192},  # attn_dim = 16*2 = 32
        {"num_heads": 4, "ffn_dim": 384},  # attn_dim = 16*4 = 64
    ]

    for cfg in test_configs:
        super_block.set_sample_config(
            sample_num_heads=cfg["num_heads"],
            sample_ffn_dim=cfg["ffn_dim"],
        )
        x = torch.randn(B, H, W, C)
        with torch.no_grad():
            y_super = super_block(x)
            subnet = super_block.get_active_subnet().eval()
            y_sub = subnet(x)
        diff = (y_super - y_sub).abs().max().item()
        print(f"  [Pass] Config={cfg}, Consistency Diff={diff:.2e}")
        assert diff < 1e-6

    assert y_super.shape == (B, H, W, C), (
        f"Expected {(B, H, W, C)}, got {y_super.shape}"
    )

    # 3) Verify Parameter Count
    super_block.set_sample_config(sample_num_heads=2, sample_ffn_dim=192)
    p_active = super_block.elastic_num_params
    print(f"[Params] Active params for num_heads=2, ffn=192: {p_active}")

    print(">>> All SwinWindowBlock Tests Passed!")
