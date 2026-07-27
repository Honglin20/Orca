from typing import Any

import torch
import torch.nn as nn

from .primitive_blocks import ElasticLinear, ElasticRMSNorm


class RandomSynthesizerCore(nn.Module):
    """Materialized active random-synthesizer attention core."""

    def __init__(
        self,
        value_proj: nn.Linear,
        out_proj: nn.Linear,
        attention: torch.Tensor,
        max_seq_len: int,
    ):
        super().__init__()
        self.value_proj = value_proj
        self.out_proj = out_proj
        self.attention = nn.Parameter(attention.detach().clone())
        self.max_seq_len = max_seq_len

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        _, length, _ = x.shape
        if length > self.max_seq_len:
            raise ValueError(
                f"Sequence length {length} exceeds max_seq_len "
                f"{self.max_seq_len}; the length is the total token count, "
                "including any cls token."
            )
        attention = self.attention[:, :length, :length]
        value = self.value_proj(x)
        return self.out_proj(torch.matmul(attention, value))


class ElasticRandomSynthesizerCore(nn.Module):
    """Elastic Random Synthesizer attention core.

    Replaces dot-product attention with a learned per-token mixing matrix.
    Value projection: global_dim -> D (D = num_heads * head_dim).
    Output projection: D -> global_dim.
    The mixing matrix has shape [1, max_seq_len, max_seq_len] and is
    sliced to [:, :L, :L] at runtime; it is independent of head_dim
    and num_heads. This supports variable total token lengths only up to
    max_seq_len. The total length includes any cls token if present; cls
    tokens are not required or handled specially.
    """

    def __init__(
        self,
        *,
        super_num_heads: int,
        global_dim: int,
        head_dim: int,
        max_seq_len: int = 256,
    ):
        super().__init__()
        self.global_dim = global_dim
        self.head_dim = head_dim
        self.super_num_heads = super_num_heads
        self.super_attn_dim = head_dim * super_num_heads
        self.max_seq_len = max_seq_len

        # Value projection: global_dim -> D
        self.value_proj = ElasticLinear(
            super_in_dim=self.global_dim, super_out_dim=self.super_attn_dim
        )
        # Output projection: D -> global_dim
        self.out_proj = ElasticLinear(
            super_in_dim=self.super_attn_dim, super_out_dim=self.global_dim
        )
        # Learned per-token mixing matrix (independent of head config)
        self.attention = nn.Parameter(torch.empty(1, max_seq_len, max_seq_len))
        nn.init.xavier_uniform_(self.attention)

        # Runtime defaults
        self.sample_num_heads = super_num_heads
        self.sample_attn_dim = self.super_attn_dim

    def set_sample_config(self, *, sample_num_heads: int):
        self.sample_num_heads = sample_num_heads
        self.sample_attn_dim = self.head_dim * self.sample_num_heads
        self.value_proj.set_sample_config(
            sample_in_dim=self.global_dim,
            sample_out_dim=self.sample_attn_dim,
        )
        self.out_proj.set_sample_config(
            sample_in_dim=self.sample_attn_dim,
            sample_out_dim=self.global_dim,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        _, length, _ = x.shape
        if length > self.max_seq_len:
            raise ValueError(
                f"Sequence length {length} exceeds max_seq_len "
                f"{self.max_seq_len}; "
                "the length is the total token count, including any cls token."
            )
        attn = self.attention[:, :length, :length]
        value = self.value_proj(x)
        out = torch.matmul(attn, value)
        return self.out_proj(out)

    def get_active_subnet(self) -> nn.Module:
        return RandomSynthesizerCore(
            self.value_proj.get_active_subnet(),
            self.out_proj.get_active_subnet(),
            self.attention,
            self.max_seq_len,
        )

    @property
    def elastic_num_params(self):
        return (
            self.value_proj.elastic_num_params
            + self.out_proj.elastic_num_params
            + self.max_seq_len * self.max_seq_len
        )


class ElasticRandomSynthesizerBlock(nn.Module):
    """Random Synthesizer Block with pre-RMSNorm ordering.

    All operations use global_dim as external I/O dimension.
    Random Synthesizer attention projects global_dim -> D internally
    (D = num_heads * head_dim) via a value projection, mixes tokens
    with a learned [L, L] matrix, then projects D -> global_dim.
    FFN projects global_dim -> ffn_dim -> global_dim.
    The learned token mixer is allocated at max_seq_len and sliced to
    runtime L, so inputs may vary only while L <= max_seq_len. The length
    includes any cls token if present; the block itself is cls-token agnostic.

    Structure:
    x [B, N, global_dim]
    -> RMSNorm -> RandomSynthesizerAttn(global_dim->D->global_dim) -> residual add
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
        max_seq_len: int = 256,
    ):
        super().__init__()
        self.global_dim = global_dim
        self.head_dim = head_dim
        self.super_num_heads = super_num_heads
        self.super_ffn_dim = super_ffn_dim

        # Runtime State
        self.sample_num_heads = self.super_num_heads
        self.sample_ffn_dim = self.super_ffn_dim

        # Attention Path
        self.attn = ElasticRandomSynthesizerCore(
            super_num_heads=super_num_heads,
            global_dim=self.global_dim,
            head_dim=head_dim,
            max_seq_len=max_seq_len,
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

    def set_sample_config(self, *, sample_num_heads: int, sample_ffn_dim: int):
        self.sample_num_heads = sample_num_heads
        self.sample_ffn_dim = sample_ffn_dim

        self.attn.set_sample_config(sample_num_heads=sample_num_heads)
        self.norm1.set_sample_config(sample_hidden_size=self.global_dim)
        self.norm2.set_sample_config(sample_hidden_size=self.global_dim)

        self.mlp_fc1.set_sample_config(
            sample_in_dim=self.global_dim,
            sample_out_dim=self.sample_ffn_dim,
        )
        self.mlp_fc2.set_sample_config(
            sample_in_dim=self.sample_ffn_dim,
            sample_out_dim=self.global_dim,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.attn(self.norm1(x))
        x = x + self.mlp_fc2(self.mlp_act(self.mlp_fc1(self.norm2(x))))
        return x

    def get_active_subnet(self) -> nn.Module:
        class RandomSynthesizerBlock(nn.Module):
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

        return RandomSynthesizerBlock(
            self.norm1.get_active_subnet(),
            self.attn.get_active_subnet(),
            self.norm2.get_active_subnet(),
            nn.Sequential(self.mlp_fc1.get_active_subnet(), self.mlp_act, self.mlp_fc2.get_active_subnet()),
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


def is_valid_random_synthesizer_block(config: dict[str, Any]) -> bool:
    num_heads = config.get('num_heads')
    ffn_dim = config.get('ffn_dim')
    return isinstance(num_heads, int) and num_heads > 0 and isinstance(ffn_dim, int) and ffn_dim > 0


if __name__ == "__main__":
    B, L, C = 2, 64, 128
    # 1) Initialize Supernet
    super_block = ElasticRandomSynthesizerBlock(
        super_num_heads=4,
        super_ffn_dim=256,
        global_dim=C,
        head_dim=16,
    ).eval()

    print(f"[Init] RandomSynthesizerBlock Global={C}, HeadDim=16, MaxHeads=4")

    # 2) Verify Subnet Consistency
    torch.manual_seed(42)
    test_configs = [
        {"num_heads": 2, "ffn_dim": 64},  # attn_dim = 16*2 = 32
        {"num_heads": 4, "ffn_dim": 256},  # attn_dim = 16*4 = 64
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

    print(">>> All RandomSynthesizerBlock Tests Passed!")
