import math
from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F

from .primitive_blocks import ElasticConv1d, ElasticLayerNorm, ElasticLinear


class ElasticITransformerFullAttention(nn.Module):
    """Official iTransformer FullAttention dataflow on BLC-projected heads."""

    def __init__(
        self,
        *,
        mask_flag: bool = False,
        scale: float | None = None,
        output_attention: bool = False,
    ):
        super().__init__()
        self.mask_flag = mask_flag
        self.scale = scale
        self.output_attention = output_attention

    def forward(
        self,
        queries: torch.Tensor,
        keys: torch.Tensor,
        values: torch.Tensor,
        attn_mask: torch.Tensor | None = None,
    ) -> tuple[torch.Tensor, torch.Tensor | None]:
        batch, length, _, head_dim = queries.shape
        scale = self.scale or 1.0 / math.sqrt(head_dim)
        scores = torch.einsum("blhe,bshe->bhls", queries, keys)
        if self.mask_flag and attn_mask is not None:
            scores = scores.masked_fill(attn_mask, float("-inf"))
        attn = torch.softmax(scale * scores, dim=-1)
        out = torch.einsum("bhls,bshd->blhd", attn, values)
        if self.output_attention:
            return out.contiguous(), attn
        return out.contiguous(), None

    @property
    def elastic_num_params(self):
        return 0


class ITransformerAttentionLayer(nn.Module):
    """Materialized active iTransformer attention layer."""

    def __init__(
        self,
        query_projection: nn.Linear,
        key_projection: nn.Linear,
        value_projection: nn.Linear,
        out_projection: nn.Linear,
        inner_attention: nn.Module,
        *,
        num_heads: int,
        head_dim: int,
    ):
        super().__init__()
        self.query_projection = query_projection
        self.key_projection = key_projection
        self.value_projection = value_projection
        self.out_projection = out_projection
        self.inner_attention = inner_attention
        self.num_heads = num_heads
        self.head_dim = head_dim
        self.attn_dim = num_heads * head_dim

    def forward(
        self,
        queries: torch.Tensor,
        keys: torch.Tensor,
        values: torch.Tensor,
        attn_mask: torch.Tensor | None = None,
    ) -> tuple[torch.Tensor, torch.Tensor | None]:
        batch, query_len, _ = queries.shape
        _, key_len, _ = keys.shape
        q = self.query_projection(queries).view(
            batch, query_len, self.num_heads, self.head_dim
        )
        k = self.key_projection(keys).view(
            batch, key_len, self.num_heads, self.head_dim
        )
        v = self.value_projection(values).view(
            batch, key_len, self.num_heads, self.head_dim
        )
        out, attention = self.inner_attention(q, k, v, attn_mask)
        out = out.view(batch, query_len, self.attn_dim)
        return self.out_projection(out), attention


