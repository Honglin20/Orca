import copy
from typing import Any

import torch
import torch.nn as nn

from .common import make_cnn_stage_downsample
from .primitive_blocks import ElasticConv2d, ElasticGroupNorm2d, GroupNorm2d


class ElasticDepthwiseSeparableConvBlock(nn.Module):
    def __init__(
        self,
        *,
        in_channels: int,
        out_channels: int,
        super_expand_channels: int | None = None,
        candidate_kernel_sizes: tuple[int, ...] = (3, 5),
        stride: int = 1,
        activation_layer: type[nn.Module] = nn.ReLU,
        pointwise_activation: bool = True,
    ):
        super().__init__()
        if not candidate_kernel_sizes:
            raise ValueError("candidate_kernel_sizes must not be empty.")
        if super_expand_channels is None:
            super_expand_channels = out_channels
        if super_expand_channels <= 0:
            raise ValueError("super_expand_channels must be positive.")
        if super_expand_channels % out_channels != 0:
            raise ValueError("super_expand_channels must be divisible by out_channels.")

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.super_expand_channels = super_expand_channels
        self.stride = stride
        self.candidate_kernel_sizes = tuple(sorted(set(candidate_kernel_sizes)))
        self.active_kernel_size = max(self.candidate_kernel_sizes)
        self.sample_expand_channels = super_expand_channels

        self.downsample = make_cnn_stage_downsample(
            in_channels=in_channels,
            out_channels=out_channels,
            stride=stride,
        )
        self.depthwise_conv = ElasticConv2d(
            super_in_channels=out_channels,
            super_out_channels=super_expand_channels,
            kernel_size=max(self.candidate_kernel_sizes),
            stride=1,
            padding=max(self.candidate_kernel_sizes) // 2,
            groups=out_channels,
            bias=False,
            candidate_kernel_sizes=self.candidate_kernel_sizes,
        )
        self.depthwise_norm = ElasticGroupNorm2d(
            super_num_channels=super_expand_channels
        )
        self.depthwise_act = activation_layer()

        self.pointwise_conv = ElasticConv2d(
            super_in_channels=super_expand_channels,
            super_out_channels=out_channels,
            kernel_size=1,
            stride=1,
            padding=0,
            bias=False,
        )
        self.pointwise_norm = GroupNorm2d(out_channels)
        self.pointwise_act = (
            activation_layer() if pointwise_activation else nn.Identity()
        )

        self.set_sample_config(
            sample_kernel_size=self.active_kernel_size,
            sample_expand_channels=self.sample_expand_channels,
        )

    def set_sample_config(
        self, *, sample_kernel_size: int, sample_expand_channels: int | None = None
    ):
        if sample_kernel_size not in self.candidate_kernel_sizes:
            raise ValueError(f"Unsupported kernel size: {sample_kernel_size}")
        if sample_expand_channels is None:
            sample_expand_channels = self.sample_expand_channels
        if not (0 < sample_expand_channels <= self.super_expand_channels):
            raise ValueError(f"Unsupported expand channels: {sample_expand_channels}")
        if sample_expand_channels % self.out_channels != 0:
            raise ValueError(
                "sample_expand_channels must be divisible by out_channels."
            )

        self.active_kernel_size = sample_kernel_size
        self.sample_expand_channels = sample_expand_channels
        self.depthwise_conv.set_sample_config(
            sample_in_channels=self.out_channels,
            sample_out_channels=sample_expand_channels,
            sample_groups=self.out_channels,
            sample_kernel_size=sample_kernel_size,
        )
        self.depthwise_norm.set_sample_config(
            sample_num_channels=sample_expand_channels
        )
        self.pointwise_conv.set_sample_config(
            sample_in_channels=sample_expand_channels,
            sample_out_channels=self.out_channels,
            sample_groups=1,
            sample_kernel_size=1,
        )

    def forward(self, x):
        x = self.downsample(x)
        out = self.depthwise_conv(x)
        out = self.depthwise_norm(out)
        out = self.depthwise_act(out)
        out = self.pointwise_conv(out)
        out = self.pointwise_norm(out)
        out = self.pointwise_act(out)
        return x + out

    def get_active_subnet(self) -> nn.Module:
        class SubnetDepthwiseSeparableConvBlock(nn.Module):
            def __init__(
                self,
                downsample: nn.Module,
                main_path: nn.Module,
            ):
                super().__init__()
                self.downsample = downsample
                self.main_path = main_path

            def forward(self, x):
                x = self.downsample(x)
                out = self.main_path(x)
                return x + out

        return SubnetDepthwiseSeparableConvBlock(
            downsample=copy.deepcopy(self.downsample),
            main_path=nn.Sequential(
                self.depthwise_conv.get_active_subnet(),
                self.depthwise_norm.get_active_subnet(),
                copy.deepcopy(self.depthwise_act),
                self.pointwise_conv.get_active_subnet(),
                copy.deepcopy(self.pointwise_norm),
                copy.deepcopy(self.pointwise_act),
            ),
        )

    @property
    def elastic_num_params(self):
        params = (
            self.depthwise_conv.elastic_num_params
            + self.depthwise_norm.elastic_num_params
            + self.pointwise_conv.elastic_num_params
            + sum(p.numel() for p in self.pointwise_norm.parameters())
        )
        params += sum(p.numel() for p in self.downsample.parameters())
        return params


