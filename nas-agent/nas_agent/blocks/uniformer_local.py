import copy
from typing import Any

import torch
import torch.nn as nn

from .primitive_blocks import (
    ElasticConv2d,
    ElasticGroupNorm2d,
    ElasticLayerNorm,
    ElasticLinear,
)

# -----------------------------------------------------------------------
# Core: Local spatial mixer (depthwise-conv-based relation aggregator)
# Ref: https://arxiv.org/abs/2201.09450 (UniFormer)
# -----------------------------------------------------------------------


class ElasticLocalSpatialMixer(nn.Module):
    """Local spatial mixer implemented via depthwise conv.

    A pointwise projection lifts `global_dim -> sample_mixer_dim`, a
    depthwise convolution mixes spatially, GroupNorm normalizes without
    batch-stat calibration, and `proj` lowers back to `global_dim`.
    Both `mixer_dim` and the kernel size are elastic.
    """

    def __init__(
        self,
        *,
        super_mixer_dim: int,
        global_dim: int,
        candidate_kernel_sizes: tuple[int, ...] = (3, 5),
        channels_per_group: int = 16,
    ):
        super().__init__()
        if not candidate_kernel_sizes:
            raise ValueError("candidate_kernel_sizes must not be empty.")
        if super_mixer_dim <= 0:
            raise ValueError("super_mixer_dim must be positive.")
        self.global_dim = global_dim
        self.super_mixer_dim = super_mixer_dim
        self.channels_per_group = channels_per_group
        self.candidate_kernel_sizes = tuple(sorted(set(candidate_kernel_sizes)))
        max_ks = max(self.candidate_kernel_sizes)

        self.v_proj = ElasticLinear(
            super_in_dim=global_dim, super_out_dim=self.super_mixer_dim
        )
        self.dw_conv = ElasticConv2d(
            super_in_channels=self.super_mixer_dim,
            super_out_channels=self.super_mixer_dim,
            kernel_size=max_ks,
            stride=1,
            padding=max_ks // 2,
            groups=self.super_mixer_dim,
            bias=True,
            candidate_kernel_sizes=self.candidate_kernel_sizes,
        )
        self.mixer_norm = ElasticGroupNorm2d(
            super_num_channels=self.super_mixer_dim,
            channels_per_group=channels_per_group,
        )
        self.out_proj = ElasticLinear(
            super_in_dim=self.super_mixer_dim, super_out_dim=global_dim
        )

        self.sample_mixer_dim = super_mixer_dim
        self.sample_kernel_size = max_ks

    def set_sample_config(self, *, sample_mixer_dim: int, sample_kernel_size: int):
        if sample_mixer_dim <= 0:
            raise ValueError("sample_mixer_dim must be positive.")
        if sample_mixer_dim > self.super_mixer_dim:
            raise ValueError("sample_mixer_dim cannot exceed super_mixer_dim.")
        if sample_kernel_size not in self.candidate_kernel_sizes:
            raise ValueError(f"Unsupported kernel size: {sample_kernel_size}")
        self.sample_mixer_dim = sample_mixer_dim
        self.sample_kernel_size = sample_kernel_size

        self.v_proj.set_sample_config(
            sample_in_dim=self.global_dim, sample_out_dim=self.sample_mixer_dim
        )
        self.dw_conv.set_sample_config(
            sample_in_channels=self.sample_mixer_dim,
            sample_out_channels=self.sample_mixer_dim,
            sample_groups=self.sample_mixer_dim,
            sample_kernel_size=sample_kernel_size,
        )
        self.mixer_norm.set_sample_config(sample_num_channels=self.sample_mixer_dim)
        self.out_proj.set_sample_config(
            sample_in_dim=self.sample_mixer_dim, sample_out_dim=self.global_dim
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        v = self.v_proj(x)  # (B, H, W, sample_mixer_dim)
        v2d = v.permute(0, 3, 1, 2).contiguous()
        a = self.dw_conv(v2d)
        a = self.mixer_norm(a)
        a = a.permute(0, 2, 3, 1).contiguous()
        return self.out_proj(a)

    def get_active_subnet(self) -> nn.Module:
        class LocalSpatialMixer(nn.Module):
            def __init__(self, v_proj, dw_conv, mixer_norm, out_proj):
                super().__init__()
                self.v_proj = v_proj
                self.dw_conv = dw_conv
                self.mixer_norm = mixer_norm
                self.out_proj = out_proj

            def forward(self, x):
                v = self.v_proj(x)
                v2d = v.permute(0, 3, 1, 2).contiguous()
                a = self.mixer_norm(self.dw_conv(v2d))
                a = a.permute(0, 2, 3, 1).contiguous()
                return self.out_proj(a)

        return LocalSpatialMixer(
            self.v_proj.get_active_subnet(),
            self.dw_conv.get_active_subnet(),
            self.mixer_norm.get_active_subnet(),
            self.out_proj.get_active_subnet(),
        )

    @property
    def elastic_num_params(self):
        return (
            self.v_proj.elastic_num_params
            + self.dw_conv.elastic_num_params
            + self.mixer_norm.elastic_num_params
            + self.out_proj.elastic_num_params
        )


# -----------------------------------------------------------------------
# Full Block
# -----------------------------------------------------------------------


class ElasticUniFormerLocalBlock(nn.Module):
    """UniFormer local-mixer block: DPE + local spatial mixer + GELU MLP (pre-LN).

    Structure:
        x = x + DPE(x)                       # 3x3 depthwise conv on global_dim
        x = x + LocalMixer(LN(x))            # DW-conv based local mixing
        x = x + MLP(LN(x))                   # standard channel mixing

    Input: [B, H, W, global_dim].
    """

    def __init__(
        self,
        *,
        super_mixer_dim: int,
        super_ffn_dim: int,
        global_dim: int,
        candidate_kernel_sizes: tuple[int, ...] = (3, 5),
        channels_per_group: int = 16,
    ):
        super().__init__()
        self.global_dim = global_dim
        self.super_mixer_dim = super_mixer_dim
        self.super_ffn_dim = super_ffn_dim

        self.dpe = nn.Conv2d(global_dim, global_dim, 3, padding=1, groups=global_dim)
        self.norm1 = ElasticLayerNorm(super_hidden_size=global_dim)
        self.mixer = ElasticLocalSpatialMixer(
            super_mixer_dim=super_mixer_dim,
            global_dim=global_dim,
            candidate_kernel_sizes=candidate_kernel_sizes,
            channels_per_group=channels_per_group,
        )
        self.norm2 = ElasticLayerNorm(super_hidden_size=global_dim)
        self.mlp_fc1 = ElasticLinear(
            super_in_dim=global_dim, super_out_dim=super_ffn_dim
        )
        self.mlp_act = nn.GELU()
        self.mlp_fc2 = ElasticLinear(
            super_in_dim=super_ffn_dim, super_out_dim=global_dim
        )
        self.sample_mixer_dim = super_mixer_dim
        self.sample_ffn_dim = super_ffn_dim
        self.sample_kernel_size = max(self.mixer.candidate_kernel_sizes)

    def set_sample_config(
        self,
        *,
        sample_mixer_dim: int,
        sample_ffn_dim: int,
        sample_kernel_size: int,
    ):
        self.sample_mixer_dim = sample_mixer_dim
        self.sample_ffn_dim = sample_ffn_dim
        self.sample_kernel_size = sample_kernel_size

        self.norm1.set_sample_config(sample_hidden_size=self.global_dim)
        self.mixer.set_sample_config(
            sample_mixer_dim=sample_mixer_dim,
            sample_kernel_size=sample_kernel_size,
        )
        self.norm2.set_sample_config(sample_hidden_size=self.global_dim)
        self.mlp_fc1.set_sample_config(
            sample_in_dim=self.global_dim, sample_out_dim=self.sample_ffn_dim
        )
        self.mlp_fc2.set_sample_config(
            sample_in_dim=self.sample_ffn_dim, sample_out_dim=self.global_dim
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        dpe_input = x.permute(0, 3, 1, 2).contiguous()
        x = x + self.dpe(dpe_input).permute(0, 2, 3, 1).contiguous()
        x = x + self.mixer(self.norm1(x))
        x = x + self.mlp_fc2(self.mlp_act(self.mlp_fc1(self.norm2(x))))
        return x

    def get_active_subnet(self) -> nn.Module:
        class UniFormerBlock(nn.Module):
            def __init__(self, dpe, norm1, mixer, norm2, mlp):
                super().__init__()
                self.dpe = dpe
                self.norm1 = norm1
                self.mixer = mixer
                self.norm2 = norm2
                self.mlp = mlp

            def forward(self, x):
                dpe_input = x.permute(0, 3, 1, 2).contiguous()
                x = x + self.dpe(dpe_input).permute(0, 2, 3, 1).contiguous()
                x = x + self.mixer(self.norm1(x))
                x = x + self.mlp(self.norm2(x))
                return x

        return UniFormerBlock(
            copy.deepcopy(self.dpe),
            self.norm1.get_active_subnet(),
            self.mixer.get_active_subnet(),
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
            sum(p.numel() for p in self.dpe.parameters())
            + self.norm1.elastic_num_params
            + self.mixer.elastic_num_params
            + self.norm2.elastic_num_params
            + self.mlp_fc1.elastic_num_params
            + self.mlp_fc2.elastic_num_params
        )


def is_valid_uniformer_local_block(
    config: dict[str, Any],
) -> bool:
    mixer_dim = config.get("mixer_dim")
    ffn_dim = config.get("ffn_dim")
    kernel_size = config.get("kernel_size")
    return (
        isinstance(mixer_dim, int)
        and mixer_dim > 0
        and isinstance(ffn_dim, int)
        and ffn_dim > 0
        and isinstance(kernel_size, int)
        and kernel_size > 0
        and kernel_size % 2 == 1
    )


if __name__ == "__main__":
    B, H, W, C = 2, 14, 16, 128

    super_block = ElasticUniFormerLocalBlock(
        global_dim=C,
        super_mixer_dim=64,
        super_ffn_dim=256,
        candidate_kernel_sizes=(3, 5),
    ).eval()

    print(
        f"[Init] UniFormerLocalMixerBlock  global={C}, max_mixer_dim=64, kernels=(3,5)"
    )

    torch.manual_seed(0)
    test_configs = [
        {"mixer_dim": 32, "ffn_dim": 64, "kernel_size": 3},
        {"mixer_dim": 64, "ffn_dim": 256, "kernel_size": 5},
    ]

    for cfg in test_configs:
        super_block.set_sample_config(
            sample_mixer_dim=cfg["mixer_dim"],
            sample_ffn_dim=cfg["ffn_dim"],
            sample_kernel_size=cfg["kernel_size"],
        )
        x = torch.randn(B, H, W, C)
        with torch.no_grad():
            y_super = super_block(x)
            subnet = super_block.get_active_subnet().eval()
            y_sub = subnet(x)
        diff = (y_super - y_sub).abs().max().item()
        print(f"  [cfg={cfg}] diff={diff:.2e}")
        assert diff < 1e-5, f"Consistency check failed: {diff}"

    super_block.set_sample_config(
        sample_mixer_dim=32, sample_ffn_dim=128, sample_kernel_size=3
    )
    print(
        f"[Params] elastic_num_params (mixer=32, ffn=128, k=3): {super_block.elastic_num_params}"
    )
    print(">>> All UniFormerLocalMixerBlock tests passed!")
