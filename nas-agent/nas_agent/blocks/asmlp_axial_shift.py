import copy
from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F

from .primitive_blocks import ElasticLinear


def _module_num_params(module: nn.Module) -> int:
    return sum(p.numel() for p in module.parameters())


def _apply_nchw_norm_to_bhwc(norm: nn.Module, x: torch.Tensor) -> torch.Tensor:
    x_nchw = x.permute(0, 3, 1, 2).contiguous()
    return norm(x_nchw).permute(0, 2, 3, 1).contiguous()


def _axial_shift_dim(x2d: torch.Tensor, shift_size: int, dim: int) -> torch.Tensor:
    """Official AS-MLP-style channel-chunk shift along one spatial axis."""
    if shift_size <= 0 or shift_size % 2 == 0:
        raise ValueError("shift_size must be a positive odd integer.")
    if x2d.size(1) < shift_size:
        raise ValueError("Channel count must be at least shift_size for AS-MLP shift.")

    pad = shift_size // 2
    if pad == 0:
        return x2d

    _, _, height, width = x2d.shape
    x_pad = F.pad(x2d, (pad, pad, pad, pad), "constant", 0)
    chunks = torch.chunk(x_pad, shift_size, dim=1)
    shifts = range(-pad, pad + 1)
    shifted = [
        torch.roll(chunk, shift, dims=dim) for chunk, shift in zip(chunks, shifts)
    ]
    x_cat = torch.cat(shifted, dim=1)
    return x_cat[:, :, pad : pad + height, pad : pad + width]


# -----------------------------------------------------------------------
# Full Block
# Ref: https://arxiv.org/abs/2107.08391 (AS-MLP)
# -----------------------------------------------------------------------


