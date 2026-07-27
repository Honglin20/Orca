import copy
from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F

from .common import make_cnn_stage_downsample
from .primitive_blocks import ElasticConv2d, ElasticGroupNorm2d, GroupNorm2d


class ElasticDilatedConvBlock(nn.Module):
    """Conv-Norm-Act with elastic kernel size and elastic dilation rate.

    The same weight tensor is shared across all dilation rates — only the
    receptive-field spacing changes.  Elastic dimensions: `sample_kernel_size`,
    `sample_dilation`, and `sample_expand_channels`.  Useful for segmentation and dense-prediction backbones
    (DeepLab, PSPNet, HRNet).
    """

    def __init__(
        self,
        *,
        in_channels: int,
        out_channels: int,
        super_expand_channels: int | None = None,
        candidate_kernel_sizes: tuple[int, ...] = (3,),
        candidate_dilations: tuple[int, ...] = (1, 2, 4),
        stride: int = 1,
        activation_layer: type[nn.Module] = nn.ReLU,
    ):
        super().__init__()
        if not candidate_kernel_sizes:
            raise ValueError("candidate_kernel_sizes must not be empty.")
        if not candidate_dilations:
            raise ValueError("candidate_dilations must not be empty.")
        if super_expand_channels is None:
            super_expand_channels = out_channels
        if super_expand_channels <= 0:
            raise ValueError("super_expand_channels must be positive.")

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.super_expand_channels = super_expand_channels
        self.stride = stride
        self.candidate_kernel_sizes = tuple(sorted(set(candidate_kernel_sizes)))
        self.candidate_dilations = tuple(sorted(set(candidate_dilations)))
        self.active_kernel_size = max(self.candidate_kernel_sizes)
        self.active_dilation = max(self.candidate_dilations)
        self.sample_expand_channels = super_expand_channels

        self.downsample = make_cnn_stage_downsample(
            in_channels=in_channels,
            out_channels=out_channels,
            stride=stride,
        )
        # Forward applies active dilation directly; initialize with largest same-padding.
        self.conv = ElasticConv2d(
            super_in_channels=out_channels,
            super_out_channels=super_expand_channels,
            kernel_size=max(self.candidate_kernel_sizes),
            stride=1,
            padding=max(self.candidate_dilations)
            * (max(self.candidate_kernel_sizes) // 2),
            dilation=max(self.candidate_dilations),
            bias=False,
            candidate_kernel_sizes=self.candidate_kernel_sizes,
        )
        self.norm = ElasticGroupNorm2d(super_num_channels=super_expand_channels)
        self.act = activation_layer()
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
            sample_kernel_size=self.active_kernel_size,
            sample_dilation=self.active_dilation,
            sample_expand_channels=self.sample_expand_channels,
        )

    def set_sample_config(
        self,
        *,
        sample_kernel_size: int,
        sample_dilation: int,
        sample_expand_channels: int | None = None,
    ):
        if sample_kernel_size not in self.candidate_kernel_sizes:
            raise ValueError(f"Unsupported kernel size: {sample_kernel_size}")
        if sample_dilation not in self.candidate_dilations:
            raise ValueError(f"Unsupported dilation: {sample_dilation}")
        if sample_expand_channels is None:
            sample_expand_channels = self.sample_expand_channels
        if not (0 < sample_expand_channels <= self.super_expand_channels):
            raise ValueError(f"Unsupported expand channels: {sample_expand_channels}")

        self.active_kernel_size = sample_kernel_size
        self.active_dilation = sample_dilation
        self.sample_expand_channels = sample_expand_channels

        self.conv.set_sample_config(
            sample_in_channels=self.out_channels,
            sample_out_channels=sample_expand_channels,
            sample_groups=1,
            sample_kernel_size=sample_kernel_size,
        )
        self.norm.set_sample_config(sample_num_channels=sample_expand_channels)
        self.pointwise.set_sample_config(
            sample_in_channels=sample_expand_channels,
            sample_out_channels=self.out_channels,
            sample_groups=1,
            sample_kernel_size=1,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.downsample(x)
        # Bypass ElasticConv2d's fixed padding/dilation; apply active dilation directly.
        weight, bias = self.conv._get_active_weights()
        padding = self.active_dilation * (self.active_kernel_size // 2)
        out = F.conv2d(
            x,
            weight.contiguous(),
            bias,
            stride=1,
            padding=padding,
            dilation=self.active_dilation,
        )
        out = self.norm(out)
        out = self.act(out)
        out = self.pointwise(out)
        out = self.out_norm(out)
        out = self.out_act(out)
        return x + out

    def get_active_subnet(self) -> nn.Module:
        active_ks = self.active_kernel_size
        active_dil = self.active_dilation
        padding = active_dil * (active_ks // 2)
        weight, bias = self.conv._get_active_weights()

        active_conv = nn.Conv2d(
            self.out_channels,
            self.sample_expand_channels,
            active_ks,
            stride=1,
            padding=padding,
            dilation=active_dil,
            bias=self.conv.bias is not None,
            device=weight.device,
            dtype=weight.dtype,
        )
        with torch.no_grad():
            active_conv.weight.copy_(weight)
            if bias is not None:
                active_conv.bias.copy_(bias)

        class SubnetDilatedConvBlock(nn.Module):
            def __init__(
                self,
                downsample: nn.Module,
                conv: nn.Module,
                norm: nn.Module,
                act: nn.Module,
                pointwise: nn.Module,
                out_norm: nn.Module,
                out_act: nn.Module,
            ):
                super().__init__()
                self.downsample = downsample
                self.conv = conv
                self.norm = norm
                self.act = act
                self.pointwise = pointwise
                self.out_norm = out_norm
                self.out_act = out_act

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                x = self.downsample(x)
                out = self.act(self.norm(self.conv(x)))
                out = self.out_act(self.out_norm(self.pointwise(out)))
                return x + out

        return SubnetDilatedConvBlock(
            copy.deepcopy(self.downsample),
            active_conv,
            self.norm.get_active_subnet(),
            copy.deepcopy(self.act),
            self.pointwise.get_active_subnet(),
            copy.deepcopy(self.out_norm),
            copy.deepcopy(self.out_act),
        )

    @property
    def elastic_num_params(self) -> int:
        params = (
            self.conv.elastic_num_params
            + self.norm.elastic_num_params
            + self.pointwise.elastic_num_params
            + sum(p.numel() for p in self.out_norm.parameters())
        )
        params += sum(p.numel() for p in self.downsample.parameters())
        return params


def is_valid_dilated_conv_block(layer_config: dict[str, Any]) -> bool:
    kernel_size = layer_config.get("kernel_size")
    dilation = layer_config.get("dilation")
    expand_channels = layer_config.get("expand_channels")
    return (
        isinstance(kernel_size, int)
        and kernel_size > 0
        and kernel_size % 2 == 1
        and isinstance(dilation, int)
        and dilation > 0
        and isinstance(expand_channels, int)
        and expand_channels > 0
    )


if __name__ == "__main__":
    torch.manual_seed(42)

    # --- stride=1 tests (backward compatibility) ---
    block = ElasticDilatedConvBlock(
        in_channels=32,
        out_channels=32,
        super_expand_channels=64,
        candidate_kernel_sizes=(3, 5),
        candidate_dilations=(1, 2, 4),
    ).eval()

    print(
        "[Init] DilatedConvBlock in=32 out=32 kernels=(3,5) dilations=(1,2,4) stride=1"
    )

    for ks in (3, 5):
        for dil in (1, 2, 4):
            expand_channels = 32 if ks == 3 else 64
            block.set_sample_config(
                sample_kernel_size=ks,
                sample_dilation=dil,
                sample_expand_channels=expand_channels,
            )
            x = torch.randn(2, 32, 32, 32)
            with torch.no_grad():
                y = block(x)
                y_sub = block.get_active_subnet().eval()(x)
            diff = (y - y_sub).abs().max().item()
            assert diff < 1e-5, f"Consistency check failed: {diff}"
            assert y.shape == (2, 32, 32, 32), y.shape
            print(
                f"  [Pass] kernel={ks} dilation={dil} expand_channels={expand_channels} output={tuple(y.shape)}"
            )

    # --- stride=2 tests ---
    block_s2 = ElasticDilatedConvBlock(
        in_channels=32,
        out_channels=64,
        super_expand_channels=96,
        candidate_kernel_sizes=(3, 5),
        candidate_dilations=(1, 2, 4),
        stride=2,
    ).eval()

    print(
        "[Init] DilatedConvBlock in=32 out=64 kernels=(3,5) dilations=(1,2,4) stride=2"
    )

    for ks in (3, 5):
        for dil in (1, 2, 4):
            expand_channels = 64 if ks == 3 else 96
            block_s2.set_sample_config(
                sample_kernel_size=ks,
                sample_dilation=dil,
                sample_expand_channels=expand_channels,
            )
            x = torch.randn(2, 32, 32, 32)
            with torch.no_grad():
                y = block_s2(x)
                y_sub = block_s2.get_active_subnet().eval()(x)
            diff = (y - y_sub).abs().max().item()
            assert diff < 1e-5, f"Consistency check failed: {diff}"
            assert y.shape == (2, 64, 16, 16), f"Expected (2,64,16,16), got {y.shape}"
            print(
                f"  [Pass] kernel={ks} dilation={dil} expand_channels={expand_channels} output={tuple(y.shape)}"
            )

    print(f"[Params] Active params: {block.elastic_num_params}")
    print(">>> All ElasticDilatedConvBlock Tests Passed!")
