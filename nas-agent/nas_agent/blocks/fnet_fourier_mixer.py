import math
from typing import Any

import torch
import torch.nn as nn

from .primitive_blocks import ElasticLayerNorm, ElasticLinear

LAYER_NORM_EPSILON = 1e-12


def _build_dft_basis(
    size: int, device: torch.device
) -> tuple[torch.Tensor, torch.Tensor]:
    """Build the real/imaginary basis of an unnormalized DFT matrix."""
    idx = torch.arange(size, device=device, dtype=torch.float32)
    angle = 2.0 * math.pi * idx[:, None] * idx[None, :] / float(size)
    return torch.cos(angle), torch.sin(angle)


def _real_2d_dft(
    x: torch.Tensor,
    seq_cos: torch.Tensor,
    seq_sin: torch.Tensor,
    hidden_cos: torch.Tensor,
    hidden_sin: torch.Tensor,
) -> torch.Tensor:
    """Real component of F_L @ x @ F_C for real-valued BLC inputs.

    This is equivalent to `torch.fft.fftn(x, dim=(-2, -1)).real` with the
    default unnormalized DFT convention, but uses matmul-only ops that fit the
    repository's ONNX/NPU export path better.
    """
    dtype = x.dtype
    x_float = x.float()
    real = torch.matmul(torch.matmul(seq_cos, x_float), hidden_cos)
    real = real - torch.matmul(torch.matmul(seq_sin, x_float), hidden_sin)
    return real.to(dtype)


class ElasticFNetFourierTransform(nn.Module):
    """FNet Fourier transform mixer for native BLC sequence tensors.

    The official FNet implementation applies a 2-D Fourier transform over the
    final two dimensions, usually `[max_seq_length, hidden_dim]`, and returns
    the real component. This PyTorch version keeps the same BLC contract and
    uses the DFT-matrix path that is also provided by the official code.
    """

    def __init__(self):
        super().__init__()
        self._cached_length: int = 0
        self._cached_hidden: int = 0
        self.register_buffer("_seq_cos", torch.empty(0), persistent=False)
        self.register_buffer("_seq_sin", torch.empty(0), persistent=False)
        self.register_buffer("_hidden_cos", torch.empty(0), persistent=False)
        self.register_buffer("_hidden_sin", torch.empty(0), persistent=False)

    def _ensure_basis(
        self, length: int, hidden_size: int, device: torch.device
    ) -> None:
        if (
            length == self._cached_length
            and hidden_size == self._cached_hidden
            and self._seq_cos.device == device
        ):
            return
        self._seq_cos, self._seq_sin = _build_dft_basis(length, device)
        self._hidden_cos, self._hidden_sin = _build_dft_basis(hidden_size, device)
        self._cached_length = length
        self._cached_hidden = hidden_size

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        _, length, hidden_size = x.shape
        self._ensure_basis(length, hidden_size, x.device)
        return _real_2d_dft(
            x,
            self._seq_cos,
            self._seq_sin,
            self._hidden_cos,
            self._hidden_sin,
        )

    def get_active_subnet(self) -> nn.Module:
        class FNetFourierTransform(nn.Module):
            def __init__(self):
                super().__init__()
                self._cached_length: int = 0
                self._cached_hidden: int = 0
                self.register_buffer("_seq_cos", torch.empty(0), persistent=False)
                self.register_buffer("_seq_sin", torch.empty(0), persistent=False)
                self.register_buffer("_hidden_cos", torch.empty(0), persistent=False)
                self.register_buffer("_hidden_sin", torch.empty(0), persistent=False)

            def _ensure_basis(
                self, length: int, hidden_size: int, device: torch.device
            ) -> None:
                if (
                    length == self._cached_length
                    and hidden_size == self._cached_hidden
                    and self._seq_cos.device == device
                ):
                    return
                self._seq_cos, self._seq_sin = _build_dft_basis(length, device)
                self._hidden_cos, self._hidden_sin = _build_dft_basis(
                    hidden_size, device
                )
                self._cached_length = length
                self._cached_hidden = hidden_size

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                _, length, hidden_size = x.shape
                self._ensure_basis(length, hidden_size, x.device)
                return _real_2d_dft(
                    x,
                    self._seq_cos,
                    self._seq_sin,
                    self._hidden_cos,
                    self._hidden_sin,
                )

        return FNetFourierTransform()

    @property
    def elastic_num_params(self):
        return 0


