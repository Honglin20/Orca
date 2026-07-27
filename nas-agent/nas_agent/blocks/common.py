import torch
import torch.nn as nn
import torch.nn.functional as F

from .primitive_blocks import GroupNorm2d



class CNNStageDownsample2d(nn.Module):
    """NCHW stage downsample for pre-norm NAS backbones.

    Recommended latency-first default:
        GroupNorm(C_in) -> Conv2d(C_in, C_out, k=2, s=2)

    Accuracy/dense-prediction option:
        GroupNorm(C_in) -> Conv2d(C_in, C_out, k=3, s=2, p=1)

    I/O: (B, C_in, H, W) -> (B, C_out, ceil(H/2), ceil(W/2))
    """

    def __init__(
        self,
        *,
        in_channels: int,
        out_channels: int,
        stride: int = 2,
        kernel_size: int = 2,
        conv_bias: bool = False,
        pad_odd_input: bool = True,
    ):
        super().__init__()
        if stride < 1:
            raise ValueError("stride must be >= 1.")
        if kernel_size not in (2, 3):
            raise ValueError("kernel_size must be 2 or 3.")

        self.stride = stride
        self.kernel_size = kernel_size
        self.pad_odd_input = pad_odd_input
        padding = 1 if kernel_size == 3 else 0

        self.norm = GroupNorm2d(in_channels)
        self.proj = nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            bias=conv_bias,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.norm(x)
        if self.kernel_size == 2 and self.stride > 1 and self.pad_odd_input:
            pad_h = (-x.size(2)) % self.stride
            pad_w = (-x.size(3)) % self.stride
            if pad_h or pad_w:
                x = F.pad(x, (0, pad_w, 0, pad_h))
        return self.proj(x)


class TransformerStageDownsample2d(nn.Module):
    """BHWC stage downsample for pre-norm NAS backbones.

    Recommended latency-first default:
        LayerNorm(C_in) -> Conv2d(C_in, C_out, k=2, s=2)

    Accuracy/dense-prediction option:
        LayerNorm(C_in) -> Conv2d(C_in, C_out, k=3, s=2, p=1)

    I/O: (B, H, W, C_in) -> (B, ceil(H/2), ceil(W/2), C_out)
    Conv2d is applied on a temporarily permuted BCHW view.
    """

    def __init__(
        self,
        *,
        in_channels: int,
        out_channels: int,
        stride: int = 2,
        kernel_size: int = 2,
        conv_bias: bool = True,
        norm_eps: float = 1e-6,
        pad_odd_input: bool = True,
    ):
        super().__init__()
        if stride < 1:
            raise ValueError("stride must be >= 1.")
        if kernel_size not in (2, 3):
            raise ValueError("kernel_size must be 2 or 3.")

        self.stride = stride
        self.kernel_size = kernel_size
        self.pad_odd_input = pad_odd_input
        padding = 1 if kernel_size == 3 else 0

        self.norm = nn.LayerNorm(in_channels, eps=norm_eps)
        self.proj = nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            bias=conv_bias,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.norm(x)
        x = x.permute(0, 3, 1, 2).contiguous()
        if self.kernel_size == 2 and self.stride > 1 and self.pad_odd_input:
            pad_h = (-x.size(2)) % self.stride
            pad_w = (-x.size(3)) % self.stride
            if pad_h or pad_w:
                x = F.pad(x, (0, pad_w, 0, pad_h))
        x = self.proj(x)
        return x.permute(0, 2, 3, 1).contiguous()


def make_cnn_stage_downsample(
    *,
    in_channels: int,
    out_channels: int,
    stride: int,
) -> nn.Module:
    if stride == 1 and in_channels == out_channels:
        return nn.Identity()
    return CNNStageDownsample2d(
        in_channels=in_channels,
        out_channels=out_channels,
        stride=stride,
    )

