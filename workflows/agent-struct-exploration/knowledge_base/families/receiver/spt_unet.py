"""spt_unet.py —— model8 变体（U-Net 多尺度）。

KD-NAS student 候选：subcarrier 轴 MaxPool↓(48→24) → bottleneck @ 24 → ConvTranspose↑(24→48)，
encoder/decoder 用 DilatedResBlock，skip concat（U-Net）。多尺度：bottleneck 在低分辨率做粗
处理省算力，skip 保留高频细节。与 flat 结构（spt_cnn_*/spt_puretf）cost-accuracy 曲线不同，
给 sweep 补一个"多尺度"维度的点。

参考：``nas-agent/model8/model_eng/eng_model.py`` 的 MaxPool/ConvTranspose 雏形。
契约见 ``README.md``。
"""

from __future__ import annotations

from _model8_blocks import DilatedResBlock  # noqa: F401  (共享积木)
import torch
import torch.nn as nn

DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
BUILD_FN = "build_model"

# num_blocks 控制 encoder / decoder 各自的 DilatedResBlock 数（bottleneck 固定 1）。
KNOBS = {
    "num_blocks": {"default": 2, "min": 1, "step": -1, "leverage": "high"},
    "embed_dim": {"default": 16, "min": 8, "step": -4, "leverage": "medium"},
}

_IN_CHANNELS = 4
_NUM_SYMBOLS = 64
_NUM_SUBCARRIERS = 48


class UNetReceiver(nn.Module):
    """U-Net 主体：stem → encoder(@48) → ↓ → bottleneck(@24) → ↑+skip → decoder(@48) → r_out。"""

    def __init__(self, in_channels=_IN_CHANNELS, embed_dim=16, num_symbols=_NUM_SYMBOLS,
                 num_subcarriers=_NUM_SUBCARRIERS, bias_flag=True, num_blocks=2):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.e_lyr = nn.Conv1d(in_channels, embed_dim, kernel_size=3, padding=1, bias=bias_flag)
        self.encoder = nn.Sequential(*[
            DilatedResBlock(embed_dim, num_symbols, num_subcarriers)
            for _ in range(num_blocks)
        ])
        self.down = nn.MaxPool1d(kernel_size=2, stride=2)               # 48→24
        self.bottleneck = DilatedResBlock(embed_dim, num_symbols, num_subcarriers // 2)  # @24
        self.up = nn.ConvTranspose1d(embed_dim, embed_dim, kernel_size=2, stride=2)      # 24→48
        self.skip_proj = nn.Conv1d(2 * embed_dim, embed_dim, kernel_size=1, bias=False)
        self.decoder = nn.Sequential(*[
            DilatedResBlock(embed_dim, num_symbols, num_subcarriers)
            for _ in range(num_blocks)
        ])
        self.r_out = nn.Conv1d(embed_dim, in_channels, kernel_size=3, padding=1, bias=bias_flag)

    def feature_hook_names(self) -> list[str]:
        # encoder 与 decoder 输出都在 F=48，与 teacher spatial 对齐干净（恒 2 个）。
        return ["encoder", "decoder"]

    def forward(self, inp: torch.Tensor):
        if inp.dim() == 5 and inp.shape[-1] == 1:
            inp = torch.squeeze(inp, dim=-1)
        B, P, F_, S = inp.shape
        alpha = torch.sqrt(torch.mean(inp ** 2, dim=[1, 2, 3], keepdim=True) * 2)
        x = inp / (alpha + 1e-6)
        x = x.permute(0, 3, 1, 2).reshape(B * S, P, F_)   # [B*S, P, F]
        x = self.e_lyr(x)                                  # [B*S, C, F]
        x = x.reshape(B, S, -1, F_)                        # [B, S, C, F]

        enc = self.encoder(x)                              # [B, S, C, 48]
        h = enc.reshape(B * S, -1, F_)
        h = self.down(h)                                   # [B*S, C, 24]
        h = h.reshape(B, S, -1, F_ // 2)
        h = self.bottleneck(h)                             # [B, S, C, 24]
        h = h.reshape(B * S, -1, F_ // 2)
        h = self.up(h)                                     # [B*S, C, 48]
        h = h.reshape(B, S, -1, F_)

        cat = torch.cat([enc, h], dim=2)                   # [B, S, 2C, 48]
        cat = self.skip_proj(cat.reshape(B * S, -1, F_)).reshape(B, S, -1, F_)  # [B, S, C, 48]
        dec = self.decoder(cat)                            # [B, S, C, 48]

        out = dec.reshape(B * S, -1, F_)
        out = self.r_out(out)                              # [B*S, P, 48]
        out = out.reshape(B, S, P, F_).permute(0, 2, 3, 1)
        out = out * alpha
        return torch.unsqueeze(out, dim=-1)


def build_model(**cfg) -> nn.Module:
    """实例化 U-Net 变体。cfg 取 num_blocks（encoder/decoder 各 N）/ embed_dim。"""
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    return UNetReceiver(embed_dim=embed_dim, num_blocks=num_blocks)
