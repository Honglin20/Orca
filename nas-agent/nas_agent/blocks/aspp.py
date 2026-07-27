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


class ElasticASPPBlock(nn.Module):
    """Atrous Spatial Pyramid Pooling block from DeepLab v3+.

    Aggregates multi-scale context via parallel branches:
      - 1x1 conv (local)
      - 3x3 dilated convs at each rate in `dilation_rates`
      - Global average pooling + 1x1 conv (global context)
    All branches are concatenated and fused through a final 1x1 conv.

    Elastic dimension: `sample_branch_channels` (width of every parallel
    branch). Number of branches is fixed at construction via `dilation_rates`.

    Stride or channel changes are handled by a shared stage transition
    (GroupNorm -> Conv2d, no activation/residual) before the ASPP body. The
    body then runs at `out_channels` with an identity residual addition and no
    post-add norm/activation.
    """

    def __init__(
        self,
        *,
        in_channels: int,
        out_channels: int,
        super_branch_channels: int,
        dilation_rates: tuple[int, ...] = (6, 12, 18),
        stride: int = 1,
        activation_layer: type[nn.Module] = nn.ReLU,
    ):
        super().__init__()
        if super_branch_channels <= 0:
            raise ValueError("super_branch_channels must be positive.")
        if not dilation_rates:
            raise ValueError("dilation_rates must not be empty.")

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.super_branch_channels = super_branch_channels
        self.dilation_rates = tuple(sorted(set(dilation_rates)))
        self.stride = stride
        self.sample_branch_channels = super_branch_channels

        self.downsample = make_cnn_stage_downsample(
            in_channels=in_channels,
            out_channels=out_channels,
            stride=stride,
        )

        # 1x1 conv branch
        self.conv1x1 = ElasticConv2d(
            super_in_channels=out_channels,
            super_out_channels=super_branch_channels,
            kernel_size=1,
            stride=1,
            padding=0,
            bias=False,
        )
        self.norm1x1 = ElasticGroupNorm2d(super_num_channels=super_branch_channels)

        # Dilated 3x3 branches (one per dilation rate)
        self.dil_convs = nn.ModuleList()
        self.dil_norms = nn.ModuleList()
        for _ in self.dilation_rates:
            self.dil_convs.append(
                ElasticConv2d(
                    super_in_channels=out_channels,
                    super_out_channels=super_branch_channels,
                    kernel_size=3,
                    stride=1,
                    padding=1,
                    bias=False,
                )
            )
            self.dil_norms.append(
                ElasticGroupNorm2d(super_num_channels=super_branch_channels)
            )

        # Global context branch: AdaptiveAvgPool + 1x1
        self.gap_conv = ElasticConv2d(
            super_in_channels=out_channels,
            super_out_channels=super_branch_channels,
            kernel_size=1,
            stride=1,
            padding=0,
            bias=False,
        )
        self.gap_norm = ElasticLayerNorm2d(super_num_channels=super_branch_channels)

        self.act = activation_layer()

        # Fusion: concat all branches -> out_channels
        num_branches = 1 + len(self.dilation_rates) + 1
        self.fuse_conv = ElasticConv2d(
            super_in_channels=num_branches * super_branch_channels,
            super_out_channels=out_channels,
            kernel_size=1,
            stride=1,
            padding=0,
            bias=False,
        )
        self.fuse_norm = GroupNorm2d(out_channels)
        self.fuse_act = activation_layer()

        self._num_branches = num_branches
        self.set_sample_config(sample_branch_channels=super_branch_channels)

    def set_sample_config(self, *, sample_branch_channels: int):
        if not (0 < sample_branch_channels <= self.super_branch_channels):
            raise ValueError(f"Unsupported branch channels: {sample_branch_channels}")
        self.sample_branch_channels = sample_branch_channels

        self.conv1x1.set_sample_config(
            sample_in_channels=self.out_channels,
            sample_out_channels=sample_branch_channels,
            sample_groups=1,
            sample_kernel_size=1,
        )
        self.norm1x1.set_sample_config(sample_num_channels=sample_branch_channels)

        for conv, norm in zip(self.dil_convs, self.dil_norms):
            conv.set_sample_config(
                sample_in_channels=self.out_channels,
                sample_out_channels=sample_branch_channels,
                sample_groups=1,
                sample_kernel_size=3,
            )
            norm.set_sample_config(sample_num_channels=sample_branch_channels)

        self.gap_conv.set_sample_config(
            sample_in_channels=self.out_channels,
            sample_out_channels=sample_branch_channels,
            sample_groups=1,
            sample_kernel_size=1,
        )
        self.gap_norm.set_sample_config(sample_num_channels=sample_branch_channels)

        self.fuse_conv.set_sample_config(
            sample_in_channels=self._num_branches * sample_branch_channels,
            sample_out_channels=self.out_channels,
            sample_groups=1,
            sample_kernel_size=1,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.downsample(x)
        h, w = x.shape[2], x.shape[3]

        branches = [self.act(self.norm1x1(self.conv1x1(x)))]

        for conv, norm, rate in zip(
            self.dil_convs, self.dil_norms, self.dilation_rates
        ):
            weight, bias = conv._get_active_weights()
            out = F.conv2d(
                x,
                weight.contiguous(),
                bias,
                stride=1,
                padding=rate,
                dilation=rate,
            )
            branches.append(self.act(norm(out)))

        gap = F.adaptive_avg_pool2d(x, 1)
        gap = self.act(self.gap_norm(self.gap_conv(gap)))
        gap = F.interpolate(gap, size=(h, w), mode="bilinear", align_corners=False)
        branches.append(gap)

        out = torch.cat(branches, dim=1)
        out = self.fuse_act(self.fuse_norm(self.fuse_conv(out)))
        return x + out

    def get_active_subnet(self) -> nn.Module:
        dilation_rates = self.dilation_rates
        sample_bch = self.sample_branch_channels
        in_ch = self.out_channels

        branch_modules = []

        # 1x1 branch
        w, b = self.conv1x1._get_active_weights()
        c1 = nn.Conv2d(
            in_ch,
            sample_bch,
            1,
            bias=b is not None,
            device=w.device,
            dtype=w.dtype,
        )
        with torch.no_grad():
            c1.weight.copy_(w)
            if b is not None:
                c1.bias.copy_(b)
        branch_modules.append(
            nn.Sequential(
                c1,
                self.norm1x1.get_active_subnet(),
                copy.deepcopy(self.act),
            )
        )

        # Dilated branches
        for conv, norm, rate in zip(self.dil_convs, self.dil_norms, dilation_rates):
            w, b = conv._get_active_weights()
            dc = nn.Conv2d(
                in_ch,
                sample_bch,
                3,
                stride=1,
                padding=rate,
                dilation=rate,
                bias=conv.bias is not None,
                device=w.device,
                dtype=w.dtype,
            )
            with torch.no_grad():
                dc.weight.copy_(w)
                if b is not None:
                    dc.bias.copy_(b)
            branch_modules.append(
                nn.Sequential(dc, norm.get_active_subnet(), copy.deepcopy(self.act))
            )

        # GAP branch
        w, b = self.gap_conv._get_active_weights()
        gc = nn.Conv2d(
            in_ch,
            sample_bch,
            1,
            bias=b is not None,
            device=w.device,
            dtype=w.dtype,
        )
        with torch.no_grad():
            gc.weight.copy_(w)
            if b is not None:
                gc.bias.copy_(b)
        gap_module = nn.Sequential(
            gc,
            self.gap_norm.get_active_subnet(),
            copy.deepcopy(self.act),
        )

        fuse = nn.Sequential(
            self.fuse_conv.get_active_subnet(),
            copy.deepcopy(self.fuse_norm),
            copy.deepcopy(self.fuse_act),
        )
        downsample = copy.deepcopy(self.downsample)

        class SubnetASPP(nn.Module):
            def __init__(
                self,
                downsample,
                branches,
                gap_branch,
                fuse,
            ):
                super().__init__()
                self.downsample = downsample
                self.branches = nn.ModuleList(branches)
                self.gap_branch = gap_branch
                self.fuse = fuse

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                x = self.downsample(x)
                h, w = x.shape[2], x.shape[3]
                outs = [branch(x) for branch in self.branches]
                gap = F.adaptive_avg_pool2d(x, 1)
                gap = F.interpolate(
                    self.gap_branch(gap),
                    size=(h, w),
                    mode="bilinear",
                    align_corners=False,
                )
                outs.append(gap)
                out = self.fuse(torch.cat(outs, dim=1))
                return x + out

        return SubnetASPP(
            downsample,
            branch_modules,
            gap_module,
            fuse,
        )

    @property
    def elastic_num_params(self) -> int:
        params = (
            sum(p.numel() for p in self.downsample.parameters())
            + self.conv1x1.elastic_num_params
            + self.norm1x1.elastic_num_params
            + sum(
                c.elastic_num_params + b.elastic_num_params
                for c, b in zip(self.dil_convs, self.dil_norms)
            )
            + self.gap_conv.elastic_num_params
            + self.gap_norm.elastic_num_params
            + self.fuse_conv.elastic_num_params
            + sum(p.numel() for p in self.fuse_norm.parameters())
        )
        return params


def is_valid_aspp_block(layer_config: dict[str, Any]) -> bool:
    branch_channels = layer_config.get("branch_channels")
    return isinstance(branch_channels, int) and branch_channels > 0


if __name__ == "__main__":
    torch.manual_seed(42)

    # --- stride=1 tests ---
    block = ElasticASPPBlock(
        in_channels=256,
        out_channels=256,
        super_branch_channels=64,
        dilation_rates=(6, 12, 18),
    ).eval()
    print("[Init] ASPPBlock in=256 out=256 stride=1")
    for bch in (32, 64):
        block.set_sample_config(sample_branch_channels=bch)
        x = torch.randn(2, 256, 32, 32)
        with torch.no_grad():
            y = block(x)
            y_sub = block.get_active_subnet().eval()(x)
        diff = (y - y_sub).abs().max().item()
        assert diff < 1e-5, f"Consistency: {diff}"
        assert y.shape == (2, 256, 32, 32), y.shape
        print(f"  [Pass] bch={bch} out={tuple(y.shape)}")

    # --- stride=2 tests ---
    block_s2 = ElasticASPPBlock(
        in_channels=256,
        out_channels=256,
        super_branch_channels=64,
        dilation_rates=(6, 12, 18),
        stride=2,
    ).eval()
    print("[Init] ASPPBlock in=256 out=256 stride=2")
    for bch in (32, 64):
        block_s2.set_sample_config(sample_branch_channels=bch)
        x = torch.randn(2, 256, 32, 32)
        with torch.no_grad():
            y = block_s2(x)
            y_sub = block_s2.get_active_subnet().eval()(x)
        diff = (y - y_sub).abs().max().item()
        assert diff < 1e-5, f"Consistency: {diff}"
        assert y.shape == (2, 256, 16, 16), y.shape
        print(f"  [Pass] bch={bch} out={tuple(y.shape)}")

    print(f"[Params] Active params: {block.elastic_num_params}")
    print(">>> All ElasticASPPBlock Tests Passed!")