class ElasticASMLPAxialShiftBlock(nn.Module):
    """AS-MLP block: axial-shift token mixer + GELU MLP.

    Replaces self-attention with the official AS-MLP axial-shift mixer:
    1x1 conv projection, GroupNorm(1, C), horizontal and vertical shifted
    branches with separate 1x1 convs, branch addition, GroupNorm(1, C),
    and final 1x1 conv projection.

    `shift_size` is elastic; all 1x1 conv and GroupNorm components operate
    on the fixed external `global_dim` stage width.

    Input: [B, H, W, global_dim].
    """

    def __init__(
        self,
        *,
        super_ffn_dim: int,
        super_shift_size: int,
        global_dim: int,
        as_bias: bool = True,
    ):
        super().__init__()
        if global_dim < super_shift_size:
            raise ValueError("AS-MLP requires global_dim >= super_shift_size.")
        if super_shift_size <= 0 or super_shift_size % 2 == 0:
            raise ValueError("super_shift_size must be a positive odd integer.")
        self.global_dim = global_dim
        self.super_ffn_dim = super_ffn_dim
        self.super_shift_size = super_shift_size

        self.norm1 = nn.GroupNorm(1, global_dim)
        self.conv1 = nn.Conv2d(global_dim, global_dim, kernel_size=1, bias=as_bias)
        self.axial_norm1 = nn.GroupNorm(1, global_dim)
        self.axial_act = nn.GELU()
        self.conv2_1 = nn.Conv2d(global_dim, global_dim, kernel_size=1, bias=as_bias)
        self.conv2_2 = nn.Conv2d(global_dim, global_dim, kernel_size=1, bias=as_bias)
        self.axial_norm2 = nn.GroupNorm(1, global_dim)
        self.conv3 = nn.Conv2d(global_dim, global_dim, kernel_size=1, bias=as_bias)
        self.norm2 = nn.GroupNorm(1, global_dim)
        self.mlp_fc1 = ElasticLinear(
            super_in_dim=global_dim, super_out_dim=super_ffn_dim
        )
        self.mlp_act = nn.GELU()
        self.mlp_fc2 = ElasticLinear(
            super_in_dim=super_ffn_dim, super_out_dim=global_dim
        )

        self.sample_ffn_dim = super_ffn_dim
        self.sample_shift_size = super_shift_size

    def set_sample_config(
        self,
        *,
        sample_ffn_dim: int,
        sample_shift_size: int,
    ):
        if sample_shift_size <= 0 or sample_shift_size % 2 == 0:
            raise ValueError("sample_shift_size must be a positive odd integer.")
        if sample_shift_size > self.super_shift_size:
            raise ValueError(
                f"sample_shift_size ({sample_shift_size}) exceeds super_shift_size ({self.super_shift_size})."
            )
        self.sample_ffn_dim = sample_ffn_dim
        self.sample_shift_size = sample_shift_size

        self.mlp_fc1.set_sample_config(
            sample_in_dim=self.global_dim, sample_out_dim=self.sample_ffn_dim
        )
        self.mlp_fc2.set_sample_config(
            sample_in_dim=self.sample_ffn_dim, sample_out_dim=self.global_dim
        )

    def _axial_shift_nchw(self, x2d: torch.Tensor) -> torch.Tensor:
        x2d = self.conv1(x2d)
        x2d = self.axial_norm1(x2d)
        x2d = self.axial_act(x2d)

        x_shift_lr = _axial_shift_dim(x2d, self.sample_shift_size, dim=3)
        x_shift_td = _axial_shift_dim(x2d, self.sample_shift_size, dim=2)
        x_lr = self.axial_act(self.conv2_1(x_shift_lr))
        x_td = self.axial_act(self.conv2_2(x_shift_td))

        x2d = x_lr + x_td
        x2d = self.axial_norm2(x2d)
        return self.conv3(x2d)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        mix = _apply_nchw_norm_to_bhwc(self.norm1, x)
        mix_2d = mix.permute(0, 3, 1, 2).contiguous()
        mix = self._axial_shift_nchw(mix_2d).permute(0, 2, 3, 1).contiguous()
        xi = x + mix
        xi = xi + self.mlp_fc2(
            self.mlp_act(self.mlp_fc1(_apply_nchw_norm_to_bhwc(self.norm2, xi)))
        )
        return xi

    def get_active_subnet(self) -> nn.Module:
        shift_size = self.sample_shift_size

        class ASMLPBlock(nn.Module):
            def __init__(
                self,
                norm1,
                conv1,
                axial_norm1,
                conv2_1,
                conv2_2,
                axial_norm2,
                conv3,
                norm2,
                mlp,
            ):
                super().__init__()
                self.norm1 = norm1
                self.conv1 = conv1
                self.axial_norm1 = axial_norm1
                self.conv2_1 = conv2_1
                self.conv2_2 = conv2_2
                self.axial_norm2 = axial_norm2
                self.conv3 = conv3
                self.axial_act = nn.GELU()
                self.norm2 = norm2
                self.mlp = mlp
                self.shift_size = shift_size

            def _axial_shift_nchw(self, x2d):
                x2d = self.conv1(x2d)
                x2d = self.axial_norm1(x2d)
                x2d = self.axial_act(x2d)

                x_shift_lr = _axial_shift_dim(x2d, self.shift_size, dim=3)
                x_shift_td = _axial_shift_dim(x2d, self.shift_size, dim=2)
                x_lr = self.axial_act(self.conv2_1(x_shift_lr))
                x_td = self.axial_act(self.conv2_2(x_shift_td))

                x2d = x_lr + x_td
                x2d = self.axial_norm2(x2d)
                return self.conv3(x2d)

            def forward(self, x):
                mix = _apply_nchw_norm_to_bhwc(self.norm1, x)
                mix_2d = mix.permute(0, 3, 1, 2).contiguous()
                mix = self._axial_shift_nchw(mix_2d).permute(0, 2, 3, 1).contiguous()
                x = x + mix
                x = x + self.mlp(_apply_nchw_norm_to_bhwc(self.norm2, x))
                return x

        return ASMLPBlock(
            copy.deepcopy(self.norm1),
            copy.deepcopy(self.conv1),
            copy.deepcopy(self.axial_norm1),
            copy.deepcopy(self.conv2_1),
            copy.deepcopy(self.conv2_2),
            copy.deepcopy(self.axial_norm2),
            copy.deepcopy(self.conv3),
            copy.deepcopy(self.norm2),
            nn.Sequential(
                self.mlp_fc1.get_active_subnet(),
                self.mlp_act,
                self.mlp_fc2.get_active_subnet(),
            ),
        )

    @property
    def elastic_num_params(self):
        return (
            _module_num_params(self.norm1)
            + _module_num_params(self.conv1)
            + _module_num_params(self.axial_norm1)
            + _module_num_params(self.conv2_1)
            + _module_num_params(self.conv2_2)
            + _module_num_params(self.axial_norm2)
            + _module_num_params(self.conv3)
            + _module_num_params(self.norm2)
            + self.mlp_fc1.elastic_num_params
            + self.mlp_fc2.elastic_num_params
        )


def is_valid_asmlp_axial_shift_block(config: dict[str, Any]) -> bool:
    ffn_dim = config.get("ffn_dim")
    shift_size = config.get("shift_size")
    return (
        isinstance(ffn_dim, int)
        and ffn_dim > 0
        and isinstance(shift_size, int)
        and shift_size > 0
        and shift_size % 2 == 1
    )


if __name__ == "__main__":
    B, H, W, C = 2, 14, 16, 128

    super_block = ElasticASMLPAxialShiftBlock(
        global_dim=C,
        super_ffn_dim=256,
        super_shift_size=5,
    ).eval()

    print(f"[Init] ASMLPBlock  global={C}, super_shift=5")

    torch.manual_seed(0)
    test_configs = [
        {"ffn_dim": 64, "shift_size": 3},
        {"ffn_dim": 256, "shift_size": 5},
    ]

    for cfg in test_configs:
        super_block.set_sample_config(
            sample_ffn_dim=cfg["ffn_dim"],
            sample_shift_size=cfg["shift_size"],
        )
        x = torch.randn(B, H, W, C)
        with torch.no_grad():
            y_super = super_block(x)
            subnet = super_block.get_active_subnet().eval()
            y_sub = subnet(x)
        diff = (y_super - y_sub).abs().max().item()
        print(f"  [cfg={cfg}] diff={diff:.2e}")
        assert diff < 1e-5, f"Consistency check failed: {diff}"

    super_block.set_sample_config(sample_ffn_dim=128, sample_shift_size=3)
    print(
        f"[Params] elastic_num_params (ffn=128, shift=3): {super_block.elastic_num_params}"
    )
    print(">>> All ASMLPBlock tests passed!")
