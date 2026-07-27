from typing import Any

import torch
import torch.nn as nn

from .primitive_blocks import ElasticLinear, ElasticMHSAQKVProjector, ElasticRMSNorm


class ReluAttentionCore(nn.Module):
    """Materialized active ReLU attention core."""

    def __init__(
        self,
        qkv_proj: nn.Module,
        out_proj: nn.Linear,
        *,
        num_heads: int,
        head_dim: int,
    ):
        super().__init__()
        self.qkv_proj = qkv_proj
        self.out_proj = out_proj
        self.num_heads = num_heads
        self.head_dim = head_dim
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        batch, length, _ = x.shape
        q, k, v = self.qkv_proj(x)
        attention = self.relu(
            (q @ k.transpose(-2, -1)) * (self.head_dim**-0.5)
        )
        attention = attention / max(attention.size(-1), 1)
        out = (attention @ v).transpose(1, 2).contiguous().view(
            batch, length, -1
        )
        return self.out_proj(out)


class ElasticReluAttentionCore(nn.Module):
    """Elastic ReLU-on-logits attention core.

    Standard QKV attention but applies ReLU to the scaled attention logits
    (instead of softmax) and divides by sequence length for normalization.

    Projections: global_dim -> attn_dim (attn_dim = num_heads * head_dim)
    for Q/K/V, and attn_dim -> global_dim for output projection.
    """

    def __init__(
        self,
        *,
        super_num_heads: int,
        global_dim: int,
        head_dim: int,
    ):
        super().__init__()
        self.global_dim = global_dim
        self.head_dim = head_dim
        self.super_num_heads = super_num_heads
        self.super_attn_dim = super_num_heads * head_dim

        # Q, K, V projections: global_dim -> attn_dim
        self.qkv_proj = ElasticMHSAQKVProjector(
            super_in_dim=self.global_dim,
            super_out_dim=self.super_attn_dim,
            head_dim=self.head_dim,
        )
        # Output projection: attn_dim -> global_dim
        self.out_proj = ElasticLinear(
            super_in_dim=self.super_attn_dim, super_out_dim=self.global_dim
        )

        self.sample_num_heads = super_num_heads
        self.sample_attn_dim = self.super_attn_dim
        self.relu = nn.ReLU(inplace=True)

    def set_sample_config(self, *, sample_num_heads: int):
        self.sample_num_heads = sample_num_heads
        self.sample_attn_dim = sample_num_heads * self.head_dim
        self.qkv_proj.set_sample_config(
            sample_in_dim=self.global_dim,
            sample_out_dim=self.sample_attn_dim,
        )
        self.out_proj.set_sample_config(
            sample_in_dim=self.sample_attn_dim,
            sample_out_dim=self.global_dim,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        batch, length, _ = x.shape
        d = self.head_dim
        q, k, v = self.qkv_proj(x)
        attn = self.relu((q @ k.transpose(-2, -1)) * (d ** -0.5))
        attn = attn / max(attn.size(-1), 1)
        out = (attn @ v).transpose(1, 2).contiguous().view(batch, length, -1)
        return self.out_proj(out)

    def get_active_subnet(self) -> nn.Module:
        return ReluAttentionCore(
            self.qkv_proj.get_active_subnet(),
            self.out_proj.get_active_subnet(),
            num_heads=self.sample_num_heads,
            head_dim=self.head_dim,
        )

    @property
    def elastic_num_params(self):
        return (
            self.qkv_proj.elastic_num_params
            + self.out_proj.elastic_num_params
        )


class ElasticReluAttentionBlock(nn.Module):
    """ReLU Attention Block with pre-RMSNorm ordering.

    All operations use global_dim as external I/O dimension.
    Attention projects global_dim -> attn_dim internally
    (attn_dim = num_heads * head_dim). FFN projects
    global_dim -> ffn_dim -> global_dim. Uses ReLU on attention logits
    (instead of softmax).

    Structure:
    x [B, N, global_dim]
    -> RMSNorm -> ReluAttn(global_dim->attn_dim->global_dim) -> residual add
    -> RMSNorm -> FFN(global_dim->ffn_dim->global_dim) -> residual add
    -> output [B, N, global_dim]
    """

    def __init__(
        self,
        *,
        super_num_heads: int,
        super_ffn_dim: int,
        global_dim: int,
        head_dim: int,
    ):
        super().__init__()
        self.global_dim = global_dim
        self.head_dim = head_dim
        self.super_num_heads = super_num_heads
        self.super_ffn_dim = super_ffn_dim

        # Attention Path
        self.attn = ElasticReluAttentionCore(
            super_num_heads=super_num_heads,
            global_dim=self.global_dim,
            head_dim=head_dim,
        )
        self.norm1 = ElasticRMSNorm(super_hidden_size=self.global_dim)

        # FFN Path
        self.mlp_fc1 = ElasticLinear(
            super_in_dim=self.global_dim, super_out_dim=self.super_ffn_dim
        )
        self.mlp_act = nn.ReLU(inplace=True)
        self.mlp_fc2 = ElasticLinear(
            super_in_dim=self.super_ffn_dim, super_out_dim=self.global_dim
        )
        self.norm2 = ElasticRMSNorm(super_hidden_size=self.global_dim)

        self.sample_num_heads = super_num_heads
        self.sample_ffn_dim = super_ffn_dim

    def set_sample_config(self, *, sample_num_heads: int, sample_ffn_dim: int):
        self.sample_num_heads = sample_num_heads
        self.sample_ffn_dim = sample_ffn_dim

        self.attn.set_sample_config(sample_num_heads=sample_num_heads)
        self.norm1.set_sample_config(sample_hidden_size=self.global_dim)
        self.mlp_fc1.set_sample_config(
            sample_in_dim=self.global_dim,
            sample_out_dim=self.sample_ffn_dim,
        )
        self.mlp_fc2.set_sample_config(
            sample_in_dim=self.sample_ffn_dim,
            sample_out_dim=self.global_dim,
        )
        self.norm2.set_sample_config(sample_hidden_size=self.global_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.attn(self.norm1(x))
        x = x + self.mlp_fc2(self.mlp_act(self.mlp_fc1(self.norm2(x))))
        return x

    def get_active_subnet(self) -> nn.Module:
        class ReluAttentionBlock(nn.Module):
            def __init__(self, norm1, attn, norm2, mlp):
                super().__init__()
                self.norm1 = norm1
                self.attn = attn
                self.norm2 = norm2
                self.mlp = mlp

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                x = x + self.attn(self.norm1(x))
                x = x + self.mlp(self.norm2(x))
                return x

        return ReluAttentionBlock(
            self.norm1.get_active_subnet(),
            self.attn.get_active_subnet(),
            self.norm2.get_active_subnet(),
            nn.Sequential(
                self.mlp_fc1.get_active_subnet(),
                self.mlp_act,
                self.mlp_fc2.get_active_subnet(),
            ),
        )

    @property
    def elastic_num_params(self):
        return (
            self.attn.elastic_num_params
            + self.norm1.elastic_num_params
            + self.mlp_fc1.elastic_num_params
            + self.mlp_fc2.elastic_num_params
            + self.norm2.elastic_num_params
        )


def is_valid_relu_attention_block(config: dict[str, Any]) -> bool:
    num_heads = config.get('num_heads')
    ffn_dim = config.get('ffn_dim')
    return isinstance(num_heads, int) and num_heads > 0 and isinstance(ffn_dim, int) and ffn_dim > 0


if __name__ == "__main__":
    B, L, C = 2, 64, 128

    # 1) Initialize Supernet
    super_block = ElasticReluAttentionBlock(
        super_num_heads=4,
        super_ffn_dim=256,
        global_dim=C,
        head_dim=16,
    ).eval()

    print(f"[Init] ReluAttentionBlock Global={C}, HeadDim=16, MaxHeads=4")

    # 2) Verify Subnet Consistency
    torch.manual_seed(42)
    test_configs = [
        {"num_heads": 2, "ffn_dim": 64},  # attn_dim = head_dim*num_heads
        {"num_heads": 4, "ffn_dim": 256},  # attn_dim = head_dim*num_heads
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

    assert y_super.shape == (B, L, C), f"Expected {(B, L, C)}, got {y_super.shape}"

    # 3) Verify Parameter Count
    super_block.set_sample_config(sample_num_heads=2, sample_ffn_dim=128)
    p_active = super_block.elastic_num_params
    print(f"[Params] Active params for num_heads=2, ffn=128: {p_active}")

    print(">>> All ReluAttentionBlock Tests Passed!")
