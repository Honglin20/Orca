from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F

from .primitive_blocks import ElasticLayerNorm, ElasticLinear


class ElasticSOFTSSTARMixer(nn.Module):
    """SOFTS STar Aggregate-Redistribute mixer for native BLC sequences.

    This ports the official SOFTS STAR module's eval-time deterministic path:
        gen1: C -> C
        gen2: C -> core_dim
        softmax-weighted aggregation across tokens
        gen3: C + core_dim -> C
        gen4: C -> C

    The original SOFTS code applies STAR on `[batch, channels, d_series]`.
    Here that maps directly to `[B, L, C]`, where L is the token/series axis
    and C is the per-token feature width.
    """

    def __init__(
        self,
        *,
        super_core_dim: int,
        global_dim: int,
    ):
        super().__init__()
        if super_core_dim <= 0:
            raise ValueError("super_core_dim must be positive.")
        if global_dim <= 0:
            raise ValueError("global_dim must be positive.")

        self.global_dim = global_dim
        self.super_core_dim = super_core_dim
        self.gen1 = ElasticLinear(
            super_in_dim=global_dim,
            super_out_dim=global_dim,
        )
        self.gen2 = ElasticLinear(
            super_in_dim=global_dim,
            super_out_dim=super_core_dim,
        )
        self.gen3 = ElasticLinear(
            super_in_dim=global_dim + super_core_dim,
            super_out_dim=global_dim,
        )
        self.gen4 = ElasticLinear(
            super_in_dim=global_dim,
            super_out_dim=global_dim,
        )
        self.sample_core_dim = super_core_dim

    def set_sample_config(self, *, sample_core_dim: int):
        if sample_core_dim <= 0 or sample_core_dim > self.super_core_dim:
            raise ValueError("sample_core_dim must be in [1, super_core_dim].")
        self.sample_core_dim = sample_core_dim
        self.gen1.set_sample_config(
            sample_in_dim=self.global_dim,
            sample_out_dim=self.global_dim,
        )
        self.gen2.set_sample_config(
            sample_in_dim=self.global_dim,
            sample_out_dim=sample_core_dim,
        )
        self.gen3.set_sample_config(
            sample_in_dim=self.global_dim + sample_core_dim,
            sample_out_dim=self.global_dim,
        )
        self.gen4.set_sample_config(
            sample_in_dim=self.global_dim,
            sample_out_dim=self.global_dim,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        core = F.gelu(self.gen1(x))
        core = self.gen2(core)
        weight = F.softmax(core, dim=1)
        core = torch.sum(core * weight, dim=1, keepdim=True).expand(-1, x.size(1), -1)
        fused = torch.cat([x, core], dim=-1)
        fused = F.gelu(self.gen3(fused))
        return self.gen4(fused)

    def get_active_subnet(self) -> nn.Module:
        class SOFTSSTARMixer(nn.Module):
            def __init__(self, gen1, gen2, gen3, gen4):
                super().__init__()
                self.gen1 = gen1
                self.gen2 = gen2
                self.gen3 = gen3
                self.gen4 = gen4

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                core = F.gelu(self.gen1(x))
                core = self.gen2(core)
                weight = F.softmax(core, dim=1)
                core = torch.sum(core * weight, dim=1, keepdim=True).expand(
                    -1, x.size(1), -1
                )
                fused = torch.cat([x, core], dim=-1)
                fused = F.gelu(self.gen3(fused))
                return self.gen4(fused)

        return SOFTSSTARMixer(
            self.gen1.get_active_subnet(),
            self.gen2.get_active_subnet(),
            self.gen3.get_active_subnet(),
            self.gen4.get_active_subnet(),
        )

    @property
    def elastic_num_params(self):
        return (
            self.gen1.elastic_num_params
            + self.gen2.elastic_num_params
            + self.gen3.elastic_num_params
            + self.gen4.elastic_num_params
        )


class ElasticSOFTSSTARMixerBlock(nn.Module):
    """Pre-LayerNorm SOFTS STAR block with a residual BLC interface."""

    def __init__(
        self,
        *,
        super_core_dim: int,
        global_dim: int,
    ):
        super().__init__()
        self.global_dim = global_dim
        self.super_core_dim = super_core_dim
        self.star = ElasticSOFTSSTARMixer(
            super_core_dim=super_core_dim,
            global_dim=global_dim,
        )
        self.norm = ElasticLayerNorm(super_hidden_size=global_dim)
        self.sample_core_dim = super_core_dim

    def set_sample_config(self, *, sample_core_dim: int):
        if sample_core_dim <= 0 or sample_core_dim > self.super_core_dim:
            raise ValueError("sample_core_dim must be in [1, super_core_dim].")
        self.sample_core_dim = sample_core_dim
        self.star.set_sample_config(sample_core_dim=sample_core_dim)
        self.norm.set_sample_config(sample_hidden_size=self.global_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + self.star(self.norm(x))

    def get_active_subnet(self) -> nn.Module:
        class SOFTSSTARMixerBlock(nn.Module):
            def __init__(self, star, norm):
                super().__init__()
                self.star = star
                self.norm = norm

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                return x + self.star(self.norm(x))

        return SOFTSSTARMixerBlock(
            self.star.get_active_subnet(),
            self.norm.get_active_subnet(),
        )

    @property
    def elastic_num_params(self):
        return self.star.elastic_num_params + self.norm.elastic_num_params


def is_valid_softs_star_mixer_block(config: dict[str, Any]) -> bool:
    core_dim = config.get("core_dim")
    return isinstance(core_dim, int) and core_dim > 0


if __name__ == "__main__":
    B, L, C = 2, 64, 128
    torch.manual_seed(0)
    super_block = ElasticSOFTSSTARMixerBlock(
        super_core_dim=96,
        global_dim=C,
    ).eval()

    test_configs = [{"core_dim": 32}, {"core_dim": 96}]
    for cfg in test_configs:
        super_block.set_sample_config(sample_core_dim=cfg["core_dim"])
        x = torch.randn(B, L, C)
        with torch.no_grad():
            y_super = super_block(x)
            subnet = super_block.get_active_subnet().eval()
            y_sub = subnet(x)
        diff = (y_super - y_sub).abs().max().item()
        print(f"  [Pass] Config={cfg}, Consistency Diff={diff:.2e}")
        assert diff < 1e-6
        assert y_super.shape == (B, L, C)
