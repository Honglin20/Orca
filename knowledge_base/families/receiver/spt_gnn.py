"""spt_gnn.py —— model8 变体（Conv + GNN 交替，NVIDIA NRx 风格）。

KD-NAS student 候选：MIMO 层间（num_ports=4）全连接图 GNN 消息传递 + Conv 状态更新交替。
物理动机：MIMO 层间干扰是物理耦合，全连接图建模层间互相关，mean 聚合 ≈ MMSE-style 加权合并。
对 num_ports 维度有独占动机——其他变体把 ports 当 channel 在 e_lyr 一次性混合，本变体显式建模
层间关系（per-port embed 保留 port 作图节点）。NVIDIA NRX 实测 <1ms on A100+TRT。

⚠️ feature 几何与 teacher（per-channel attention）差异最大（含额外 num_ports 维），OFD adapter
靠 ``kd/losses._align_spatial`` 兜底对齐，KD 质量可能弱于同族变体（KB 标 KD 中等）。

工作布局 5D：``[B, num_symbols, num_ports, embed_dim, num_subcarriers]``。
来源：KB direction ``nvidia_nrx_conv_gnn.md``（D6，无 raw 代码，本文件新实现）。契约见 ``README.md``。
"""

from __future__ import annotations

import torch
import torch.nn as nn

DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
BUILD_FN = "build_model"

KNOBS = {
    "num_blocks": {"default": 4, "min": 1, "step": -1, "leverage": "high"},
    "embed_dim": {"default": 16, "min": 8, "step": -4, "leverage": "medium"},
}

_IN_CHANNELS = 4   # = num_ports（MIMO 层，GNN 图节点）
_NUM_SYMBOLS = 64
_NUM_SUBCARRIERS = 48


class _GNNStep(nn.Module):
    """MIMO 层全连接图 mean 聚合：每节点收其他 num_ports-1 节点的均值消息 + 残差。

    输入输出 ``[B, num_symbols, num_ports, embed_dim, num_subcarriers]``。
    """

    def __init__(self, embed_dim, num_ports):
        super().__init__()
        assert num_ports >= 2, f"GNN 需 ≥2 节点（num_ports），得到 {num_ports}"
        self.num_ports = num_ports
        self.proj = nn.Conv1d(embed_dim, embed_dim, kernel_size=1, bias=False)

    def forward(self, x):
        B, S, P, C, F_ = x.shape
        msg = self.proj(x.reshape(B * S * P, C, F_)).reshape(B, S, P, C, F_)
        total = msg.sum(dim=2, keepdim=True)            # [B, S, 1, C, F]
        others = (total - msg) / (self.num_ports - 1)   # 其他节点均值
        return x + others


class _ConvStep(nn.Module):
    """Conv 状态更新（subcarrier 轴 3-tap 标准 conv）+ 残差。5D 布局。"""

    def __init__(self, embed_dim, kernel_size=3):
        super().__init__()
        self.conv = nn.Conv1d(embed_dim, embed_dim, kernel_size,
                              padding=(kernel_size - 1) // 2, bias=False)
        self.bn = nn.BatchNorm1d(embed_dim)
        self.act = nn.ReLU(inplace=True)

    def forward(self, x):
        B, S, P, C, F_ = x.shape
        h = x.reshape(B * S * P, C, F_)
        h = self.act(self.bn(self.conv(h))).reshape(B, S, P, C, F_)
        return h + x


class _GnnBlock(nn.Module):
    """NRx 风格交替块：GNN 子步（层间消息）→ Conv 子步（状态更新）。"""

    def __init__(self, embed_dim, num_ports, kernel_size=3):
        super().__init__()
        self.gnn = _GNNStep(embed_dim, num_ports)
        self.conv = _ConvStep(embed_dim, kernel_size)

    def forward(self, x):
        x = self.gnn(x)
        x = self.conv(x)
        return x


class NrxReceiver(nn.Module):
    """Conv+GNN 主体：per-port embed（Conv1d 1→C，保留 port 维）→ N×_GnnBlock → per-port r_out。"""

    def __init__(self, in_channels=_IN_CHANNELS, embed_dim=16, num_symbols=_NUM_SYMBOLS,
                 num_subcarriers=_NUM_SUBCARRIERS, bias_flag=True, num_blocks=4,
                 kernel_size=3):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        # per-port embed：每个 port 的 subcarrier 序列独立 embed（不混合 port），保留 port 作图节点
        self.e_lyr = nn.Conv1d(1, embed_dim, kernel_size=3, padding=1, bias=bias_flag)
        self.main = nn.Sequential(*[
            _GnnBlock(embed_dim, in_channels, kernel_size=kernel_size) for _ in range(num_blocks)
        ])
        self.r_out = nn.Conv1d(embed_dim, 1, kernel_size=3, padding=1, bias=bias_flag)

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
        x = x.permute(0, 3, 1, 2)                       # [B, S, P, F]
        x = x.reshape(B * S * P, 1, F_)                 # per-port [N, 1, F]
        x = self.e_lyr(x)                               # [N, C, F]
        x = x.reshape(B, S, P, -1, F_)                  # [B, S, P, C, F]
        x = self.main(x)
        x = x.reshape(B * S * P, -1, F_)
        x = self.r_out(x)                               # [N, 1, F]
        x = x.reshape(B, S, P, F_).permute(0, 2, 3, 1)  # [B, P, F, S]
        x = x * alpha
        return torch.unsqueeze(x, dim=-1)


def build_model(**cfg) -> nn.Module:
    """实例化 Conv+GNN 变体。cfg 取 num_blocks / embed_dim。"""
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    return NrxReceiver(embed_dim=embed_dim, num_blocks=num_blocks)