class ElasticITransformerAttentionLayer(nn.Module):
    """Official iTransformer AttentionLayer with elastic projections."""

    def __init__(
        self,
        *,
        super_num_heads: int,
        global_dim: int,
        head_dim: int,
        output_attention: bool = False,
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
        self.inner_attention = ElasticITransformerFullAttention(
            mask_flag=False,
            output_attention=output_attention,
        )
        self.query_projection = ElasticLinear(
            super_in_dim=global_dim,
            super_out_dim=self.super_attn_dim,
        )
        self.key_projection = ElasticLinear(
            super_in_dim=global_dim,
            super_out_dim=self.super_attn_dim,
        )
        self.value_projection = ElasticLinear(
            super_in_dim=global_dim,
            super_out_dim=self.super_attn_dim,
        )
        self.out_projection = ElasticLinear(
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
        self.query_projection.set_sample_config(
            sample_in_dim=self.global_dim,
            sample_out_dim=self.sample_attn_dim,
        )
        self.key_projection.set_sample_config(
            sample_in_dim=self.global_dim,
            sample_out_dim=self.sample_attn_dim,
        )
        self.value_projection.set_sample_config(
            sample_in_dim=self.global_dim,
            sample_out_dim=self.sample_attn_dim,
        )
        self.out_projection.set_sample_config(
            sample_in_dim=self.sample_attn_dim,
            sample_out_dim=self.global_dim,
        )

    def forward(
        self,
        queries: torch.Tensor,
        keys: torch.Tensor,
        values: torch.Tensor,
        attn_mask: torch.Tensor | None = None,
    ) -> tuple[torch.Tensor, torch.Tensor | None]:
        batch, query_len, _ = queries.shape
        _, key_len, _ = keys.shape
        heads = self.sample_num_heads
        q = self.query_projection(queries).view(batch, query_len, heads, self.head_dim)
        k = self.key_projection(keys).view(batch, key_len, heads, self.head_dim)
        v = self.value_projection(values).view(batch, key_len, heads, self.head_dim)
        out, attn = self.inner_attention(q, k, v, attn_mask)
        out = out.view(batch, query_len, self.sample_attn_dim)
        return self.out_projection(out), attn

    def get_active_subnet(self) -> nn.Module:
        return ITransformerAttentionLayer(
            self.query_projection.get_active_subnet(),
            self.key_projection.get_active_subnet(),
            self.value_projection.get_active_subnet(),
            self.out_projection.get_active_subnet(),
            ElasticITransformerFullAttention(
                mask_flag=self.inner_attention.mask_flag,
                scale=self.inner_attention.scale,
                output_attention=self.inner_attention.output_attention,
            ),
            num_heads=self.sample_num_heads,
            head_dim=self.head_dim,
        )

    @property
    def elastic_num_params(self):
        return (
            self.query_projection.elastic_num_params
            + self.key_projection.elastic_num_params
            + self.value_projection.elastic_num_params
            + self.out_projection.elastic_num_params
        )


class ElasticITransformerBlock(nn.Module):
    """iTransformer-style encoder block with pre-LayerNorm residual branches."""

    def __init__(
        self,
        *,
        super_num_heads: int,
        super_ffn_dim: int,
        global_dim: int,
        head_dim: int,
        activation: str = "gelu",
    ):
        super().__init__()
        if super_ffn_dim <= 0:
            raise ValueError("super_ffn_dim must be positive.")
        self.global_dim = global_dim
        self.super_num_heads = super_num_heads
        self.super_ffn_dim = super_ffn_dim
        self.attention = ElasticITransformerAttentionLayer(
            super_num_heads=super_num_heads,
            global_dim=global_dim,
            head_dim=head_dim,
        )
        self.conv1 = ElasticConv1d(
            super_in_channels=global_dim,
            super_out_channels=super_ffn_dim,
            kernel_size=1,
        )
        self.conv2 = ElasticConv1d(
            super_in_channels=super_ffn_dim,
            super_out_channels=global_dim,
            kernel_size=1,
        )
        self.norm1 = ElasticLayerNorm(super_hidden_size=global_dim)
        self.norm2 = ElasticLayerNorm(super_hidden_size=global_dim)
        self.activation = F.relu if activation == "relu" else F.gelu
        self.sample_num_heads = super_num_heads
        self.sample_ffn_dim = super_ffn_dim

    def set_sample_config(self, *, sample_num_heads: int, sample_ffn_dim: int):
        if sample_ffn_dim <= 0 or sample_ffn_dim > self.super_ffn_dim:
            raise ValueError("sample_ffn_dim must be in [1, super_ffn_dim].")
        self.sample_num_heads = sample_num_heads
        self.sample_ffn_dim = sample_ffn_dim
        self.attention.set_sample_config(sample_num_heads=sample_num_heads)
        self.conv1.set_sample_config(
            sample_in_channels=self.global_dim,
            sample_out_channels=sample_ffn_dim,
            sample_groups=1,
        )
        self.conv2.set_sample_config(
            sample_in_channels=sample_ffn_dim,
            sample_out_channels=self.global_dim,
            sample_groups=1,
        )
        self.norm1.set_sample_config(sample_hidden_size=self.global_dim)
        self.norm2.set_sample_config(sample_hidden_size=self.global_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        attn_input = self.norm1(x)
        new_x, _ = self.attention(attn_input, attn_input, attn_input, attn_mask=None)
        x = x + new_x
        y = self.norm2(x)
        y = self.activation(self.conv1(y.transpose(-1, 1)))
        y = self.conv2(y).transpose(-1, 1)
        return x + y

    def get_active_subnet(self) -> nn.Module:
        class ITransformerBlock(nn.Module):
            def __init__(self, attention, conv1, conv2, norm1, norm2, activation):
                super().__init__()
                self.attention = attention
                self.conv1 = conv1
                self.conv2 = conv2
                self.norm1 = norm1
                self.norm2 = norm2
                self.activation = activation

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                attn_input = self.norm1(x)
                new_x, _ = self.attention(
                    attn_input, attn_input, attn_input, attn_mask=None
                )
                x = x + new_x
                y = self.norm2(x)
                y = self.activation(self.conv1(y.transpose(-1, 1)))
                y = self.conv2(y).transpose(-1, 1)
                return x + y

        return ITransformerBlock(
            self.attention.get_active_subnet(),
            self.conv1.get_active_subnet(),
            self.conv2.get_active_subnet(),
            self.norm1.get_active_subnet(),
            self.norm2.get_active_subnet(),
            self.activation,
        )

    @property
    def elastic_num_params(self):
        return (
            self.attention.elastic_num_params
            + self.conv1.elastic_num_params
            + self.conv2.elastic_num_params
            + self.norm1.elastic_num_params
            + self.norm2.elastic_num_params
        )


def is_valid_itransformer_block(config: dict[str, Any]) -> bool:
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
    super_block = ElasticITransformerBlock(
        super_num_heads=4,
        super_ffn_dim=256,
        global_dim=C,
        head_dim=32,
        activation="gelu",
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
