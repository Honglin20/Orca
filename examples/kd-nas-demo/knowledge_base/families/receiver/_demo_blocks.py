"""_demo_blocks.py —— kd-nas-demo KB 共享的【简化】model8 风格积木。

与仓库 ``knowledge_base/families/receiver/_model8_blocks.py`` 的关系：
  - **非复制**：本模块是 demo 专用的【简化原创】实现（``ReceiverShell`` 基类 + 更小的
    attention / FFN / CNN block），目的是让 kd-nas workflow 的 E2E 在 CPU 上分钟级跑通。
    结构与 ``_model8_blocks`` 不同（后者无基类、attention 含 LayerNorm/3-tap proj）。
  - I/O 契约一致：输入 ``[B, 4, 48, 64, 1]``，alpha 功率归一（``x = inp/(sqrt(mean(inp²)·2)+1e-6)``，
    出口 ``*alpha``），输出同形。
  - ``feature_hook_names`` 恒返回 2 个 hook（与 teacher 等长，供 KD 的 OFD/FitNets 特征对齐；
    否则 ``kd.compose.prepare`` 会因 student/teacher 特征数不等 raise）。

被同目录变体 ``.py`` 以 ``from _demo_blocks import ...`` 复用（与真实 KB 变体
``from _model8_blocks import ...`` 同款模式）。下划线前缀 = 共享模块，
``pick_variant.py`` glob 时排除 ``_*.py``（不是变体候选）。
"""

from __future__ import annotations

import torch
import torch.nn as nn

# 固定结构参数（与 DUMMY_INPUT 一致；变体共享）。
IN_CHANNELS = 4
NUM_SYMBOLS = 64
NUM_SUBCARRIERS = 48


