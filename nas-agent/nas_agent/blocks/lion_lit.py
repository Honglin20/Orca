from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F

from .primitive_blocks import ElasticLayerNorm, ElasticLinear


def _elu_shifted(x: torch.Tensor) -> torch.Tensor:
    return F.elu(x) + 1.0


class LionLitAttention(nn.Module):
    """Materialized active LION-LIT attention."""

    def __init__(
        self,
        q_proj: nn.Linear,
        k_proj: nn.Linear,
        v_proj: nn.Linear,
        proj: nn.Linear,
        *,
        num_heads: int,
        head_dim: int,
    ):
        super().__init__()
        self.q_proj = q_proj
        self.k_proj = k_proj
        self.v_proj = v_proj
        self.proj = proj
        self.num_heads = num_heads
        self.head_dim = head_dim
        self.attn_dim = num_heads * head_dim
        self.scale = head_dim**-0.5

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        batch, length, _ = x.shape
        q = self.q_proj(x).view(
            batch, length, self.num_heads, self.head_dim
        ).permute(0, 2, 1, 3)
        k = self.k_proj(x).view(
            batch, length, self.num_heads, self.head_dim
        ).permute(0, 2, 1, 3)
        v = self.v_proj(x).view(
            batch, length, self.num_heads, self.head_dim
        ).permute(0, 2, 1, 3)
        q = _elu_shifted(q)
        k = _elu_shifted(k)
        attention = (q @ k.transpose(-2, -1)) * self.scale
        attention = attention / (
            attention.sum(dim=-1, keepdim=True) + 1e-6
        )
        out = (attention @ v).transpose(1, 2).reshape(
            batch, length, self.attn_dim
        )
        return self.proj(out)


