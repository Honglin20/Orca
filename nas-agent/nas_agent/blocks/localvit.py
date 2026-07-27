from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F

from .primitive_blocks import (
    ElasticConv2d,
    ElasticGroupNorm2d,
    ElasticLayerNorm,
    ElasticLinear,
    ElasticMHSAQKVProjector,
)

# -----------------------------------------------------------------------
# Core: Locally-Enhanced FFN (DW-conv inside the expanded MLP)
# Ref: https://arxiv.org/abs/2104.05707 (LocalViT)
# -----------------------------------------------------------------------


class HSigmoid(nn.Module):
    def __init__(self, inplace: bool = True):
        super().__init__()
        self.relu = nn.ReLU6(inplace=inplace)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.relu(x + 3) / 6


class HSwish(nn.Module):
    def __init__(self, inplace: bool = True):
        super().__init__()
        self.sigmoid = HSigmoid(inplace=inplace)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x * self.sigmoid(x)


def _make_localvit_act(act: str) -> nn.Module:
    if act == "hs":
        return HSwish()
    if act == "relu6":
        return nn.ReLU6(inplace=True)
    raise ValueError(f"Unsupported LocalViT ConvFFN activation: {act}")


class ElasticLocalityFeedForward(nn.Module):
    """LocalViT LocalityFeedForward with an internal residual.

    The official ConvFFN backbone is 1x1 conv -> norm -> act ->
    depthwise conv -> norm -> act -> 1x1 conv -> norm. BatchNorm is
    replaced with GroupNorm so elastic subnets do not require BN
    recalibration. SE/ECA is intentionally omitted.
    """

    def __init__(
        self,
        *,
        global_dim: int,
        super_ffn_dim: int,
        dw_kernel: int = 3,
        channels_per_group: int = 16,
        act: str = "hs",
    ):
        super().__init__()
        if dw_kernel % 2 == 0:
            raise ValueError("dw_kernel must be odd.")
        self.global_dim = global_dim
        self.super_ffn_dim = super_ffn_dim
        self.dw_kernel = dw_kernel
        self.channels_per_group = channels_per_group
        self.act = act

        self.conv1 = ElasticConv2d(
            super_in_channels=global_dim,
            super_out_channels=super_ffn_dim,
            kernel_size=1,
            bias=False,
        )
        self.norm1 = ElasticGroupNorm2d(
            super_num_channels=super_ffn_dim,
            channels_per_group=channels_per_group,
        )
        self.act1 = _make_localvit_act(act)
        self.dw_conv = ElasticConv2d(
            super_in_channels=super_ffn_dim,
            super_out_channels=super_ffn_dim,
            kernel_size=dw_kernel,
            stride=1,
            padding=dw_kernel // 2,
            groups=super_ffn_dim,
            bias=False,
        )
        self.norm2 = ElasticGroupNorm2d(
            super_num_channels=super_ffn_dim,
            channels_per_group=channels_per_group,
        )
        self.act2 = _make_localvit_act(act)
        self.conv2 = ElasticConv2d(
            super_in_channels=super_ffn_dim,
            super_out_channels=global_dim,
            kernel_size=1,
            bias=False,
        )
        self.norm3 = ElasticGroupNorm2d(
            super_num_channels=global_dim,
            channels_per_group=channels_per_group,
        )

        self.sample_ffn_dim = super_ffn_dim

    def set_sample_config(self, *, sample_ffn_dim: int):
        self.sample_ffn_dim = sample_ffn_dim
        self.conv1.set_sample_config(
            sample_in_channels=self.global_dim,
            sample_out_channels=sample_ffn_dim,
        )
        self.norm1.set_sample_config(sample_num_channels=sample_ffn_dim)
        self.dw_conv.set_sample_config(
            sample_in_channels=sample_ffn_dim,
            sample_out_channels=sample_ffn_dim,
            sample_groups=sample_ffn_dim,
        )
        self.norm2.set_sample_config(sample_num_channels=sample_ffn_dim)
        self.conv2.set_sample_config(
            sample_in_channels=sample_ffn_dim,
            sample_out_channels=self.global_dim,
        )
        self.norm3.set_sample_config(sample_num_channels=self.global_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        residual = x.permute(0, 3, 1, 2).contiguous()
        h = self.act1(self.norm1(self.conv1(residual)))
        h = self.act2(self.norm2(self.dw_conv(h)))
        h = self.norm3(self.conv2(h))
        return (residual + h).permute(0, 2, 3, 1).contiguous()

    def get_active_subnet(self) -> nn.Module:
        act = self.act

        class LocalityFeedForward(nn.Module):
            def __init__(self, conv1, norm1, dw_conv, norm2, conv2, norm3):
                super().__init__()
                self.conv1 = conv1
                self.norm1 = norm1
                self.act1 = _make_localvit_act(act)
                self.dw_conv = dw_conv
                self.norm2 = norm2
                self.act2 = _make_localvit_act(act)
                self.conv2 = conv2
                self.norm3 = norm3

            def forward(self, x):
                residual = x.permute(0, 3, 1, 2).contiguous()
                h = self.act1(self.norm1(self.conv1(residual)))
                h = self.act2(self.norm2(self.dw_conv(h)))
                h = self.norm3(self.conv2(h))
                return (residual + h).permute(0, 2, 3, 1).contiguous()

        return LocalityFeedForward(
            self.conv1.get_active_subnet(),
            self.norm1.get_active_subnet(),
            self.dw_conv.get_active_subnet(),
            self.norm2.get_active_subnet(),
            self.conv2.get_active_subnet(),
            self.norm3.get_active_subnet(),
        )

    @property
    def elastic_num_params(self):
        return (
            self.conv1.elastic_num_params
            + self.norm1.elastic_num_params
            + self.dw_conv.elastic_num_params
            + self.norm2.elastic_num_params
            + self.conv2.elastic_num_params
            + self.norm3.elastic_num_params
        )


class LocalViTBlock(nn.Module):
    """Materialized active LocalViT block."""

    def __init__(
        self,
        norm1: nn.Module,
        qkv_proj: nn.Module,
        out_proj: nn.Linear,
        norm2: nn.Module,
        ffn: nn.Module,
        *,
        head_dim: int,
    ):
        super().__init__()
        self.norm1 = norm1
        self.qkv_proj = qkv_proj
        self.out_proj = out_proj
        self.norm2 = norm2
        self.ffn = ffn
        self.head_dim = head_dim

    def _attn(self, x: torch.Tensor) -> torch.Tensor:
        batch, height, width, channels = x.shape
        num_tokens = height * width
        x_seq = x.reshape(batch, num_tokens, channels)
        q, k, v = self.qkv_proj(x_seq)
        attention = F.softmax(
            (q @ k.transpose(-2, -1)) * (self.head_dim**-0.5), dim=-1
        )
        out = (attention @ v).transpose(1, 2).reshape(batch, num_tokens, -1)
        return self.out_proj(out).reshape(batch, height, width, channels)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self._attn(self.norm1(x))
        ffn_input = self.norm2(x).permute(0, 3, 1, 2).contiguous()
        ffn = self.ffn.act1(self.ffn.norm1(self.ffn.conv1(ffn_input)))
        ffn = self.ffn.act2(self.ffn.norm2(self.ffn.dw_conv(ffn)))
        ffn = self.ffn.norm3(self.ffn.conv2(ffn))
        return x + ffn.permute(0, 2, 3, 1).contiguous()


# -----------------------------------------------------------------------
# Full Block
# -----------------------------------------------------------------------


class ElasticLocalViTBlock(nn.Module):
    """LocalViT block: standard MHSA + LocalityFeedForward (pre-LN).

    Same attention as a vanilla ViT block, but the FFN is replaced by the
    LocalViT convolutional feed-forward branch.

    Input: [B, H, W, global_dim].
    """

    def __init__(
        self,
        *,
        super_num_heads: int,
        super_ffn_dim: int,
        global_dim: int,
        head_dim: int,
        dw_kernel: int = 3,
        act: str = "hs",
    ):
        super().__init__()
        self.global_dim = global_dim
        self.head_dim = head_dim
        self.super_num_heads = super_num_heads
        self.super_attn_dim = super_num_heads * head_dim
        self.super_ffn_dim = super_ffn_dim

        self.norm1 = ElasticLayerNorm(super_hidden_size=global_dim)
        self.qkv_proj = ElasticMHSAQKVProjector(
            super_in_dim=global_dim,
            super_out_dim=self.super_attn_dim,
            head_dim=head_dim,
        )
        self.out_proj = ElasticLinear(
            super_in_dim=self.super_attn_dim, super_out_dim=global_dim
        )
        self.ffn = ElasticLocalityFeedForward(
            global_dim=global_dim,
            super_ffn_dim=super_ffn_dim,
            dw_kernel=dw_kernel,
            act=act,
        )
        self.norm2 = ElasticLayerNorm(super_hidden_size=global_dim)

        self.sample_num_heads = super_num_heads
        self.sample_embed_dim = self.super_attn_dim
        self.sample_ffn_dim = super_ffn_dim

    def set_sample_config(self, *, sample_num_heads: int, sample_ffn_dim: int):
        self.sample_num_heads = sample_num_heads
        self.sample_embed_dim = sample_num_heads * self.head_dim
        self.sample_ffn_dim = sample_ffn_dim

        self.norm1.set_sample_config(sample_hidden_size=self.global_dim)
        self.qkv_proj.set_sample_config(
            sample_in_dim=self.global_dim, sample_out_dim=self.sample_embed_dim
        )
        self.out_proj.set_sample_config(
            sample_in_dim=self.sample_embed_dim, sample_out_dim=self.global_dim
        )
        self.ffn.set_sample_config(sample_ffn_dim=sample_ffn_dim)
        self.norm2.set_sample_config(sample_hidden_size=self.global_dim)

    def _attn(self, x: torch.Tensor) -> torch.Tensor:
        B, H, W, C = x.shape
        N = H * W
        x_seq = x.reshape(B, N, C)
        d = self.head_dim
        q, k, v = self.qkv_proj(x_seq)
        attn = F.softmax((q @ k.transpose(-2, -1)) * (d**-0.5), dim=-1)
        out = (attn @ v).transpose(1, 2).reshape(B, N, -1)
        return self.out_proj(out).reshape(B, H, W, C)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self._attn(self.norm1(x))
        ffn_input = self.norm2(x).permute(0, 3, 1, 2).contiguous()
        ffn = self.ffn.act1(self.ffn.norm1(self.ffn.conv1(ffn_input)))
        ffn = self.ffn.act2(self.ffn.norm2(self.ffn.dw_conv(ffn)))
        ffn = self.ffn.norm3(self.ffn.conv2(ffn))
        x = x + ffn.permute(0, 2, 3, 1).contiguous()
        return x

    def get_active_subnet(self) -> nn.Module:
        return LocalViTBlock(
            self.norm1.get_active_subnet(),
            self.qkv_proj.get_active_subnet(),
            self.out_proj.get_active_subnet(),
            self.norm2.get_active_subnet(),
            self.ffn.get_active_subnet(),
            head_dim=self.head_dim,
        )

    @property
    def elastic_num_params(self):
        return (
            self.norm1.elastic_num_params
            + self.qkv_proj.elastic_num_params
            + self.out_proj.elastic_num_params
            + self.norm2.elastic_num_params
            + self.ffn.elastic_num_params
        )


def is_valid_localvit_block(
    config: dict[str, Any],
) -> bool:
    num_heads = config.get("num_heads")
    ffn_dim = config.get("ffn_dim")
    return (
        isinstance(num_heads, int)
        and num_heads > 0
        and isinstance(ffn_dim, int)
        and ffn_dim > 0
    )


if __name__ == "__main__":
    B, H, W, C = 2, 14, 16, 128

    super_block = ElasticLocalViTBlock(
        global_dim=C,
        head_dim=16,
        super_num_heads=4,
        super_ffn_dim=256,
        dw_kernel=3,
    ).eval()

    print(f"[Init] LocalViTBlock  global={C}, head_dim=16, max_heads=4, dw_k=3")

    torch.manual_seed(0)
    test_configs = [
        {"num_heads": 2, "ffn_dim": 64},
        {"num_heads": 4, "ffn_dim": 256},
    ]

    for cfg in test_configs:
        super_block.set_sample_config(
            sample_num_heads=cfg["num_heads"], sample_ffn_dim=cfg["ffn_dim"]
        )
        x = torch.randn(B, H, W, C)
        with torch.no_grad():
            y_super = super_block(x)
            subnet = super_block.get_active_subnet().eval()
            y_sub = subnet(x)
        diff = (y_super - y_sub).abs().max().item()
        print(f"  [cfg={cfg}] diff={diff:.2e}")
        assert diff < 1e-5, f"Consistency check failed: {diff}"

    super_block.set_sample_config(sample_num_heads=2, sample_ffn_dim=128)
    print(
        f"[Params] elastic_num_params (h=2, ffn=128): {super_block.elastic_num_params}"
    )
    print(">>> All LocalViTBlock tests passed!")
