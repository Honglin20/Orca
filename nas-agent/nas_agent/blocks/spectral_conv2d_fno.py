import copy
import math
from typing import Any

import torch
import torch.nn as nn

from .common import make_cnn_stage_downsample
from .primitive_blocks import ElasticConv2d, ElasticGroupNorm2d, GroupNorm2d


def _rfft_scale(freq: torch.Tensor, full_length: int) -> torch.Tensor:
    scale = torch.full(freq.shape, 2.0, device=freq.device, dtype=torch.float32)
    scale = torch.where(freq == 0, torch.ones_like(scale), scale)
    if full_length % 2 == 0:
        scale = torch.where(freq == full_length // 2, torch.ones_like(scale), scale)
    return scale


def _build_dft2d_basis(
    height: int,
    width: int,
    modes_h: int,
    modes_w: int,
    device: torch.device,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Pre-compute 2-D DFT basis matrices and rfft scale vector.

    The basis columns are stored in 2-D frequency layout
    `(H*W, modes_h, modes_w)` so that sub-mode selection is a simple
    `[:, :active_h, :active_w]` slice.

    Returns:
        cos: shape `(height * width, modes_h, modes_w)`
        sin: shape `(height * width, modes_h, modes_w)`
        scale: shape `(modes_w,)` — only depends on width-axis frequencies
    """
    y = torch.arange(height, device=device, dtype=torch.float32)
    x = torch.arange(width, device=device, dtype=torch.float32)
    fh = torch.arange(modes_h, device=device, dtype=torch.float32)
    fw = torch.arange(modes_w, device=device, dtype=torch.float32)

    # angle[y, x, fh, fw] = 2π (y·fh/H + x·fw/W)
    angle_h = (
        2.0 * math.pi * y[:, None, None, None] * fh[None, None, :, None] / float(height)
    )
    angle_w = (
        2.0 * math.pi * x[None, :, None, None] * fw[None, None, None, :] / float(width)
    )
    angle = (angle_h + angle_w).reshape(height * width, modes_h, modes_w)
    cos = torch.cos(angle)
    sin = torch.sin(angle)
    scale = _rfft_scale(fw, width)
    return cos, sin, scale


def _real_dft_spectral_conv2d(
    x: torch.Tensor,
    weight: torch.Tensor,
    modes: int,
    basis_cos: torch.Tensor | None = None,
    basis_sin: torch.Tensor | None = None,
    basis_scale: torch.Tensor | None = None,
) -> torch.Tensor:
    """Real-valued equivalent of rfft2 -> complex matmul -> irfft2."""
    _, _, height, width = x.shape
    active_h = min(modes, height)
    active_w = min(modes, width // 2 + 1)
    n_modes = active_h * active_w
    dtype = x.dtype
    x_float = x.float()
    norm = torch.rsqrt(
        torch.full((), float(height * width), device=x.device, dtype=torch.float32)
    )

    # Use cached basis or compute on the fly
    if basis_cos is not None and basis_sin is not None and basis_scale is not None:
        # basis_cos: (H*W, max_h, max_w) -> slice to (H*W, active_h, active_w)
        cos = basis_cos[:, :active_h, :active_w].reshape(height * width, n_modes)
        sin = basis_sin[:, :active_h, :active_w].reshape(height * width, n_modes)
        scale = (
            basis_scale[:active_w].unsqueeze(0).expand(active_h, -1).reshape(n_modes)
        )
    else:
        cos_3d, sin_3d, scale_1d = _build_dft2d_basis(
            height,
            width,
            active_h,
            active_w,
            x.device,
        )
        cos = cos_3d.reshape(height * width, n_modes)
        sin = sin_3d.reshape(height * width, n_modes)
        scale = scale_1d.unsqueeze(0).expand(active_h, -1).reshape(n_modes)

    x_flat = x_float.flatten(2)
    x_re = torch.matmul(x_flat, cos).transpose(1, 2) * norm
    x_im = -torch.matmul(x_flat, sin).transpose(1, 2) * norm
    wr = weight[:, :, :active_h, :active_w, 0].float().permute(2, 3, 0, 1)
    wi = weight[:, :, :active_h, :active_w, 1].float().permute(2, 3, 0, 1)
    wr = wr.reshape(n_modes, weight.shape[0], weight.shape[1])
    wi = wi.reshape(n_modes, weight.shape[0], weight.shape[1])
    y_re = torch.matmul(x_re.unsqueeze(-2), wr.unsqueeze(0)).squeeze(-2)
    y_re = y_re - torch.matmul(x_im.unsqueeze(-2), wi.unsqueeze(0)).squeeze(-2)
    y_im = torch.matmul(x_re.unsqueeze(-2), wi.unsqueeze(0)).squeeze(-2)
    y_im = y_im + torch.matmul(x_im.unsqueeze(-2), wr.unsqueeze(0)).squeeze(-2)

    y_re = y_re * scale.view(1, n_modes, 1)
    y_im = y_im * scale.view(1, n_modes, 1)
    y = torch.matmul(y_re.transpose(1, 2), cos.transpose(0, 1))
    y = y - torch.matmul(y_im.transpose(1, 2), sin.transpose(0, 1))
    y = y.reshape(x.shape[0], weight.shape[1], height, width)
    return (y * norm).to(dtype)


class SpectralConv2d(nn.Module):
    """Materialized active spectral convolution.

    Its weight axes are ``[in_channels, out_channels, mode_h, mode_w, complex]``;
    the explicit channel attributes let pruning code avoid guessing those axes.
    """

    def __init__(self, weight: torch.Tensor, *, modes: int):
        super().__init__()
        self.weight = nn.Parameter(weight.clone())
        self.in_channels = weight.size(0)
        self.out_channels = weight.size(1)
        self.modes = modes
        self._cached_height = 0
        self._cached_width = 0
        self.register_buffer("_basis_cos", torch.empty(0), persistent=False)
        self.register_buffer("_basis_sin", torch.empty(0), persistent=False)
        self.register_buffer("_basis_scale", torch.empty(0), persistent=False)

    def _ensure_basis(
        self,
        height: int,
        width: int,
        device: torch.device,
    ) -> None:
        if (
            height == self._cached_height
            and width == self._cached_width
            and self._basis_cos.device == device
        ):
            return
        max_h = min(self.modes, height)
        max_w = min(self.modes, width // 2 + 1)
        cos, sin, scale = _build_dft2d_basis(
            height, width, max_h, max_w, device
        )
        self._basis_cos = cos
        self._basis_sin = sin
        self._basis_scale = scale
        self._cached_height = height
        self._cached_width = width

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        _, _, height, width = x.shape
        if x.size(1) != self.in_channels:
            raise ValueError(
                f"Expected {self.in_channels} channels, got {x.size(1)}."
            )
        self._ensure_basis(height, width, x.device)
        return _real_dft_spectral_conv2d(
            x,
            self.weight,
            self.modes,
            self._basis_cos,
            self._basis_sin,
            self._basis_scale,
        )


class ElasticSpectralConv2d(nn.Module):
    """2-D FNO-style spectral convolution over NCHW feature maps.

    DFT basis matrices are lazily built on the first forward call and
    cached as non-persistent buffers in shape `(H*W, max_h, max_w)`.
    Changing `sample_modes` only requires slicing the cached buffers
    (no re-computation unless the spatial resolution changes).
    """

    def __init__(self, *, super_channels: int, super_modes: int):
        super().__init__()
        if super_channels <= 0:
            raise ValueError("super_channels must be positive.")
        if super_modes <= 0:
            raise ValueError("super_modes must be positive.")
        self.super_channels = super_channels
        self.sample_channels = super_channels
        self.super_modes = super_modes
        self.sample_modes = super_modes
        scale = 1.0 / max(1, super_channels)
        self.weight = nn.Parameter(
            torch.randn(super_channels, super_channels, super_modes, super_modes, 2)
            * scale
        )
        # Placeholders – filled lazily on first forward
        self._cached_height: int = 0
        self._cached_width: int = 0
        self.register_buffer("_basis_cos", torch.empty(0), persistent=False)
        self.register_buffer("_basis_sin", torch.empty(0), persistent=False)
        self.register_buffer("_basis_scale", torch.empty(0), persistent=False)

    def _ensure_basis(
        self,
        height: int,
        width: int,
        device: torch.device,
    ) -> None:
        """Rebuild DFT basis buffers when spatial dimensions change."""
        if (
            height == self._cached_height
            and width == self._cached_width
            and self._basis_cos.device == device
        ):
            return
        max_h = min(self.super_modes, height)
        max_w = min(self.super_modes, width // 2 + 1)
        cos, sin, scale = _build_dft2d_basis(height, width, max_h, max_w, device)
        self._basis_cos = cos
        self._basis_sin = sin
        self._basis_scale = scale
        self._cached_height = height
        self._cached_width = width

    def set_sample_config(self, *, sample_modes: int, sample_channels: int):
        if sample_channels <= 0:
            raise ValueError("sample_channels must be positive.")
        if sample_channels > self.super_channels:
            raise ValueError("sample_channels cannot exceed super_channels.")
        if sample_modes <= 0:
            raise ValueError("sample_modes must be positive.")
        if sample_modes > self.super_modes:
            raise ValueError("sample_modes cannot exceed super_modes.")
        self.sample_channels = sample_channels
        self.sample_modes = sample_modes

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        _, _, height, width = x.shape
        if x.size(1) != self.sample_channels:
            raise ValueError(
                f"Expected {self.sample_channels} channels, got {x.size(1)}."
            )
        self._ensure_basis(height, width, x.device)
        weight = self.weight[
            : self.sample_channels,
            : self.sample_channels,
            : self.sample_modes,
            : self.sample_modes,
        ]
        return _real_dft_spectral_conv2d(
            x,
            weight,
            self.sample_modes,
            self._basis_cos,
            self._basis_sin,
            self._basis_scale,
        )

    def get_active_subnet(self) -> nn.Module:
        modes = self.sample_modes
        channels = self.sample_channels
        return SpectralConv2d(
            self.weight[:channels, :channels, :modes, :modes], modes=modes
        )

    @property
    def elastic_num_params(self):
        return (
            2
            * self.sample_channels
            * self.sample_channels
            * self.sample_modes
            * self.sample_modes
        )


class ElasticSpectralConv2DFNOBlock(nn.Module):
    """2-D spectral CNN block with optional stride downsampling."""

    def __init__(
        self,
        *,
        super_modes: int,
        super_spectral_channels: int,
        in_channels: int,
        out_channels: int,
        stride: int = 1,
        activation_layer: type[nn.Module] = nn.GELU,
    ):
        super().__init__()
        if super_modes <= 0:
            raise ValueError("super_modes must be positive.")
        if super_spectral_channels <= 0:
            raise ValueError("super_spectral_channels must be positive.")
        if stride < 1:
            raise ValueError("stride must be >= 1.")
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.stride = stride
        self.super_modes = super_modes
        self.super_spectral_channels = super_spectral_channels
        self.sample_modes = super_modes
        self.sample_spectral_channels = super_spectral_channels

        self.downsample = make_cnn_stage_downsample(
            in_channels=in_channels,
            out_channels=out_channels,
            stride=stride,
        )
        self.pre_proj = ElasticConv2d(
            super_in_channels=out_channels,
            super_out_channels=super_spectral_channels,
            kernel_size=1,
            stride=1,
            padding=0,
            bias=False,
        )
        self.pre_norm = ElasticGroupNorm2d(super_num_channels=super_spectral_channels)
        self.spectral = ElasticSpectralConv2d(
            super_channels=super_spectral_channels,
            super_modes=super_modes,
        )
        self.local_proj = ElasticConv2d(
            super_in_channels=super_spectral_channels,
            super_out_channels=super_spectral_channels,
            kernel_size=1,
            stride=1,
            padding=0,
            bias=False,
        )
        self.post_proj = ElasticConv2d(
            super_in_channels=super_spectral_channels,
            super_out_channels=out_channels,
            kernel_size=1,
            stride=1,
            padding=0,
            bias=False,
        )
        self.out_norm = GroupNorm2d(out_channels)
        self.act = activation_layer()
        self.set_sample_config(
            sample_modes=super_modes,
            sample_spectral_channels=super_spectral_channels,
        )

    def set_sample_config(self, *, sample_modes: int, sample_spectral_channels: int):
        if not (0 < sample_modes <= self.super_modes):
            raise ValueError(f"Unsupported modes: {sample_modes}")
        if not (0 < sample_spectral_channels <= self.super_spectral_channels):
            raise ValueError(
                f"Unsupported spectral_channels: {sample_spectral_channels}"
            )
        self.sample_modes = sample_modes
        self.sample_spectral_channels = sample_spectral_channels
        self.pre_proj.set_sample_config(
            sample_in_channels=self.out_channels,
            sample_out_channels=sample_spectral_channels,
            sample_groups=1,
            sample_kernel_size=1,
        )
        self.pre_norm.set_sample_config(sample_num_channels=sample_spectral_channels)
        self.spectral.set_sample_config(
            sample_modes=sample_modes,
            sample_channels=sample_spectral_channels,
        )
        self.local_proj.set_sample_config(
            sample_in_channels=sample_spectral_channels,
            sample_out_channels=sample_spectral_channels,
            sample_groups=1,
            sample_kernel_size=1,
        )
        self.post_proj.set_sample_config(
            sample_in_channels=sample_spectral_channels,
            sample_out_channels=self.out_channels,
            sample_groups=1,
            sample_kernel_size=1,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.downsample(x)
        h = self.pre_norm(self.pre_proj(x))
        out = self.spectral(h) + self.local_proj(h)
        out = self.act(self.out_norm(self.post_proj(out)))
        return x + out

    def get_active_subnet(self) -> nn.Module:
        class SubnetSpectral2DBlock(nn.Module):
            def __init__(self, downsample, main_path):
                super().__init__()
                self.downsample = downsample
                self.main_path = main_path

            def forward(self, x):
                x = self.downsample(x)
                out = self.main_path(x)
                return x + out

        class SpectralMain(nn.Module):
            def __init__(
                self,
                pre_proj,
                pre_norm,
                spectral,
                local_proj,
                post_proj,
                out_norm,
                act,
            ):
                super().__init__()
                self.pre_proj = pre_proj
                self.pre_norm = pre_norm
                self.spectral = spectral
                self.local_proj = local_proj
                self.post_proj = post_proj
                self.out_norm = out_norm
                self.act = act

            def forward(self, x):
                h = self.pre_norm(self.pre_proj(x))
                out = self.spectral(h) + self.local_proj(h)
                return self.act(self.out_norm(self.post_proj(out)))

        return SubnetSpectral2DBlock(
            copy.deepcopy(self.downsample),
            SpectralMain(
                self.pre_proj.get_active_subnet(),
                self.pre_norm.get_active_subnet(),
                self.spectral.get_active_subnet(),
                self.local_proj.get_active_subnet(),
                self.post_proj.get_active_subnet(),
                copy.deepcopy(self.out_norm),
                copy.deepcopy(self.act),
            ),
        )

    @property
    def elastic_num_params(self):
        params = (
            sum(p.numel() for p in self.downsample.parameters())
            + self.pre_proj.elastic_num_params
            + self.pre_norm.elastic_num_params
            + self.spectral.elastic_num_params
            + self.local_proj.elastic_num_params
            + self.post_proj.elastic_num_params
            + sum(p.numel() for p in self.out_norm.parameters())
        )
        return params


def is_valid_spectral_conv2d_fno_block(layer_config: dict[str, Any]) -> bool:
    modes = layer_config.get("modes")
    spectral_channels = layer_config.get("spectral_channels")
    return (
        isinstance(modes, int)
        and modes > 0
        and isinstance(spectral_channels, int)
        and spectral_channels > 0
    )


if __name__ == "__main__":
    torch.manual_seed(42)
    block = ElasticSpectralConv2DFNOBlock(
        super_modes=8,
        super_spectral_channels=32,
        in_channels=24,
        out_channels=24,
    ).eval()

    for modes, spectral_channels in ((4, 16), (8, 32)):
        block.set_sample_config(
            sample_modes=modes,
            sample_spectral_channels=spectral_channels,
        )
        x = torch.randn(2, 24, 16, 16)
        with torch.no_grad():
            y = block(x)
            y_sub = block.get_active_subnet().eval()(x)
        diff = (y - y_sub).abs().max().item()
        print(
            f"  [Pass] modes={modes}, spectral_channels={spectral_channels}, "
            f"output={tuple(y.shape)}, diff={diff:.2e}"
        )
        assert y.shape == x.shape
        assert diff < 1e-5

    block_s2 = ElasticSpectralConv2DFNOBlock(
        super_modes=8,
        super_spectral_channels=32,
        in_channels=24,
        out_channels=48,
        stride=2,
    ).eval()

    for modes, spectral_channels in ((4, 16), (8, 32)):
        block_s2.set_sample_config(
            sample_modes=modes,
            sample_spectral_channels=spectral_channels,
        )
        x = torch.randn(2, 24, 16, 16)
        with torch.no_grad():
            y = block_s2(x)
            y_sub = block_s2.get_active_subnet().eval()(x)
        diff = (y - y_sub).abs().max().item()
        print(
            f"  [Pass] stride=2 modes={modes}, "
            f"spectral_channels={spectral_channels}, output={tuple(y.shape)}, "
            f"diff={diff:.2e}"
        )
        assert y.shape == (2, 48, 8, 8)
        assert diff < 1e-5

    print(">>> All ElasticSpectralConv2DFNOBlock Tests Passed!")
