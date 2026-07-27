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


# -----------------------------------------------------------------------
# Full Block
# Ref: https://arxiv.org/abs/2111.11418 (MetaFormer / PoolFormer)
# -----------------------------------------------------------------------


class ElasticPoolFormerBlock(nn.Module):
    """PoolFormer / MetaFormer block: AvgPool token-mixer + GELU MLP.

    Structure (pre-norm ordering):
        [GroupNorm(1, C) -> AvgPool(pool_size, count_include_pad=False) − x] + residual
        [GroupNorm(1, C) -> MLP(GELU)] + residual

    The token mixer is `avg_pool(x) − x` (pooling "difference"), which has
    zero-mean output at initialisation, following the original PoolFormer.
    pool_size is fixed to 3 (not a search dimension).

    Input: [B, H, W, global_dim].
    """

    def __init__(
        self,
        *,
        super_ffn_dim: int,
        global_dim: int,
        pool_size: int = 3,
    ):
        super().__init__()
        self.global_dim = global_dim
        self.super_ffn_dim = super_ffn_dim
        self.pool_size = pool_size

        self.norm1 = nn.GroupNorm(1, self.global_dim)
        self.norm2 = nn.GroupNorm(1, self.global_dim)
        self.mlp_fc1 = ElasticLinear(
            super_in_dim=self.global_dim, super_out_dim=super_ffn_dim
        )
        self.mlp_act = nn.GELU()
        self.mlp_fc2 = ElasticLinear(
            super_in_dim=super_ffn_dim, super_out_dim=self.global_dim
        )

        self.sample_ffn_dim = super_ffn_dim

    def set_sample_config(self, *, sample_ffn_dim: int):
        self.sample_ffn_dim = sample_ffn_dim

        self.mlp_fc1.set_sample_config(
            sample_in_dim=self.global_dim, sample_out_dim=self.sample_ffn_dim
        )
        self.mlp_fc2.set_sample_config(
            sample_in_dim=self.sample_ffn_dim, sample_out_dim=self.global_dim
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        mix = _apply_nchw_norm_to_bhwc(self.norm1, x)
        mix_2d = mix.permute(0, 3, 1, 2).contiguous()
        pooled = F.avg_pool2d(
            mix_2d,
            kernel_size=self.pool_size,
            stride=1,
            padding=self.pool_size // 2,
            count_include_pad=False,
        )
        xi = x + pooled.permute(0, 2, 3, 1).contiguous() - mix
        xi = xi + self.mlp_fc2(
            self.mlp_act(self.mlp_fc1(_apply_nchw_norm_to_bhwc(self.norm2, xi)))
        )
        return xi

    def get_active_subnet(self) -> nn.Module:
        pool_size = self.pool_size

        class PoolFormerBlock(nn.Module):
            def __init__(self, norm1, norm2, mlp):
                super().__init__()
                self.norm1 = norm1
                self.norm2 = norm2
                self.mlp = mlp
                self.pool_size = pool_size

            def forward(self, x):
                mix = _apply_nchw_norm_to_bhwc(self.norm1, x)
                mix_2d = mix.permute(0, 3, 1, 2).contiguous()
                pooled = F.avg_pool2d(
                    mix_2d,
                    kernel_size=self.pool_size,
                    stride=1,
                    padding=self.pool_size // 2,
                    count_include_pad=False,
                )
                xi = x + pooled.permute(0, 2, 3, 1).contiguous() - mix
                xi = xi + self.mlp(_apply_nchw_norm_to_bhwc(self.norm2, xi))
                return xi

        return PoolFormerBlock(
            copy.deepcopy(self.norm1),
            copy.deepcopy(self.norm2),
            nn.Sequential(
                self.mlp_fc1.get_active_subnet(),
                self.mlp_act,
                self.mlp_fc2.get_active_subnet(),
            ),
        )

    @property
    def elastic_num_params(self):
        # AvgPool has no parameters
        return (
            _module_num_params(self.norm1)
            + _module_num_params(self.norm2)
            + self.mlp_fc1.elastic_num_params
            + self.mlp_fc2.elastic_num_params
        )


def is_valid_poolformer_block(config: dict[str, Any]) -> bool:
    ffn_dim = config.get("ffn_dim")
    return isinstance(ffn_dim, int) and ffn_dim > 0


if __name__ == "__main__":
    B, H, W, C = 2, 14, 16, 128

    super_block = ElasticPoolFormerBlock(
        global_dim=C,
        super_ffn_dim=256,
        pool_size=3,
    ).eval()

    print(f"[Init] PoolFormerBlock  global={C}, pool_size=3")

    torch.manual_seed(0)
    test_configs = [
        {"ffn_dim": 64},
        {"ffn_dim": 256},
    ]

    for cfg in test_configs:
        super_block.set_sample_config(sample_ffn_dim=cfg["ffn_dim"])
        x = torch.randn(B, H, W, C)
        with torch.no_grad():
            y_super = super_block(x)
            subnet = super_block.get_active_subnet().eval()
            y_sub = subnet(x)
        diff = (y_super - y_sub).abs().max().item()
        print(f"  [cfg={cfg}] diff={diff:.2e}")
        assert diff < 1e-5, f"Consistency check failed: {diff}"

    super_block.set_sample_config(sample_ffn_dim=128)
    print(f"[Params] elastic_num_params (ffn=128): {super_block.elastic_num_params}")
    print(">>> All PoolFormerBlock tests passed!")
