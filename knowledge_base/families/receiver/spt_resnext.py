"""spt_resnext.py —— model8 变体（全 CNN · 分组卷积 cardinality）。

KD-NAS student 候选：**分组卷积**残差块（ResNeXt 风格）——标准 Conv1d(k=3, groups=cardinality)
+ BN + ReLU + 残差。物理动机：MIMO-OFDM 的 ``embed_dim`` 个特征通道并非全连接耦合——
分组卷积把通道按 ``cardinality`` 切组，每组独立做 3-tap 频率滤波，等价于**先按子通道族做局部
频率均衡、再跨族组合**（参数量 ÷ groups，感受野不变）。与 ``spt_cnn_dilated`` 的"全通道一致
dilation"正交——本变体是**正交分组频率处理**极。

昇腾友好性：分组 Conv1d 是 IMG2COL 的分组变体（cube unit 原生支持，CANN ``GroupedConv`` 算子
有专用 kernel）；groups=4 是昇腾 cube 的高效分块粒度（channel 16/4=4 满足 cube tile）。**零
MATMUL**（无 1×1、无 attention、无 DW——DW groups=C 在昇腾饿死 cube 见 KB failures.md #1；
本变体 groups=4 << C，远非 DW，cube 正常吃）。

与原 ResNeXt 的差异：原版 ResNeXt bottleneck = 1×1 reduce → grouped 3×3 → 1×1 expand
（2 个 1×1 = 2 次 MATMUL 格式 + TransData）。本变体**砍掉 1×1 bottleneck**，只保留 grouped 3×3
+ 残差——保住 ResNeXt 的核心身份（cardinality 分组），把 MATMUL 密集的瓶颈层让给 cube 友好
的纯 grouped conv。代价：失去 1×1 的跨组信息混合（由下一 block 的 e_lyr 3-tap 标准 conv
部分补偿）。

来源 / 灵感：
- ResNeXt（Xie et al., CVPR 2017, "Aggregated Residual Transformations..."）——cardinality
  维度思想（"split-transform-merge"）。
- HGEMM on Ascend NPU（MDPI Computers 2026）——Ascend cube+vector 对分组 GEMM 的支持。
- 本变体是 ResNeXt 的昇腾友好极简版（去 1×1 bottleneck）。

与现有变体的关系：
- ``spt_cnn_dilated`` —— 全通道一致 dilation（无分组）
- ``spt_cnn_pointwise`` —— 全局 pointwise（groups=1 但 k=1）
- **``spt_resnext``（本）—— 分组 k=3（groups=4 < C）**

约束：``embed_dim`` 必须被 ``_CARDINALITY=4`` 整除（KNOBS 16→12→8 均满足；min=8 已含此约束）。

契约见 ``README.md``。
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

_IN_CHANNELS = 4
_NUM_SYMBOLS = 64
_NUM_SUBCARRIERS = 48
_CARDINALITY = 4   # ResNeXt 分组数（固定；embed_dim 须被它整除，KNOBS min=8 已兼容）


class ResNeXtBlock(nn.Module):
    """分组卷积残差块：Conv1d(k=3, groups=cardinality) → BN → ReLU + residual。

    无 1×1 bottleneck（避免 MATMUL）。num_symbols/num_subcarriers 仅 introspection（forward
    靠输入 shape），与 ``DilatedResBlock`` 签名对齐以便 hook 复用。
    """

    def __init__(self, embed_dim, num_symbols, num_subcarriers,
                 kernel_size=3, cardinality=_CARDINALITY):
        super().__init__()
        assert embed_dim % cardinality == 0, (
            f"embed_dim={embed_dim} 须被 cardinality={cardinality} 整除（ResNeXt 分组约束）"
        )
        assert kernel_size % 2 == 1, f"kernel 须奇数，得到 {kernel_size}"
        self.cv = nn.Conv1d(embed_dim, embed_dim, kernel_size=kernel_size,
                            padding=(kernel_size - 1) // 2, groups=cardinality, bias=False)
        self.bn = nn.BatchNorm1d(embed_dim)
        self.act = nn.ReLU(inplace=True)

    def forward(self, x):
        B, S, C, F_ = x.shape
        h = x.reshape(B * S, C, F_)
        h = self.act(self.bn(self.cv(h)))
        h = h.reshape(B, S, C, F_)
        return h + x


class ResNeXtReceiver(nn.Module):
    """分组卷积 CNN 主体：3-tap stem → N×ResNeXtBlock → 3-tap r_out，alpha 功率归一外壳。"""

    def __init__(self, in_channels=_IN_CHANNELS, embed_dim=16, num_symbols=_NUM_SYMBOLS,
                 num_subcarriers=_NUM_SUBCARRIERS, bias_flag=True, num_blocks=4,
                 cardinality=_CARDINALITY):
        super().__init__()
        assert embed_dim % cardinality == 0, (
            f"embed_dim={embed_dim} 须被 cardinality={cardinality} 整除"
        )
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.e_lyr = nn.Conv1d(in_channels, embed_dim, kernel_size=3, padding=1, bias=bias_flag)
        self.main = nn.Sequential(*[
            ResNeXtBlock(embed_dim, num_symbols, num_subcarriers, cardinality=cardinality)
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
    """实例化分组卷积 ResNeXt 变体。cfg 取 num_blocks / embed_dim（cardinality 固定 4）。

    embed_dim 须被 cardinality=4 整除，否则 fail loud（KNOBS min=8 + step=-4 已保证缩容路径
    8/12/16 均兼容）。
    """
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    if embed_dim % _CARDINALITY != 0:
        raise ValueError(
            f"spt_resnext embed_dim={embed_dim} 须被 cardinality={_CARDINALITY} 整除"
        )
    return ResNeXtReceiver(embed_dim=embed_dim, num_blocks=num_blocks)
