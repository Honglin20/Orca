import copy
from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F

from .common import make_cnn_stage_downsample
from .primitive_blocks import (
    ElasticConv2d,
    ElasticGroupNorm2d,
    ElasticLayerNorm2d,
    GroupNorm2d,
)


class HSwish(nn.Module):
    def __init__(self, inplace: bool = True):
        super().__init__()
        self.inplace = inplace

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x * F.relu6(x + 3.0, inplace=self.inplace) / 6.0


class SubnetMBV3SqueezeExcitation(nn.Module):
    def __init__(
        self,
        pool: nn.Module,
        conv1: nn.Module,
        norm1: nn.Module,
        act: nn.Module,
        conv2: nn.Module,
        gate: nn.Module,
    ):
        super().__init__()
        self.pool = pool
        self.conv1 = conv1
        self.norm1 = norm1
        self.act = act
        self.conv2 = conv2
        self.gate = gate

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        scale = self.pool(x)
        scale = self.conv1(scale)
        scale = self.norm1(scale)
        scale = self.act(scale)
        scale = self.conv2(scale)
        scale = self.gate(scale)
        return x * scale


class ElasticMBV3SqueezeExcitation(nn.Module):
    def __init__(self, *, super_channels: int, reduction: int = 4):
        super().__init__()
        self.super_channels = super_channels
        self.reduction = reduction
        self.active_channels = super_channels
        self.active_use = True
        squeezed_channels = max(super_channels // reduction, 8)

        self.pool = nn.AdaptiveAvgPool2d(1)
        self.conv1 = ElasticConv2d(
            super_in_channels=super_channels,
            super_out_channels=squeezed_channels,
            kernel_size=1,
            stride=1,
            padding=0,
            bias=False,
        )
        self.norm1 = ElasticLayerNorm2d(super_num_channels=squeezed_channels)
        self.act = nn.ReLU(inplace=True)
        self.conv2 = ElasticConv2d(
            super_in_channels=squeezed_channels,
            super_out_channels=super_channels,
            kernel_size=1,
            stride=1,
            padding=0,
            bias=False,
        )
        self.gate = nn.Hardsigmoid()

    def set_sample_config(self, *, sample_channels: int, sample_use: bool):
        self.active_channels = sample_channels
        self.active_use = sample_use
        squeezed_channels = max(sample_channels // self.reduction, 8)
        self.conv1.set_sample_config(
            sample_in_channels=sample_channels,
            sample_out_channels=squeezed_channels,
            sample_groups=1,
            sample_kernel_size=1,
        )
        self.norm1.set_sample_config(sample_num_channels=squeezed_channels)
        self.conv2.set_sample_config(
            sample_in_channels=squeezed_channels,
            sample_out_channels=sample_channels,
            sample_groups=1,
            sample_kernel_size=1,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if not self.active_use:
            return x
        scale = self.pool(x)
        scale = self.conv1(scale)
        scale = self.norm1(scale)
        scale = self.act(scale)
        scale = self.conv2(scale)
        scale = self.gate(scale)
        return x * scale

    def get_active_subnet(self) -> nn.Module:
        if not self.active_use:
            return nn.Identity()
        return SubnetMBV3SqueezeExcitation(
            self.pool,
            self.conv1.get_active_subnet(),
            self.norm1.get_active_subnet(),
            self.act,
            self.conv2.get_active_subnet(),
            self.gate,
        )

    @property
    def elastic_num_params(self):
        if not self.active_use:
            return 0
        return (
            self.conv1.elastic_num_params
            + self.norm1.elastic_num_params
            + self.conv2.elastic_num_params
        )


class ElasticMobileNetV3Block(nn.Module):
    def __init__(
        self,
        *,
        super_expand_channels: int,
        in_channels: int,
        out_channels: int,
        candidate_kernel_sizes: tuple[int, ...] = (3, 5),
        candidate_use_se: tuple[bool, ...] = (False, True),
        stride: int = 1,
        se_reduction: int = 4,
        activation_layer: type[nn.Module] = nn.ReLU,
    ):
        super().__init__()
        if super_expand_channels <= 0:
            raise ValueError("super_expand_channels must be positive.")
        if not candidate_kernel_sizes:
            raise ValueError("candidate_kernel_sizes must not be empty.")
        if not candidate_use_se:
            raise ValueError("candidate_use_se must not be empty.")

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.stride = stride
        self.super_expand_channels = super_expand_channels
        self.candidate_kernel_sizes = tuple(sorted(set(candidate_kernel_sizes)))
        self.candidate_use_se = tuple(candidate_use_se)

        self.sample_expand_channels = super_expand_channels
        self.sample_kernel_size = max(self.candidate_kernel_sizes)
        self.sample_use_se = (
            True if True in self.candidate_use_se else self.candidate_use_se[-1]
        )

        self.downsample = make_cnn_stage_downsample(
            in_channels=in_channels,
            out_channels=out_channels,
            stride=stride,
        )
        self.expand_conv = ElasticConv2d(
            super_in_channels=out_channels,
            super_out_channels=self.super_expand_channels,
            kernel_size=1,
            stride=1,
            padding=0,
            bias=False,
        )
        self.expand_norm = ElasticGroupNorm2d(
            super_num_channels=self.super_expand_channels
        )
        self.expand_act = activation_layer()

        self.depthwise_conv = ElasticConv2d(
            super_in_channels=self.super_expand_channels,
            super_out_channels=self.super_expand_channels,
            kernel_size=max(self.candidate_kernel_sizes),
            stride=1,
            padding=max(self.candidate_kernel_sizes) // 2,
            groups=self.super_expand_channels,
            bias=False,
            candidate_kernel_sizes=self.candidate_kernel_sizes,
        )
        self.depthwise_norm = ElasticGroupNorm2d(
            super_num_channels=self.super_expand_channels
        )
        self.depthwise_act = activation_layer()

        self.se = ElasticMBV3SqueezeExcitation(
            super_channels=self.super_expand_channels, reduction=se_reduction
        )

        self.project_conv = ElasticConv2d(
            super_in_channels=self.super_expand_channels,
            super_out_channels=out_channels,
            kernel_size=1,
            stride=1,
            padding=0,
            bias=False,
        )
        self.project_norm = GroupNorm2d(out_channels)

        self.set_sample_config(
            sample_expand_channels=self.sample_expand_channels,
            sample_kernel_size=self.sample_kernel_size,
            sample_use_se=self.sample_use_se,
        )

    def set_sample_config(
        self,
        *,
        sample_expand_channels: int,
        sample_kernel_size: int,
        sample_use_se: bool | None = None,
    ):
        if not (0 < sample_expand_channels <= self.super_expand_channels):
            raise ValueError(f"Unsupported expand channels: {sample_expand_channels}")
        if sample_kernel_size not in self.candidate_kernel_sizes:
            raise ValueError(f"Unsupported kernel size: {sample_kernel_size}")
        if sample_use_se is None:
            sample_use_se = self.sample_use_se
        if sample_use_se not in self.candidate_use_se:
            raise ValueError(f"Unsupported use_se flag: {sample_use_se}")

        self.sample_expand_channels = sample_expand_channels
        self.sample_kernel_size = sample_kernel_size
        self.sample_use_se = sample_use_se

        expand_channels = self.sample_expand_channels
        self.expand_conv.set_sample_config(
            sample_in_channels=self.out_channels,
            sample_out_channels=expand_channels,
            sample_groups=1,
            sample_kernel_size=1,
        )
        self.expand_norm.set_sample_config(sample_num_channels=expand_channels)
        self.depthwise_conv.set_sample_config(
            sample_in_channels=expand_channels,
            sample_out_channels=expand_channels,
            sample_groups=expand_channels,
            sample_kernel_size=self.sample_kernel_size,
        )
        self.depthwise_norm.set_sample_config(sample_num_channels=expand_channels)
        self.se.set_sample_config(
            sample_channels=expand_channels, sample_use=self.sample_use_se
        )
        self.project_conv.set_sample_config(
            sample_in_channels=expand_channels,
            sample_out_channels=self.out_channels,
            sample_groups=1,
            sample_kernel_size=1,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.downsample(x)
        out = self.expand_conv(x)
        out = self.expand_norm(out)
        out = self.expand_act(out)
        out = self.depthwise_conv(out)
        out = self.depthwise_norm(out)
        out = self.depthwise_act(out)
        out = self.se(out)
        out = self.project_conv(out)
        out = self.project_norm(out)

        return x + out

    def get_active_subnet(self) -> nn.Module:
        class SubnetMobileNetV3Block(nn.Module):
            def __init__(self, downsample: nn.Module, main_path: nn.Module):
                super().__init__()
                self.downsample = downsample
                self.main_path = main_path

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                x = self.downsample(x)
                out = self.main_path(x)
                return x + out

        return SubnetMobileNetV3Block(
            downsample=copy.deepcopy(self.downsample),
            main_path=nn.Sequential(
                self.expand_conv.get_active_subnet(),
                self.expand_norm.get_active_subnet(),
                self.expand_act,
                self.depthwise_conv.get_active_subnet(),
                self.depthwise_norm.get_active_subnet(),
                self.depthwise_act,
                self.se.get_active_subnet(),
                self.project_conv.get_active_subnet(),
                copy.deepcopy(self.project_norm),
            ),
        )

    @property
    def elastic_num_params(self):
        params = (
            self.expand_conv.elastic_num_params
            + self.expand_norm.elastic_num_params
            + self.depthwise_conv.elastic_num_params
            + self.depthwise_norm.elastic_num_params
            + self.se.elastic_num_params
            + self.project_conv.elastic_num_params
            + sum(p.numel() for p in self.project_norm.parameters())
        )
        params += sum(p.numel() for p in self.downsample.parameters())
        return params


def is_valid_mobilenet_v3_block(layer_config: dict[str, Any]) -> bool:
    expand_channels = layer_config.get("expand_channels")
    kernel_size = layer_config.get("kernel_size")
    use_se = layer_config.get("use_se", True)
    return (
        isinstance(expand_channels, int)
        and expand_channels > 0
        and isinstance(kernel_size, int)
        and kernel_size > 0
        and kernel_size % 2 == 1
        and isinstance(use_se, bool)
    )


if __name__ == "__main__":
    torch.manual_seed(42)

    super_block = ElasticMobileNetV3Block(
        super_expand_channels=32,
        in_channels=16,
        out_channels=16,
        candidate_kernel_sizes=(3, 5),
        stride=1,
    ).eval()

    print("[Init] MobileNetV3Block In=16 Out=16 Kernels=(3, 5)")

    test_configs = [
        {"expand_channels": 16, "kernel_size": 3},
        {"expand_channels": 32, "kernel_size": 5},
    ]

    for cfg in test_configs:
        super_block.set_sample_config(
            sample_expand_channels=cfg["expand_channels"],
            sample_kernel_size=cfg["kernel_size"],
        )
        x = torch.randn(2, 16, 16, 16)
        with torch.no_grad():
            y_super = super_block(x)
            subnet = super_block.get_active_subnet().eval()
            y_sub = subnet(x)

        diff = (y_super - y_sub).abs().max().item()
        print(f"  [cfg={cfg}] output={tuple(y_super.shape)} diff={diff:.2e}")
        assert diff < 1e-5, f"Consistency check failed: {diff}"

    block_s2 = ElasticMobileNetV3Block(
        super_expand_channels=64,
        in_channels=16,
        out_channels=32,
        candidate_kernel_sizes=(3, 5),
        candidate_use_se=(False, True),
        stride=2,
    ).eval()

    print("[Init] MobileNetV3Block In=16 Out=32 Kernels=(3, 5) stride=2")

    for cfg in (
        {"expand_channels": 32, "kernel_size": 3, "use_se": False},
        {"expand_channels": 64, "kernel_size": 5, "use_se": True},
    ):
        block_s2.set_sample_config(
            sample_expand_channels=cfg["expand_channels"],
            sample_kernel_size=cfg["kernel_size"],
            sample_use_se=cfg["use_se"],
        )
        x = torch.randn(2, 16, 16, 16)
        with torch.no_grad():
            y_super = block_s2(x)
            subnet = block_s2.get_active_subnet().eval()
            y_sub = subnet(x)

        diff = (y_super - y_sub).abs().max().item()
        print(f"  [stride=2 cfg={cfg}] output={tuple(y_super.shape)} diff={diff:.2e}")
        assert y_super.shape == (2, 32, 8, 8), y_super.shape
        assert diff < 1e-5, f"Consistency check failed: {diff}"

    print(f"[Params] Active params: {super_block.elastic_num_params}")
    print(">>> All ElasticMobileNetV3Block Tests Passed!")
