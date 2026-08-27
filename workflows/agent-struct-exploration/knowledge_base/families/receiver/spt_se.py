"""spt_se.py —— model8 变体（全 CNN · Squeeze-Excitation 通道注意力）。

KD-NAS student 候选：DeepRx 风格 dilated 残差块 + **SE 通道注意力**（Squeeze-Excitation）。
SE 思想：先 ``squeeze``（全局平均池化压 (S,F) 空间 → 每 channel 一个描述子）→ ``excitation``
（两层 1×1 FC 学 channel 间互依 → sigmoid 门控）→ 用门权重逐 channel 缩放特征。物理动机：
OFDM 信道在不同子载波 / 时隙的 SNR 起伏大，SE 让网络**自适应抑制弱信噪比通道、放大强信噪比
通道**——per-channel 频率选择性增益。与 ``spt_channelformer`` 的"浅 attn precoder（全局空间
注意力）"正交——本变体是**轻量 channel-wise 门控注意力**极，重活仍由 dilated CNN 干。

昇腾友好性：
- 主体是 ``DilatedResBlock``（标准 Conv1d，IMG2COL 进 Cube）。
- SE 的 2 个 1×1 投影是**微小 MATMUL**（C=16 → r=4 → C=16，共 ~128 参数 / block），cube 一次
  tile 就吃完，TransData 开销可忽略；sigmoid / scale 是 elementwise（Vector core）。
- 无 attention 的 QK^T·V 大 MATMUL，无 DW。比 ``spt_channelformer`` 的浅 attn 还轻。

来源 / 灵感：
- Squeeze-and-Excitation Networks（Hu et al., CVPR 2018 / TPAMI 2020，引用 54k+）——SE block。
- ECA-Net（Wang et al., CVPR 2020）——高效通道注意力变体（本变体用原版 SE，更稳）。
- DeepRx（Honkala et al., arXiv:2005.01494）——dilated CNN 接收机（本变体的主干来源）。
- 无线侧应用先例：PACE-Net（Entropy 2025）、Attention-based NN for Wireless CE（Luan 2022,
  arXiv:2204.13465）——self-attention 路线；本变体把 SE 这种**轻量通道注意力**首次引入 KB。

与现有变体的关系：
- ``spt_channelformer`` —— 浅 attn precoder（QK^T·V 空间注意力，1 层）
- ``spt_cnn_dilated`` —— 纯 dilated CNN（无任何注意力）
- **``spt_se``（本）—— dilated CNN + 轻量 SE 通道门控注意力**

契约见 ``README.md``。
"""

from __future__ import annotations

from _model8_blocks import DilatedResBlock  # noqa: F401  (共享积木)
import torch
import torch.nn as nn

DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
BUILD_FN = "build_model"

KNOBS = {
    "num_blocks": {"default": 4, "min": 1, "step": -1, "leverage": "high"},
    "embed_dim": {"default": 16, "min": 8, "step": -4, "leverage": "medium"},
}

_IN_CHANNELS = 4
_NUM_SYMBOLS = 64
_NUM_SUBCARRIERS = 48
_SE_RATIO = 4     # SE 缩减比（reduction ratio；hidden = embed_dim / _SE_RATIO）


class SEBlock(nn.Module):
    """Squeeze-Excitation 通道注意力：pool(S,F) → 1×1↓ → ReLU → 1×1↑ → Sigmoid → 逐 channel scale。

    输入输出 ``[B, num_symbols, embed_dim, num_subcarriers]``。pool 跨 (S, F) 两空间轴得
    per-channel descriptor ``[B, C]``；excitation 走两个 1×1 Conv1d（reshape 到 [B, C, 1]）。
    """

    def __init__(self, embed_dim, se_ratio=_SE_RATIO):
        super().__init__()
        hidden = max(1, embed_dim // se_ratio)
        self.embed_dim = embed_dim
        # squeeze 在 forward 用 mean 跨 (S, F) 直接做（无须 AdaptivePool 模块）。
        self.excite_down = nn.Conv1d(embed_dim, hidden, kernel_size=1, bias=True)
        self.act = nn.ReLU(inplace=True)
        self.excite_up = nn.Conv1d(hidden, embed_dim, kernel_size=1, bias=True)
        self.gate = nn.Sigmoid()

    def forward(self, x):
        # x: [B, S, C, F]
        B, S, C, F_ = x.shape
        h = x.mean(dim=(1, 3)).reshape(B, C, 1)         # squeeze: 跨 (S,F) 平均 → [B, C, 1]
        h = self.act(self.excite_down(h))               # excitation ↓ [B, hidden, 1]
        h = self.gate(self.excite_up(h))                # excitation ↑ [B, C, 1]
        g = h.reshape(B, 1, C, 1)                       # broadcast 到 [B, S, C, F]
        return x * g


class SEResBlock(nn.Module):
    """SE + dilated CNN 残差块：DilatedResBlock → SE → +残差。"""

    def __init__(self, embed_dim, num_symbols, num_subcarriers, se_ratio=_SE_RATIO):
        super().__init__()
        self.body = DilatedResBlock(embed_dim, num_symbols, num_subcarriers)
        self.se = SEBlock(embed_dim, se_ratio=se_ratio)

    def forward(self, x):
        h = self.body(x)
        h = self.se(h)
        return h + x


class SEReceiver(nn.Module):
    """SE + dilated CNN 主体：3-tap stem → N×SEResBlock → 3-tap r_out，alpha 功率归一外壳。"""

    def __init__(self, in_channels=_IN_CHANNELS, embed_dim=16, num_symbols=_NUM_SYMBOLS,
                 num_subcarriers=_NUM_SUBCARRIERS, bias_flag=True, num_blocks=4,
                 se_ratio=_SE_RATIO):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.e_lyr = nn.Conv1d(in_channels, embed_dim, kernel_size=3, padding=1, bias=bias_flag)
        self.main = nn.Sequential(*[
            SEResBlock(embed_dim, num_symbols, num_subcarriers, se_ratio=se_ratio)
            for _ in range(num_blocks)
        ])
        self.r_out = nn.Conv1d(embed_dim, in_channels, kernel_size=3, padding=1, bias=bias_flag)

    def feature_hook_names(self) -> list[str]:
        n = len(self.main)
        mid = max(1, n // 2) if n > 1 else 0
        second = f"main.{mid}" if n > 1 else "main.0"
        return ["main.0", second]

    def forward(self, inp: torch.Tensor):
        if inp.dim() == 5 and inp.shape[-1] == 1:
            inp = torch.squeeze(inp, dim=-1)
        B, P, F_, S = inp.shape
        alpha = torch.sqrt(torch.mean(inp ** 2, dim=[1, 2, 3], keepdim=True) * 2)
        x = inp / (alpha + 1e-6)
        x = x.permute(0, 3, 1, 2).reshape(B * S, P, F_)
        x = self.e_lyr(x)
        x = x.reshape(B, S, -1, F_)
        x = self.main(x)
        x = x.reshape(B * S, -1, F_)
        x = self.r_out(x)
        x = x.reshape(B, S, P, F_).permute(0, 2, 3, 1)
        x = x * alpha
        return torch.unsqueeze(x, dim=-1)


def build_model(**cfg) -> nn.Module:
    """实例化 SE 通道注意力 CNN 变体。cfg 取 num_blocks / embed_dim（se_ratio 固定 4）。"""
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    return SEReceiver(embed_dim=embed_dim, num_blocks=num_blocks)