def is_valid_depthwise_separable_conv_block(layer_config: dict[str, Any]) -> bool:
    kernel_size = layer_config.get("kernel_size")
    expand_channels = layer_config.get("expand_channels")
    return (
        isinstance(kernel_size, int)
        and kernel_size > 0
        and kernel_size % 2 == 1
        and isinstance(expand_channels, int)
        and expand_channels > 0
    )


if __name__ == "__main__":
    torch.manual_seed(42)
    block = ElasticDepthwiseSeparableConvBlock(
        in_channels=24,
        out_channels=24,
        super_expand_channels=48,
        candidate_kernel_sizes=(3, 5),
        stride=1,
    ).eval()

    print("[Init] DepthwiseSeparableConvBlock In=24 Out=24 Kernels=(3, 5)")

    for kernel_size, expand_channels in ((3, 24), (5, 48)):
        block.set_sample_config(
            sample_kernel_size=kernel_size,
            sample_expand_channels=expand_channels,
        )
        x = torch.randn(2, 24, 16, 16)
        with torch.no_grad():
            y = block(x)
            subnet = block.get_active_subnet().eval()
            y_sub = subnet(x)
        diff = (y - y_sub).abs().max().item()
        assert diff < 1e-5, f"Consistency check failed: {diff}"
        assert y.shape == (2, 24, 16, 16), y.shape
        print(
            f"  [Pass] kernel_size={kernel_size}, expand_channels={expand_channels}, output={tuple(y.shape)}"
        )

    block_s2 = ElasticDepthwiseSeparableConvBlock(
        in_channels=24,
        out_channels=48,
        super_expand_channels=96,
        candidate_kernel_sizes=(3, 5),
        stride=2,
    ).eval()
    print("[Init] DepthwiseSeparableConvBlock In=24 Out=48 Kernels=(3, 5) stride=2")

    for kernel_size, expand_channels, spatial_size in ((3, 48, 15), (5, 96, 16)):
        block_s2.set_sample_config(
            sample_kernel_size=kernel_size,
            sample_expand_channels=expand_channels,
        )
        x = torch.randn(2, 24, spatial_size, spatial_size)
        with torch.no_grad():
            y = block_s2(x)
            subnet = block_s2.get_active_subnet().eval()
            y_sub = subnet(x)
        diff = (y - y_sub).abs().max().item()
        assert diff < 1e-5, f"Consistency check failed: {diff}"
        assert y.shape == (2, 48, 8, 8), y.shape
        print(
            f"  [Pass] stride=2 kernel_size={kernel_size}, expand_channels={expand_channels}, input={spatial_size}, output={tuple(y.shape)}"
        )

    print(f"[Params] Active params: {block.elastic_num_params}")
    print(">>> All ElasticDepthwiseSeparableConvBlock Tests Passed!")
