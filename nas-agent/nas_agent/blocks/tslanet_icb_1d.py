from typing import Any

import torch
import torch.nn as nn

from .primitive_blocks import ElasticConv1d, ElasticLayerNorm


class ElasticTSLANetICB1DMixer(nn.Module):
    """TSLANet Interactive Convolution Block for native BLC sequences.

    This follows the official ICB dataflow:
        conv1 = 1x1 Conv1d(C -> hidden)
        conv2 = 3x1 Conv1d(C -> hidden)
        out = 1x1 Conv1d(hidden -> C)(conv1 * gelu(conv2) + conv2 * gelu(conv1))

    The external block layout remains `[B, L, C]`; Conv1d is applied on the
    sequence axis after a local `[B, L, C] <-> [B, C, L]` transpose.
    """

    def __init__(
        self,
        *,
        super_ffn_dim: int,
        global_dim: int,
    ):
        super().__init__()
        if super_ffn_dim <= 0:
            raise ValueError("super_ffn_dim must be positive.")
        if global_dim <= 0:
            raise ValueError("global_dim must be positive.")

        self.global_dim = global_dim
        self.super_ffn_dim = super_ffn_dim
        self.conv1 = ElasticConv1d(
            super_in_channels=global_dim,
            super_out_channels=super_ffn_dim,
            kernel_size=1,
            bias=True,
        )
        self.conv2 = ElasticConv1d(
            super_in_channels=global_dim,
            super_out_channels=super_ffn_dim,
            kernel_size=3,
            padding=1,
            bias=True,
        )
        self.conv3 = ElasticConv1d(
            super_in_channels=super_ffn_dim,
            super_out_channels=global_dim,
            kernel_size=1,
            bias=True,
        )
        self.act = nn.GELU()
        self.sample_ffn_dim = super_ffn_dim

    def set_sample_config(self, *, sample_ffn_dim: int):
        if sample_ffn_dim <= 0 or sample_ffn_dim > self.super_ffn_dim:
            raise ValueError("sample_ffn_dim must be in [1, super_ffn_dim].")
        self.sample_ffn_dim = sample_ffn_dim
        self.conv1.set_sample_config(
            sample_in_channels=self.global_dim,
            sample_out_channels=sample_ffn_dim,
            sample_groups=1,
        )
        self.conv2.set_sample_config(
            sample_in_channels=self.global_dim,
            sample_out_channels=sample_ffn_dim,
            sample_groups=1,
        )
        self.conv3.set_sample_config(
            sample_in_channels=sample_ffn_dim,
            sample_out_channels=self.global_dim,
            sample_groups=1,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x.transpose(1, 2)
        x1 = self.conv1(x)
        x1_act = self.act(x1)
        x2 = self.conv2(x)
        x2_act = self.act(x2)
        out = self.conv3(x1 * x2_act + x2 * x1_act)
        return out.transpose(1, 2)

    def get_active_subnet(self) -> nn.Module:
        class TSLANetICB1DMixer(nn.Module):
            def __init__(self, conv1, conv2, conv3, act):
                super().__init__()
                self.conv1 = conv1
                self.conv2 = conv2
                self.conv3 = conv3
                self.act = act

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                x = x.transpose(1, 2)
                x1 = self.conv1(x)
                x1_act = self.act(x1)
                x2 = self.conv2(x)
                x2_act = self.act(x2)
                out = self.conv3(x1 * x2_act + x2 * x1_act)
                return out.transpose(1, 2)

        return TSLANetICB1DMixer(
            self.conv1.get_active_subnet(),
            self.conv2.get_active_subnet(),
            self.conv3.get_active_subnet(),
            nn.GELU(),
        )

    @property
    def elastic_num_params(self):
        return (
            self.conv1.elastic_num_params
            + self.conv2.elastic_num_params
            + self.conv3.elastic_num_params
        )


class ElasticTSLANetICB1DBlock(nn.Module):
    """Pre-LayerNorm TSLANet ICB block with a residual BLC interface."""

    def __init__(
        self,
        *,
        super_ffn_dim: int,
        global_dim: int,
    ):
        super().__init__()
        self.global_dim = global_dim
        self.super_ffn_dim = super_ffn_dim
        self.norm = ElasticLayerNorm(super_hidden_size=global_dim)
        self.icb = ElasticTSLANetICB1DMixer(
            super_ffn_dim=super_ffn_dim,
            global_dim=global_dim,
        )
        self.sample_ffn_dim = super_ffn_dim

    def set_sample_config(self, *, sample_ffn_dim: int):
        if sample_ffn_dim <= 0 or sample_ffn_dim > self.super_ffn_dim:
            raise ValueError("sample_ffn_dim must be in [1, super_ffn_dim].")
        self.sample_ffn_dim = sample_ffn_dim
        self.norm.set_sample_config(sample_hidden_size=self.global_dim)
        self.icb.set_sample_config(sample_ffn_dim=sample_ffn_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + self.icb(self.norm(x))

    def get_active_subnet(self) -> nn.Module:
        class TSLANetICB1DBlock(nn.Module):
            def __init__(self, norm, icb):
                super().__init__()
                self.norm = norm
                self.icb = icb

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                return x + self.icb(self.norm(x))

        return TSLANetICB1DBlock(
            self.norm.get_active_subnet(),
            self.icb.get_active_subnet(),
        )

    @property
    def elastic_num_params(self):
        return self.norm.elastic_num_params + self.icb.elastic_num_params


def is_valid_tslanet_icb_1d_block(config: dict[str, Any]) -> bool:
    ffn_dim = config.get("ffn_dim")
    return isinstance(ffn_dim, int) and ffn_dim > 0


if __name__ == "__main__":
    B, L, C = 2, 64, 128
    torch.manual_seed(0)
    super_block = ElasticTSLANetICB1DBlock(
        super_ffn_dim=256,
        global_dim=C,
    ).eval()

    test_configs = [{"ffn_dim": 128}, {"ffn_dim": 256}]
    for cfg in test_configs:
        super_block.set_sample_config(sample_ffn_dim=cfg["ffn_dim"])
        x = torch.randn(B, L, C)
        with torch.no_grad():
            y_super = super_block(x)
            subnet = super_block.get_active_subnet().eval()
            y_sub = subnet(x)
        diff = (y_super - y_sub).abs().max().item()
        print(f"  [Pass] Config={cfg}, Consistency Diff={diff:.2e}")
        assert diff < 1e-6
        assert y_super.shape == (B, L, C)