class ElasticLionLitAttention(nn.Module):
    """LION-LIT attention path for native BLC sequences.

    This ports the official LION `mask_type='Lit'`, `format='Attention'`,
    `order='Normal'` path:
        qkv Linear -> head reshape/permute
        q,k = ELU(x)+1
        attn = q @ k^T / sqrt(head_dim)
        attn = attn / sum(attn)
        out = attn @ v -> output projection

    External input/output remains `[B, L, C]`. The internal permutation is the
    official multi-head layout `[B, H, L, D]`, not a 2-D image or BCL conv path.
    """

    def __init__(
        self,
        *,
        super_num_heads: int,
        global_dim: int,
        head_dim: int,
        qkv_bias: bool = False,
    ):
        super().__init__()
        if super_num_heads <= 0:
            raise ValueError("super_num_heads must be positive.")
        if global_dim <= 0:
            raise ValueError("global_dim must be positive.")
        if head_dim <= 0:
            raise ValueError("head_dim must be positive.")

        self.global_dim = global_dim
        self.head_dim = head_dim
        self.super_num_heads = super_num_heads
        self.super_attn_dim = super_num_heads * head_dim
        self.scale = head_dim**-0.5
        self.q_proj = ElasticLinear(
            super_in_dim=global_dim,
            super_out_dim=self.super_attn_dim,
            bias=qkv_bias,
        )
        self.k_proj = ElasticLinear(
            super_in_dim=global_dim,
            super_out_dim=self.super_attn_dim,
            bias=qkv_bias,
        )
        self.v_proj = ElasticLinear(
            super_in_dim=global_dim,
            super_out_dim=self.super_attn_dim,
            bias=qkv_bias,
        )
        self.proj = ElasticLinear(
            super_in_dim=self.super_attn_dim,
            super_out_dim=global_dim,
        )
        self.sample_num_heads = super_num_heads
        self.sample_attn_dim = self.super_attn_dim

    def set_sample_config(self, *, sample_num_heads: int):
        if sample_num_heads <= 0 or sample_num_heads > self.super_num_heads:
            raise ValueError("sample_num_heads must be in [1, super_num_heads].")
        self.sample_num_heads = sample_num_heads
        self.sample_attn_dim = sample_num_heads * self.head_dim
        self.q_proj.set_sample_config(
            sample_in_dim=self.global_dim,
            sample_out_dim=self.sample_attn_dim,
        )
        self.k_proj.set_sample_config(
            sample_in_dim=self.global_dim,
            sample_out_dim=self.sample_attn_dim,
        )
        self.v_proj.set_sample_config(
            sample_in_dim=self.global_dim,
            sample_out_dim=self.sample_attn_dim,
        )
        self.proj.set_sample_config(
            sample_in_dim=self.sample_attn_dim,
            sample_out_dim=self.global_dim,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        batch, length, _ = x.shape
        heads = self.sample_num_heads
        q = self.q_proj(x).view(batch, length, heads, self.head_dim).permute(0, 2, 1, 3)
        k = self.k_proj(x).view(batch, length, heads, self.head_dim).permute(0, 2, 1, 3)
        v = self.v_proj(x).view(batch, length, heads, self.head_dim).permute(0, 2, 1, 3)
        q = _elu_shifted(q)
        k = _elu_shifted(k)
        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn / (attn.sum(dim=-1, keepdim=True) + 1e-6)
        out = (attn @ v).transpose(1, 2).reshape(batch, length, self.sample_attn_dim)
        return self.proj(out)

    def get_active_subnet(self) -> nn.Module:
        return LionLitAttention(
            self.q_proj.get_active_subnet(),
            self.k_proj.get_active_subnet(),
            self.v_proj.get_active_subnet(),
            self.proj.get_active_subnet(),
            num_heads=self.sample_num_heads,
            head_dim=self.head_dim,
        )

    @property
    def elastic_num_params(self):
        return (
            self.q_proj.elastic_num_params
            + self.k_proj.elastic_num_params
            + self.v_proj.elastic_num_params
            + self.proj.elastic_num_params
        )


class ElasticLionLitBlock(nn.Module):
    """Official LION block structure with LION-LIT attention and BLC I/O."""

    def __init__(
        self,
        *,
        super_num_heads: int,
        super_ffn_dim: int,
        global_dim: int,
        head_dim: int,
        qkv_bias: bool = False,
    ):
        super().__init__()
        self.global_dim = global_dim
        self.super_num_heads = super_num_heads
        self.super_ffn_dim = super_ffn_dim
        self.norm1 = ElasticLayerNorm(super_hidden_size=global_dim)
        self.attn = ElasticLionLitAttention(
            super_num_heads=super_num_heads,
            global_dim=global_dim,
            head_dim=head_dim,
            qkv_bias=qkv_bias,
        )
        self.norm2 = ElasticLayerNorm(super_hidden_size=global_dim)
        self.mlp_fc1 = ElasticLinear(
            super_in_dim=global_dim,
            super_out_dim=super_ffn_dim,
        )
        self.mlp_act = nn.GELU()
        self.mlp_fc2 = ElasticLinear(
            super_in_dim=super_ffn_dim,
            super_out_dim=global_dim,
        )
        self.sample_num_heads = super_num_heads
        self.sample_ffn_dim = super_ffn_dim

    def set_sample_config(self, *, sample_num_heads: int, sample_ffn_dim: int):
        if sample_ffn_dim <= 0 or sample_ffn_dim > self.super_ffn_dim:
            raise ValueError("sample_ffn_dim must be in [1, super_ffn_dim].")
        self.sample_num_heads = sample_num_heads
        self.sample_ffn_dim = sample_ffn_dim
        self.norm1.set_sample_config(sample_hidden_size=self.global_dim)
        self.attn.set_sample_config(sample_num_heads=sample_num_heads)
        self.norm2.set_sample_config(sample_hidden_size=self.global_dim)
        self.mlp_fc1.set_sample_config(
            sample_in_dim=self.global_dim,
            sample_out_dim=sample_ffn_dim,
        )
        self.mlp_fc2.set_sample_config(
            sample_in_dim=sample_ffn_dim,
            sample_out_dim=self.global_dim,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.attn(self.norm1(x))
        y = self.mlp_fc2(self.mlp_act(self.mlp_fc1(self.norm2(x))))
        return x + y

    def get_active_subnet(self) -> nn.Module:
        class LionLitBlock(nn.Module):
            def __init__(self, norm1, attn, norm2, mlp):
                super().__init__()
                self.norm1 = norm1
                self.attn = attn
                self.norm2 = norm2
                self.mlp = mlp

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                x = x + self.attn(self.norm1(x))
                return x + self.mlp(self.norm2(x))

        return LionLitBlock(
            self.norm1.get_active_subnet(),
            self.attn.get_active_subnet(),
            self.norm2.get_active_subnet(),
            nn.Sequential(
                self.mlp_fc1.get_active_subnet(),
                nn.GELU(),
                self.mlp_fc2.get_active_subnet(),
            ),
        )

    @property
    def elastic_num_params(self):
        return (
            self.norm1.elastic_num_params
            + self.attn.elastic_num_params
            + self.norm2.elastic_num_params
            + self.mlp_fc1.elastic_num_params
            + self.mlp_fc2.elastic_num_params
        )


def is_valid_lion_lit_block(config: dict[str, Any]) -> bool:
    num_heads = config.get("num_heads")
    ffn_dim = config.get("ffn_dim")
    return (
        isinstance(num_heads, int)
        and num_heads > 0
        and isinstance(ffn_dim, int)
        and ffn_dim > 0
    )


if __name__ == "__main__":
    B, L, C = 2, 64, 128
    torch.manual_seed(0)
    super_block = ElasticLionLitBlock(
        super_num_heads=4,
        super_ffn_dim=256,
        global_dim=C,
        head_dim=32,
    ).eval()

    test_configs = [
        {"num_heads": 2, "ffn_dim": 128},
        {"num_heads": 4, "ffn_dim": 256},
    ]
    for cfg in test_configs:
        super_block.set_sample_config(
            sample_num_heads=cfg["num_heads"],
            sample_ffn_dim=cfg["ffn_dim"],
        )
        x = torch.randn(B, L, C)
        with torch.no_grad():
            y_super = super_block(x)
            subnet = super_block.get_active_subnet().eval()
            y_sub = subnet(x)
        diff = (y_super - y_sub).abs().max().item()
        print(f"  [Pass] Config={cfg}, Consistency Diff={diff:.2e}")
        assert diff < 1e-6
        assert y_super.shape == (B, L, C)
