import copy
from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F

from .common import make_cnn_stage_downsample
from .primitive_blocks import ElasticConv2d, ElasticGroupNorm2d, GroupNorm2d


class ElasticSqueezeExcitation(nn.Module):
    def __init__(
        self,
        *,
        super_channels: int,
        reduction: int = 4,
        activation_layer: type[nn.Module] = nn.SiLU,
    ):
        super().__init__()
        self.super_channels = super_channels
        self.reduction = reduction
        self.sample_channels = super_channels
        self.sample_use = True
        super_squeeze_channels = max(super_channels // reduction, 8)

        self.reduce = ElasticConv2d(
            super_in_channels=super_channels,
            super_out_channels=super_squeeze_channels,
            kernel_size=1,
            stride=1,
            padding=0,
        )
        self.act = activation_layer()
        self.expand = ElasticConv2d(
            super_in_channels=super_squeeze_channels,
            super_out_channels=super_channels,
            kernel_size=1,
            stride=1,
            padding=0,
        )
        self.gate = nn.Sigmoid()

    def set_sample_config(self, *, sample_channels: int, sample_use: bool = True):
        self.sample_channels = sample_channels
        self.sample_use = sample_use
        sample_squeeze_channels = max(sample_channels // self.reduction, 8)
        self.reduce.set_sample_config(
            sample_in_channels=sample_channels,
            sample_out_channels=sample_squeeze_channels,
            sample_kernel_size=1,
            sample_groups=1,
        )
        self.expand.set_sample_config(
            sample_in_channels=sample_squeeze_channels,
            sample_out_channels=sample_channels,
            sample_kernel_size=1,
            sample_groups=1,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if not self.sample_use:
            return x
        scale = F.adaptive_avg_pool2d(x, 1)
        scale = self.reduce(scale)
        scale = self.act(scale)
        scale = self.expand(scale)
        scale = self.gate(scale)
        return x * scale

    def get_active_subnet(self) -> nn.Module:
        if not self.sample_use:
            return nn.Identity()

        class SubnetSqueezeExcitation(nn.Module):
            def __init__(
                self,
                reduce: nn.Module,
                act: nn.Module,
                expand: nn.Module,
                gate: nn.Module,
            ):
                super().__init__()
                self.reduce = reduce
                self.act = act
                self.expand = expand
                self.gate = gate

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                scale = F.adaptive_avg_pool2d(x, 1)
                scale = self.reduce(scale)
                scale = self.act(scale)
                scale = self.expand(scale)
                scale = self.gate(scale)
                return x * scale

        return SubnetSqueezeExcitation(
            self.reduce.get_active_subnet(),
            self.act,
            self.expand.get_active_subnet(),
            self.gate,
        )

    @property
    def elastic_num_params(self):
        if not self.sample_use:
            return 0
        return self.reduce.elastic_num_params + self.expand.elastic_num_params


class ElasticEfficientNetBlock(nn.Module):
    def __init__(
        self,
        *,
        super_expand_channels: int,
        in_channels: int,
        out_channels: int,
        candidate_kernel_sizes: tuple[int, ...] = (3, 5),
        stride: int = 1,
        se_reduction: int = 4,
    ):
        super().__init__()
        if super_expand_channels <= 0:
            raise ValueError("super_expand_channels must be positive.")
        if not candidate_kernel_sizes:
            raise ValueError("candidate_kernel_sizes must not be empty.")

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.stride = stride
        self.super_expand_channels = super_expand_channels
        self.candidate_kernel_sizes = tuple(sorted(set(candidate_kernel_sizes)))
        self.sample_expand_channels = super_expand_channels
        self.sample_kernel_size = max(self.candidate_kernel_sizes)

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
        self.expand_act = nn.SiLU()

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
        self.depthwise_act = nn.SiLU()

        self.se = ElasticSqueezeExcitation(
            super_channels=self.super_expand_channels,
            reduction=se_reduction,
            activation_layer=nn.SiLU,
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
        )

    def set_sample_config(
        self,
        *,
        sample_expand_channels: int,
        sample_kernel_size: int,
    ):
        if not (0 < sample_expand_channels <= self.super_expand_channels):
            raise ValueError(f"Unsupported expand channels: {sample_expand_channels}")
        if sample_kernel_size not in self.candidate_kernel_sizes:
            raise ValueError(f"Unsupported kernel size: {sample_kernel_size}")

        self.sample_expand_channels = sample_expand_channels
        self.sample_kernel_size = sample_kernel_size

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
        self.se.set_sample_config(sample_channels=expand_channels, sample_use=True)
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
        class SubnetEfficientNetBlock(nn.Module):
            def __init__(self, downsample: nn.Module, main_path: nn.Module):
                super().__init__()
                self.downsample = downsample
                self.main_path = main_path

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                x = self.downsample(x)
                out = self.main_path(x)
                return x + out

        return SubnetEfficientNetBlock(
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


def is_valid_efficientnet_block(layer_config: dict[str, Any]) -> bool:
    expand_channels = layer_config.get("expand_channels")
    kernel_size = layer_config.get("kernel_size")
    return (
        isinstance(expand_channels, int)
        and expand_channels > 0
        and isinstance(kernel_size, int)
        and kernel_size > 0
        and kernel_size % 2 == 1
    )


if __name__ == "__main__":
    torch.manual_seed(42)

    super_block = ElasticEfficientNetBlock(
        super_expand_channels=32,
        in_channels=16,
        out_channels=16,
        candidate_kernel_sizes=(3, 5),
        stride=1,
    ).eval()

    print("[Init] EfficientNetBlock In=16 Out=16 Kernels=(3, 5)")

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

    block_s2 = ElasticEfficientNetBlock(
        super_expand_channels=64,
        in_channels=16,
        out_channels=32,
        candidate_kernel_sizes=(3, 5),
        stride=2,
    ).eval()

    print("[Init] EfficientNetBlock In=16 Out=32 Kernels=(3, 5) stride=2")

    for cfg in (
        {"expand_channels": 32, "kernel_size": 3},
        {"expand_channels": 64, "kernel_size": 5},
    ):
        block_s2.set_sample_config(
            sample_expand_channels=cfg["expand_channels"],
            sample_kernel_size=cfg["kernel_size"],
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
    print(">>> All ElasticEfficientNetBlock Tests Passed!")
