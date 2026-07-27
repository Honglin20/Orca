import copy
from typing import Any

import torch
import torch.nn as nn

from .common import make_cnn_stage_downsample
from .primitive_blocks import ElasticConv2d, ElasticGroupNorm2d, GroupNorm2d


class ElasticResConvBlock(nn.Module):
    def __init__(
        self,
        *,
        super_hidden_channels: int | None = None,
        in_channels: int,
        out_channels: int,
        candidate_kernel_sizes: tuple[int, ...] = (3, 5),
        stride: int = 1,
        activation_layer: type[nn.Module] = nn.ReLU,
    ):
        super().__init__()
        if not candidate_kernel_sizes:
            raise ValueError("candidate_kernel_sizes must not be empty.")
        if super_hidden_channels is None:
            super_hidden_channels = out_channels
        if super_hidden_channels <= 0:
            raise ValueError("super_hidden_channels must be positive.")

        self.super_hidden_channels = super_hidden_channels
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.stride = stride
        self.candidate_kernel_sizes = tuple(sorted(set(candidate_kernel_sizes)))
        self.active_kernel_size = max(self.candidate_kernel_sizes)
        self.sample_hidden_channels = super_hidden_channels

        self.downsample = make_cnn_stage_downsample(
            in_channels=in_channels,
            out_channels=out_channels,
            stride=stride,
        )
        self.norm1 = GroupNorm2d(out_channels)
        self.act1 = activation_layer()
        self.conv1 = ElasticConv2d(
            super_in_channels=out_channels,
            super_out_channels=super_hidden_channels,
            kernel_size=max(self.candidate_kernel_sizes),
            stride=1,
            padding=max(self.candidate_kernel_sizes) // 2,
            bias=False,
            candidate_kernel_sizes=self.candidate_kernel_sizes,
        )
        self.norm2 = ElasticGroupNorm2d(super_num_channels=super_hidden_channels)
        self.act2 = activation_layer()
        self.conv2 = ElasticConv2d(
            super_in_channels=super_hidden_channels,
            super_out_channels=out_channels,
            kernel_size=max(self.candidate_kernel_sizes),
            stride=1,
            padding=max(self.candidate_kernel_sizes) // 2,
            bias=False,
            candidate_kernel_sizes=self.candidate_kernel_sizes,
        )

        self.set_sample_config(
            sample_hidden_channels=self.sample_hidden_channels,
            sample_kernel_size=self.active_kernel_size,
        )

    def set_sample_config(
        self, *, sample_kernel_size: int, sample_hidden_channels: int | None = None
    ):
        if sample_kernel_size not in self.candidate_kernel_sizes:
            raise ValueError(f"Unsupported kernel size: {sample_kernel_size}")
        if sample_hidden_channels is None:
            sample_hidden_channels = self.sample_hidden_channels
        if not (0 < sample_hidden_channels <= self.super_hidden_channels):
            raise ValueError(f"Unsupported hidden channels: {sample_hidden_channels}")

        self.active_kernel_size = sample_kernel_size
        self.sample_hidden_channels = sample_hidden_channels
        self.conv1.set_sample_config(
            sample_in_channels=self.out_channels,
            sample_out_channels=sample_hidden_channels,
            sample_groups=1,
            sample_kernel_size=sample_kernel_size,
        )
        self.norm2.set_sample_config(sample_num_channels=sample_hidden_channels)
        self.conv2.set_sample_config(
            sample_in_channels=sample_hidden_channels,
            sample_out_channels=self.out_channels,
            sample_groups=1,
            sample_kernel_size=sample_kernel_size,
        )

    def forward(self, x):
        x = self.downsample(x)
        preact = self.act1(self.norm1(x))
        out = self.conv1(preact)
        out = self.norm2(out)
        out = self.act2(out)
        out = self.conv2(out)
        return x + out

    def get_active_subnet(self) -> nn.Module:
        class SubnetResConvBlock(nn.Module):
            def __init__(
                self,
                downsample: nn.Module,
                norm1: nn.Module,
                act1: nn.Module,
                conv1: nn.Module,
                norm2: nn.Module,
                act2: nn.Module,
                conv2: nn.Module,
            ):
                super().__init__()
                self.downsample = downsample
                self.norm1 = norm1
                self.act1 = act1
                self.conv1 = conv1
                self.norm2 = norm2
                self.act2 = act2
                self.conv2 = conv2

            def forward(self, x):
                x = self.downsample(x)
                preact = self.act1(self.norm1(x))
                out = self.conv1(preact)
                out = self.act2(self.norm2(out))
                out = self.conv2(out)
                return x + out

        return SubnetResConvBlock(
            downsample=copy.deepcopy(self.downsample),
            norm1=copy.deepcopy(self.norm1),
            act1=copy.deepcopy(self.act1),
            conv1=self.conv1.get_active_subnet(),
            norm2=self.norm2.get_active_subnet(),
            act2=copy.deepcopy(self.act2),
            conv2=self.conv2.get_active_subnet(),
        )

    @property
    def elastic_num_params(self):
        params = (
            sum(p.numel() for p in self.norm1.parameters())
            + self.conv1.elastic_num_params
            + self.norm2.elastic_num_params
            + self.conv2.elastic_num_params
        )
        params += sum(p.numel() for p in self.downsample.parameters())
        return params


def is_valid_res_conv_block(layer_config: dict[str, Any]) -> bool:
    kernel_size = layer_config.get("kernel_size")
    hidden_channels = layer_config.get("hidden_channels")
    return (
        isinstance(kernel_size, int)
        and kernel_size > 0
        and kernel_size % 2 == 1
        and isinstance(hidden_channels, int)
        and hidden_channels > 0
    )


def _main():
    torch.manual_seed(42)
    block = ElasticResConvBlock(
        super_hidden_channels=32,
        in_channels=24,
        out_channels=24,
        candidate_kernel_sizes=(3, 5),
        stride=1,
    ).eval()

    print("[Init] ResConvBlock In=24 Out=24 Kernels=(3, 5)")

    for kernel_size, hidden_channels in ((3, 16), (5, 32)):
        block.set_sample_config(
            sample_kernel_size=kernel_size,
            sample_hidden_channels=hidden_channels,
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
            f"  [Pass] kernel_size={kernel_size}, "
            f"hidden_channels={hidden_channels}, output={tuple(y.shape)}"
        )

    block_s2 = ElasticResConvBlock(
        super_hidden_channels=40,
        in_channels=24,
        out_channels=32,
        candidate_kernel_sizes=(3, 5),
        stride=2,
    ).eval()
    block_s2.set_sample_config(sample_kernel_size=3, sample_hidden_channels=32)
    x = torch.randn(2, 24, 16, 16)
    with torch.no_grad():
        y = block_s2(x)
        y_sub = block_s2.get_active_subnet().eval()(x)
    diff = (y - y_sub).abs().max().item()
    assert diff < 1e-5, f"Stride-2 consistency check failed: {diff}"
    assert y.shape == (2, 32, 8, 8), y.shape
    print(f"  [Pass] stride=2 auto-projection output={tuple(y.shape)}")

    print(f"[Params] Active params: {block.elastic_num_params}")
    print(">>> All ElasticResConvBlock Tests Passed!")


if __name__ == "__main__":
    _main()
