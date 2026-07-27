import copy
import math
from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F

from .common import make_cnn_stage_downsample
from .primitive_blocks import ElasticConv2d, GroupNorm2d


class GaborDepthwiseConv2d(nn.Module):
    """Materialized active Gabor filter bank."""

    def __init__(
        self,
        weight: torch.Tensor,
        *,
        in_channels: int,
        kernel_size: int,
        stride: int,
    ):
        super().__init__()
        self.weight = nn.Parameter(weight.clone())
        self.in_channels = in_channels
        self.out_channels = weight.size(0)
        self.groups = in_channels
        self.kernel_size = kernel_size
        self.stride = stride

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return F.conv2d(
            x,
            self.weight.to(dtype=x.dtype),
            stride=self.stride,
            padding=self.kernel_size // 2,
            groups=self.groups,
        )


class ElasticGaborDepthwiseConv2d(nn.Module):
    """Depthwise 2-D Gabor filter bank with elastic odd kernel size."""

    def __init__(
        self,
        *,
        channels: int,
        super_kernel_size: int,
        super_out_channels: int | None = None,
        stride: int = 1,
    ):
        super().__init__()
        if super_kernel_size % 2 == 0:
            raise ValueError("super_kernel_size must be odd.")
        if stride < 1:
            raise ValueError("stride must be >= 1.")
        if super_out_channels is None:
            super_out_channels = channels
        if super_out_channels % channels != 0:
            raise ValueError("super_out_channels must be divisible by channels.")
        self.channels = channels
        self.super_out_channels = super_out_channels
        self.sample_out_channels = super_out_channels
        self.super_kernel_size = super_kernel_size
        self.sample_kernel_size = super_kernel_size
        self.stride = stride

        self.log_sigma = nn.Parameter(torch.zeros(super_out_channels))
        self.log_frequency = nn.Parameter(torch.zeros(super_out_channels))
        self.theta = nn.Parameter(torch.linspace(0.0, math.pi, super_out_channels))
        self.phase = nn.Parameter(torch.zeros(super_out_channels))
        self.amplitude = nn.Parameter(torch.ones(super_out_channels))

    def set_sample_config(
        self, *, sample_kernel_size: int, sample_out_channels: int | None = None
    ):
        if sample_kernel_size % 2 == 0:
            raise ValueError("sample_kernel_size must be odd.")
        if sample_kernel_size > self.super_kernel_size:
            raise ValueError("sample_kernel_size cannot exceed super_kernel_size.")
        if sample_out_channels is None:
            sample_out_channels = self.sample_out_channels
        if not (0 < sample_out_channels <= self.super_out_channels):
            raise ValueError(f"Unsupported out channels: {sample_out_channels}")
        if sample_out_channels % self.channels != 0:
            raise ValueError("sample_out_channels must be divisible by channels.")
        self.sample_kernel_size = sample_kernel_size
        self.sample_out_channels = sample_out_channels

    def _kernel(self) -> torch.Tensor:
        k = self.sample_kernel_size
        coords = torch.arange(k, device=self.theta.device, dtype=torch.float32)
        coords = coords - (k - 1) / 2.0
        yy, xx = torch.meshgrid(coords, coords, indexing="ij")

        out_channels = self.sample_out_channels
        theta = self.theta[:out_channels].float().view(-1, 1, 1)
        sigma = (F.softplus(self.log_sigma[:out_channels].float()) + 0.5).view(-1, 1, 1)
        frequency = (F.softplus(self.log_frequency[:out_channels].float()) + 0.05).view(
            -1, 1, 1
        )
        phase = self.phase[:out_channels].float().view(-1, 1, 1)
        amplitude = self.amplitude[:out_channels].float().view(-1, 1, 1)

        x_theta = xx * torch.cos(theta) + yy * torch.sin(theta)
        y_theta = -xx * torch.sin(theta) + yy * torch.cos(theta)
        envelope = torch.exp(
            -(x_theta.square() + y_theta.square()) / (2.0 * sigma.square())
        )
        carrier = torch.cos(2.0 * math.pi * frequency * x_theta + phase)
        kernel = envelope * carrier
        kernel = kernel - kernel.mean(dim=(1, 2), keepdim=True)
        kernel = kernel / (kernel.abs().sum(dim=(1, 2), keepdim=True) + 1e-6)
        return (amplitude * kernel).unsqueeze(1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        padding = self.sample_kernel_size // 2
        return F.conv2d(
            x,
            self._kernel().to(dtype=x.dtype),
            stride=self.stride,
            padding=padding,
            groups=self.channels,
        )

    def get_active_subnet(self) -> nn.Module:
        return GaborDepthwiseConv2d(
            self._kernel().detach(),
            in_channels=self.channels,
            kernel_size=self.sample_kernel_size,
            stride=self.stride,
        )

    @property
    def elastic_num_params(self):
        k = self.sample_kernel_size
        return self.sample_out_channels * k * k


class ElasticGaborDepthwiseConvBlock(nn.Module):
    """Gabor depthwise CNN block with optional stride downsampling."""

    def __init__(
        self,
        *,
        in_channels: int,
        out_channels: int,
        super_expand_channels: int | None = None,
        candidate_kernel_sizes: tuple[int, ...] = (7, 11),
        stride: int = 1,
        activation_layer: type[nn.Module] = nn.GELU,
    ):
        super().__init__()
        if not candidate_kernel_sizes:
            raise ValueError("candidate_kernel_sizes must not be empty.")
        if stride < 1:
            raise ValueError("stride must be >= 1.")
        if super_expand_channels is None:
            super_expand_channels = out_channels
        if super_expand_channels <= 0:
            raise ValueError("super_expand_channels must be positive.")
        if super_expand_channels % out_channels != 0:
            raise ValueError("super_expand_channels must be divisible by out_channels.")
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.super_expand_channels = super_expand_channels
        self.candidate_kernel_sizes = tuple(sorted(set(candidate_kernel_sizes)))
        self.stride = stride
        self.sample_kernel_size = max(self.candidate_kernel_sizes)
        self.sample_expand_channels = super_expand_channels

        self.downsample = make_cnn_stage_downsample(
            in_channels=in_channels,
            out_channels=out_channels,
            stride=stride,
        )
        self.gabor = ElasticGaborDepthwiseConv2d(
            channels=out_channels,
            super_kernel_size=self.sample_kernel_size,
            super_out_channels=super_expand_channels,
            stride=1,
        )
        self.pre_norm = GroupNorm2d(out_channels)
        self.pointwise = ElasticConv2d(
            super_in_channels=super_expand_channels,
            super_out_channels=out_channels,
            kernel_size=1,
            stride=1,
            padding=0,
            bias=False,
        )
        self.out_norm = GroupNorm2d(out_channels)
        self.out_act = activation_layer()

        self.set_sample_config(
            sample_kernel_size=self.sample_kernel_size,
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
        self.sample_kernel_size = sample_kernel_size
        self.sample_expand_channels = sample_expand_channels
        self.gabor.set_sample_config(
            sample_kernel_size=sample_kernel_size,
            sample_out_channels=sample_expand_channels,
        )
        self.pointwise.set_sample_config(
            sample_in_channels=sample_expand_channels,
            sample_out_channels=self.out_channels,
            sample_groups=1,
            sample_kernel_size=1,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.downsample(x)
        out = self.gabor(self.pre_norm(x))
        out = self.out_act(self.out_norm(self.pointwise(out)))
        return x + out

    def get_active_subnet(self) -> nn.Module:
        class SubnetGaborBlock(nn.Module):
            def __init__(self, downsample, main_path):
                super().__init__()
                self.downsample = downsample
                self.main_path = main_path

            def forward(self, x):
                x = self.downsample(x)
                out = self.main_path(x)
                return x + out

        return SubnetGaborBlock(
            copy.deepcopy(self.downsample),
            nn.Sequential(
                copy.deepcopy(self.pre_norm),
                self.gabor.get_active_subnet(),
                self.pointwise.get_active_subnet(),
                copy.deepcopy(self.out_norm),
                copy.deepcopy(self.out_act),
            ),
        )

    @property
    def elastic_num_params(self):
        params = (
            sum(p.numel() for p in self.pre_norm.parameters())
            + self.gabor.elastic_num_params
            + self.pointwise.elastic_num_params
            + sum(p.numel() for p in self.out_norm.parameters())
        )
        params += sum(p.numel() for p in self.downsample.parameters())
        return params


def is_valid_gabor_depthwise_conv_block(layer_config: dict[str, Any]) -> bool:
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
    block = ElasticGaborDepthwiseConvBlock(
        in_channels=24,
        out_channels=24,
        super_expand_channels=48,
        candidate_kernel_sizes=(7, 11),
    ).eval()

    for kernel_size, expand_channels in ((7, 24), (11, 48)):
        block.set_sample_config(
            sample_kernel_size=kernel_size,
            sample_expand_channels=expand_channels,
        )
        x = torch.randn(2, 24, 16, 16)
        with torch.no_grad():
            y = block(x)
            y_sub = block.get_active_subnet().eval()(x)
        diff = (y - y_sub).abs().max().item()
        print(
            f"  [Pass] kernel_size={kernel_size}, expand_channels={expand_channels}, output={tuple(y.shape)}, diff={diff:.2e}"
        )
        assert y.shape == x.shape
        assert diff < 1e-5

    block_s2 = ElasticGaborDepthwiseConvBlock(
        in_channels=24,
        out_channels=48,
        super_expand_channels=96,
        candidate_kernel_sizes=(7, 11),
        stride=2,
    ).eval()

    for kernel_size, expand_channels in ((7, 48), (11, 96)):
        block_s2.set_sample_config(
            sample_kernel_size=kernel_size,
            sample_expand_channels=expand_channels,
        )
        x = torch.randn(2, 24, 16, 16)
        with torch.no_grad():
            y = block_s2(x)
            y_sub = block_s2.get_active_subnet().eval()(x)
        diff = (y - y_sub).abs().max().item()
        print(
            f"  [Pass] stride=2 kernel_size={kernel_size}, "
            f"expand_channels={expand_channels}, output={tuple(y.shape)}, "
            f"diff={diff:.2e}"
        )
        assert y.shape == (2, 48, 8, 8)
        assert diff < 1e-5

    print(">>> All ElasticGaborDepthwiseConvBlock Tests Passed!")