class ReceiverShell(nn.Module):
    """model8 alpha-norm 外壳 + I/O reshape。subclass 在 ``__init__`` 里设 ``self.main``。

    forward 流水（变体只需提供 ``self.main: nn.Sequential``，输入输出 ``[B, S, C, F_]``）::

        squeeze(-1) → alpha 功率归一 → [B*S, P, F_] → e_lyr(3-tap) → [B, S, C, F_]
        → self.main → [B*S, C, F_] → r_out(3-tap) → [B, S, P, F_] → permute → *alpha → unsqueeze

    e_lyr / r_out 用 3-tap Conv1d 补跨子载波局部性（pointwise 主干缺频率平滑时靠它）。
    """

    def __init__(self, in_channels: int = IN_CHANNELS, embed_dim: int = 16,
                 num_symbols: int = NUM_SYMBOLS, num_subcarriers: int = NUM_SUBCARRIERS,
                 bias_flag: bool = True):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.e_lyr = nn.Conv1d(in_channels, embed_dim, kernel_size=3, padding=1, bias=bias_flag)
        self.r_out = nn.Conv1d(embed_dim, in_channels, kernel_size=3, padding=1, bias=bias_flag)
        # self.main 由 subclass 设置（nn.Sequential，元素输出 [B, S, C, F_]）。

    def feature_hook_names(self) -> list[str]:
        """OFD/FitNets 特征对齐 hook 名（**恒为 2 个**，与 teacher 等长）。

        取首层 + 中间层 block。``num_blocks=1`` 时第二个 hook 重复 ``main.0``——保持与
        teacher（固定 2 hook）等长，否则 ``kd.compose.prepare`` 会 raise。
        """
        n = len(self.main)
        mid = max(1, n // 2) if n > 1 else 0
        second = f"main.{mid}" if n > 1 else "main.0"
        return ["main.0", second]

    def forward(self, inp: torch.Tensor) -> torch.Tensor:
        if inp.dim() == 5 and inp.shape[-1] == 1:
            inp = torch.squeeze(inp, dim=-1)
        B, num_ports, num_sub, num_syms = inp.shape
        alpha = torch.sqrt(torch.mean(inp ** 2, dim=[1, 2, 3], keepdim=True) * 2)
        x = inp / (alpha + 1e-6)
        x = x.permute(0, 3, 1, 2).reshape(B * num_syms, num_ports, num_sub)
        x = self.e_lyr(x)
        x = x.reshape(B, num_syms, -1, num_sub)
        x = self.main(x)
        x = x.reshape(B * num_syms, -1, num_sub)
        x = self.r_out(x)
        x = x.reshape(B, num_syms, num_ports, num_sub).permute(0, 2, 3, 1)
        x = x * alpha
        return torch.unsqueeze(x, dim=-1)


class TinyAttention(nn.Module):
    """简化单轴 attention（pointwise QKV + softmax(QK^T·scale)·V + pointwise proj）。

    比 ``_model8_blocks.SignalAttention1D`` 简化：去 LayerNorm、QKV/proj 全 pointwise（1×1）。
    - ``m_type="t1"``：symbol 轴 attention（scale = num_subcarriers^-0.5）
    - ``m_type="t2"``：subcarrier 轴 attention（scale = embed_dim^-0.5）

    输入输出 ``[B, num_symbols, embed_dim, num_subcarriers]``。
    """

    def __init__(self, embed_dim: int, num_symbols: int, num_subcarriers: int, m_type: str = "t1"):
        super().__init__()
        self.embed_dim = embed_dim
        self.m_type = m_type
        self.scale = (num_subcarriers ** -0.5) if m_type == "t1" else (embed_dim ** -0.5)
        self.qkv = nn.Conv1d(embed_dim, 3 * embed_dim, kernel_size=1, bias=True)
        self.proj = nn.Conv1d(embed_dim, embed_dim, kernel_size=1, bias=False)
        self.sm = nn.Softmax(dim=-1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, S, C, F_ = x.shape
        h = x.reshape(B * S, C, F_)
        qkv = self.qkv(h).reshape(B, S, 3 * C, F_)
        q = qkv[:, :, 0:C, :]
        k = qkv[:, :, C:2 * C, :]
        v = qkv[:, :, 2 * C:, :]
        if self.m_type == "t1":
            q, k, v = q.permute(0, 2, 1, 3), k.permute(0, 2, 1, 3), v.permute(0, 2, 1, 3)  # [B, C, S, F_]
            dots = torch.matmul(q, k.transpose(-1, -2)) * self.scale                    # [B, C, S, S]
            out = torch.matmul(self.sm(dots), v).permute(0, 2, 1, 3)                     # [B, S, C, F_]
        else:
            q, k, v = q.permute(0, 3, 1, 2), k.permute(0, 3, 1, 2), v.permute(0, 3, 1, 2)  # [B, F_, S, C]
            dots = torch.matmul(q, k.transpose(-1, -2)) * self.scale                     # [B, F_, S, S]
            out = torch.matmul(self.sm(dots), v).permute(0, 2, 3, 1)                      # [B, S, C, F_]
        out_f = out.reshape(B * S, C, F_)
        out_f = self.proj(out_f)
        return out_f.reshape(B, S, C, F_)


class TinyFFN(nn.Module):
    """简化 FFN：pointwise expand C→2C → GELU → pointwise contract 2C→C。"""

    def __init__(self, embed_dim: int):
        super().__init__()
        self.cv1 = nn.Conv1d(embed_dim, 2 * embed_dim, kernel_size=1, bias=True)
        self.act = nn.GELU()
        self.cv2 = nn.Conv1d(2 * embed_dim, embed_dim, kernel_size=1, bias=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, S, C, F_ = x.shape
        h = x.reshape(B * S, C, F_)
        h = self.cv2(self.act(self.cv1(h)))
        return h.reshape(B, S, C, F_)


class TinyTransformerBlock(nn.Module):
    """简化 transformer block：attn + residual + FFN + residual。"""

    def __init__(self, embed_dim: int, num_symbols: int, num_subcarriers: int, m_type: str = "t1"):
        super().__init__()
        self.m_a = TinyAttention(embed_dim, num_symbols, num_subcarriers, m_type=m_type)
        self.m_c = TinyFFN(embed_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.m_a(x)
        x = x + self.m_c(x)
        return x


class PointwiseBlock(nn.Module):
    """ConvNeXt 风格 pointwise inverted-bottleneck 残差块。

    1×1 expand C→2C → GELU → 1×1 contract 2C→C + residual。纯 pointwise（Cube-friendly），
    无 attention、无 DW。输入输出 ``[B, S, C, F_]``。
    """

    def __init__(self, embed_dim: int):
        super().__init__()
        self.expand = nn.Conv1d(embed_dim, 2 * embed_dim, kernel_size=1, bias=False)
        self.act = nn.GELU()
        self.contract = nn.Conv1d(2 * embed_dim, embed_dim, kernel_size=1, bias=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, S, C, F_ = x.shape
        h = x.reshape(B * S, C, F_)
        h = self.contract(self.act(self.expand(h)))
        h = h.reshape(B, S, C, F_)
        return h + x


class DilatedResBlock(nn.Module):
    """简化 DeepRx 风格 dilated conv 残差块。

    hourglass dilation ``(1, 2)`` 串联（kernel=3，等效 RF=7），标准 Conv1d（禁 DW）+ ReLU +
    residual。无 BatchNorm（避免 train-mode batch=1 边界 + 简化 ONNX 导出）。输入输出
    ``[B, S, C, F_]``。
    """

    def __init__(self, embed_dim: int, dilations: tuple[int, ...] = (1, 2)):
        super().__init__()
        self.layers = nn.ModuleList([
            nn.Sequential(
                nn.Conv1d(embed_dim, embed_dim, kernel_size=3, padding=d, dilation=d, bias=True),
                nn.ReLU(inplace=True),
            )
            for d in dilations
        ])

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, S, C, F_ = x.shape
        h = x.reshape(B * S, C, F_)
        for layer in self.layers:
            h = layer(h)
        h = h.reshape(B, S, C, F_)
        return h + x