class ElasticFNetFourierMixerBlock(nn.Module):
    """FNet encoder block with pre-LayerNorm ordering.

    Structure:
        LayerNorm -> FourierTransform -> residual add
        LayerNorm -> FFN(GELU) -> residual add

    Input/output: `[B, L, global_dim]`.
    """

    def __init__(
        self,
        *,
        super_ffn_dim: int,
        global_dim: int,
    ):
        super().__init__()
        self.global_dim = global_dim
        self.super_ffn_dim = super_ffn_dim

        self.fourier = ElasticFNetFourierTransform()
        self.mixing_norm = ElasticLayerNorm(
            super_hidden_size=global_dim,
            eps=LAYER_NORM_EPSILON,
        )
        self.mlp_fc1 = ElasticLinear(
            super_in_dim=global_dim,
            super_out_dim=super_ffn_dim,
        )
        self.mlp_act = nn.GELU()
        self.mlp_fc2 = ElasticLinear(
            super_in_dim=super_ffn_dim,
            super_out_dim=global_dim,
        )
        self.output_norm = ElasticLayerNorm(
            super_hidden_size=global_dim,
            eps=LAYER_NORM_EPSILON,
        )

        self.sample_ffn_dim = super_ffn_dim
        self.reset_parameters()

    def reset_parameters(self):
        nn.init.normal_(self.mlp_fc1.weight, std=0.02)
        if self.mlp_fc1.bias is not None:
            nn.init.normal_(self.mlp_fc1.bias, std=0.02)
        nn.init.normal_(self.mlp_fc2.weight, std=0.02)
        if self.mlp_fc2.bias is not None:
            nn.init.zeros_(self.mlp_fc2.bias)

    def set_sample_config(self, *, sample_ffn_dim: int):
        if sample_ffn_dim < 1 or sample_ffn_dim > self.super_ffn_dim:
            raise ValueError("sample_ffn_dim must be in [1, super_ffn_dim].")
        self.sample_ffn_dim = sample_ffn_dim
        self.mixing_norm.set_sample_config(sample_hidden_size=self.global_dim)
        self.mlp_fc1.set_sample_config(
            sample_in_dim=self.global_dim,
            sample_out_dim=sample_ffn_dim,
        )
        self.mlp_fc2.set_sample_config(
            sample_in_dim=sample_ffn_dim,
            sample_out_dim=self.global_dim,
        )
        self.output_norm.set_sample_config(sample_hidden_size=self.global_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.fourier(self.mixing_norm(x))
        ffn = self.mlp_fc2(self.mlp_act(self.mlp_fc1(self.output_norm(x))))
        return x + ffn

    def get_active_subnet(self) -> nn.Module:
        class FNetFourierMixerBlock(nn.Module):
            def __init__(self, fourier, mixing_norm, mlp, output_norm):
                super().__init__()
                self.fourier = fourier
                self.mixing_norm = mixing_norm
                self.mlp = mlp
                self.output_norm = output_norm

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                x = x + self.fourier(self.mixing_norm(x))
                return x + self.mlp(self.output_norm(x))

        return FNetFourierMixerBlock(
            self.fourier.get_active_subnet(),
            self.mixing_norm.get_active_subnet(),
            nn.Sequential(
                self.mlp_fc1.get_active_subnet(),
                nn.GELU(),
                self.mlp_fc2.get_active_subnet(),
            ),
            self.output_norm.get_active_subnet(),
        )

    @property
    def elastic_num_params(self):
        return (
            self.fourier.elastic_num_params
            + self.mixing_norm.elastic_num_params
            + self.mlp_fc1.elastic_num_params
            + self.mlp_fc2.elastic_num_params
            + self.output_norm.elastic_num_params
        )


def is_valid_fnet_fourier_mixer_block(config: dict[str, Any]) -> bool:
    ffn_dim = config.get("ffn_dim")
    return isinstance(ffn_dim, int) and ffn_dim > 0


if __name__ == "__main__":
    B, L, C = 2, 128, 64
    torch.manual_seed(0)
    super_block = ElasticFNetFourierMixerBlock(
        global_dim=C,
        super_ffn_dim=128,
    ).eval()

    test_configs = [
        {"ffn_dim": 64},
        {"ffn_dim": 128},
    ]
    for cfg in test_configs:
        super_block.set_sample_config(sample_ffn_dim=cfg["ffn_dim"])
        x = torch.randn(B, L, C)
        with torch.no_grad():
            y_super = super_block(x)
            subnet = super_block.get_active_subnet().eval()
            y_sub = subnet(x)
        diff = (y_super - y_sub).abs().max().item()
        print(f"  [Pass] Config={cfg}, Consistency Diff={diff:.2e}")
        assert diff < 1e-6
        assert y_super.shape == (B, L, C)

    print(">>> All FNetFourierMixerBlock tests passed!")
