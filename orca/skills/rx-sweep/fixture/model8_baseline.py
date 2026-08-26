"""model8_baseline.py —— rx-sweep fixture 的 model8 参考线（attention 模型）。

本文件是 fixture 自包含的「原模型」基线，供：
  - gate_check 验 model8 I/O 通；
  - KD 实验的 teacher 结构（state_dict 由 train_rx 在运行时随机初始化并 torch.save）。

结构参考生产 model8 attention 接收机，仅作 fixture baseline；加了 fixture 契约
（``DUMMY_INPUT`` / ``BUILD_FN`` / ``build_model``），**未改 forward 语义**。

I/O 契约（与所有 model8 变体一致）：
  - 输入 ``[B, num_ports, num_subcarriers, num_symbols, 1]``
  - 输出同形
  - 内部自理 alpha 功率归一（``x = inp/(sqrt(mean(inp²)·2)+1e-6)``，出口 ``*alpha``）
"""

from __future__ import annotations

import torch
import torch.nn as nn

# ---------- fixture 契约 ----------
DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
OUTPUT_SHAPE = [1, 4, 48, 64, 1]
BUILD_FN = "build_model"


# ---------- model8 积木（fixture baseline，结构参考生产 model8 attention 接收机）----------
class SignalAttention1D(nn.Module):
    """model8 attention。``m_type="t1"`` = symbol 轴 attention（scale=num_subcarriers^-0.5）；
    ``"t2"`` = subcarrier 轴 attention（scale=embed_dim^-0.5）。"""

    def __init__(self, embed_dim, num_symbols, num_subcarriers, b_flg=True, m_type="t1"):
        super().__init__()
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.m_type = m_type

        self.s = num_subcarriers ** -0.5 if m_type == "t1" else embed_dim ** -0.5

        self.ln = nn.LayerNorm([embed_dim, num_symbols, num_subcarriers], elementwise_affine=False)
        self.sm = nn.Softmax(dim=-1)
        self.p_lyr = nn.Conv1d(in_channels=embed_dim, out_channels=3 * embed_dim,
                               kernel_size=3, padding=1, bias=b_flg)

    def forward(self, x):
        batch, num_syms, embed_dim, num_subs = x.shape

        x = x.permute(0, 2, 1, 3)
        x = self.ln(x)
        x = x.permute(0, 2, 1, 3)

        x_f = torch.reshape(x, [batch * num_syms, embed_dim, num_subs])
        qkv = self.p_lyr(x_f)
        qkv = torch.reshape(qkv, [batch, num_syms, 3 * self.embed_dim, num_subs])

        q = qkv[:, :, 0:self.embed_dim, :]
        k = qkv[:, :, self.embed_dim:2 * self.embed_dim, :]
        v = qkv[:, :, 2 * self.embed_dim:, :]

        if self.m_type == "t1":
            q = q.permute(0, 2, 1, 3)
            k = k.permute(0, 2, 1, 3)
            v = v.permute(0, 2, 1, 3)
            dots = torch.matmul(q, k.transpose(-1, -2)) * self.s
            at = self.sm(dots)
            out = torch.matmul(at, v).permute(0, 2, 1, 3)
        else:
            q = q.permute(0, 3, 1, 2)
            k = k.permute(0, 3, 1, 2)
            v = v.permute(0, 3, 1, 2)
            dots = torch.matmul(q, k.transpose(-1, -2)) * self.s
            at = self.sm(dots)
            out = torch.matmul(at, v).permute(0, 2, 3, 1)
        return out


class SignalFeedForward1D(nn.Module):
    def __init__(self, embed_dim, num_symbols, num_subcarriers, b_flg=True):
        super().__init__()
        self.embed_dim = embed_dim
        self.ln = nn.LayerNorm([num_symbols, embed_dim, num_subcarriers], elementwise_affine=False)
        self.cv1 = nn.Conv1d(in_channels=embed_dim, out_channels=2 * embed_dim, kernel_size=3, padding=1, bias=b_flg)
        self.act = nn.GELU()
        self.cv2 = nn.Conv1d(in_channels=2 * embed_dim, out_channels=embed_dim, kernel_size=3, padding=1, bias=b_flg)

    def forward(self, x):
        batch, num_syms, embed_dim, num_subs = x.shape
        x = self.ln(x)
        x_f = torch.reshape(x, [batch * num_syms, embed_dim, num_subs])
        x = self.cv1(x_f)
        x = self.act(x)
        x = self.cv2(x)
        return torch.reshape(x, [batch, num_syms, embed_dim, num_subs])


class SignalTransformerBlock(nn.Module):
    def __init__(self, embed_dim, num_symbols, num_subcarriers, m_type="t1"):
        super().__init__()
        self.m_a = SignalAttention1D(embed_dim, num_symbols, num_subcarriers, m_type=m_type)
        self.proj = nn.Conv1d(in_channels=embed_dim, out_channels=embed_dim, kernel_size=3, padding=1, bias=False)
        self.m_c = SignalFeedForward1D(embed_dim, num_symbols, num_subcarriers)

    def forward(self, x):
        x_a = self.m_a(x)
        batch, num_syms, embed_dim, num_subs = x.shape
        x_f_f = torch.reshape(x_a, [batch * num_syms, -1, num_subs])
        x_p = self.proj(x_f_f)
        x_p = torch.reshape(x_p, [batch, num_syms, embed_dim, num_subs])
        x = x_p + x
        x_m_c = self.m_c(x)
        x = x_m_c + x
        return x


class SignalProcessingTransformer(nn.Module):
    """model8 主体。``block_mtypes`` 显式给出每个 block 的 attention 类型。"""

    def __init__(self, block_mtypes, in_channels=4, embed_dim=16, num_symbols=64,
                 num_subcarriers=48, bias_flag=True):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.b_flg = bias_flag
        self.block_mtypes = list(block_mtypes)

        self.e_lyr = nn.Conv1d(in_channels=in_channels, out_channels=embed_dim,
                               kernel_size=3, padding=1, bias=bias_flag)
        self.main = nn.Sequential(*[
            SignalTransformerBlock(embed_dim, num_symbols, num_subcarriers, m_type=mt)
            for mt in self.block_mtypes
        ])
        self.r_out = nn.Conv1d(in_channels=embed_dim, out_channels=in_channels,
                               kernel_size=3, padding=1, bias=bias_flag)

    def feature_hook_names(self) -> list[str]:
        """KD/FitNets 特征对齐 hook 名（恒 2 个）。取首层 + 中间层 block。"""
        n = len(self.block_mtypes)
        mid = max(1, n // 2) if n > 1 else 0
        second = f"main.{mid}" if n > 1 else "main.0"
        return ["main.0", second]

    def forward(self, inp: torch.Tensor):
        if inp.dim() == 5 and inp.shape[-1] == 1:
            inp = torch.squeeze(inp, dim=-1)
        B, num_ports, num_subcarriers, num_symbols = inp.shape
        alpha = torch.sqrt(torch.mean(inp ** 2, dim=[1, 2, 3], keepdim=True) * 2)
        x = inp / (alpha + 1e-6)
        x = x.permute(0, 3, 1, 2)
        x = torch.reshape(x, [B * num_symbols, num_ports, num_subcarriers])
        x = self.e_lyr(x)
        x = torch.reshape(x, [B, num_symbols, -1, num_subcarriers])
        x = self.main(x)
        x = torch.reshape(x, [B * num_symbols, -1, num_subcarriers])
        x = self.r_out(x)
        x = torch.reshape(x, [B, num_symbols, num_ports, num_subcarriers])
        x = x.permute(0, 2, 3, 1)
        x = x * alpha
        x = torch.unsqueeze(x, dim=-1)
        return x


# ---------- fixture 构造契约 ----------
def build_model(**cfg) -> nn.Module:
    """零参用默认；cfg 覆盖。

    接受的 key：``num_blocks`` / ``embed_dim`` / ``num_symbols`` / ``num_subcarriers``
    / ``in_channels`` / ``block_mtypes`` / ``bias_flag``。pure_cnn 族独有的 key
    （``variant`` / ``use_pilot_enrich`` / ``use_lmmse`` / ``dilations`` / ``noise_var`` /
    ``pilot_mask`` / ``pilot_values``）容错忽略——model8 baseline 用不上。
    """
    num_blocks = cfg.get("num_blocks", 4)
    embed_dim = cfg.get("embed_dim", 16)
    num_symbols = cfg.get("num_symbols", 64)
    num_subcarriers = cfg.get("num_subcarriers", 48)
    in_channels = cfg.get("in_channels", 4)
    block_mtypes = cfg.get("block_mtypes", ["t1"] * num_blocks)
    bias_flag = cfg.get("bias_flag", True)
    return SignalProcessingTransformer(
        block_mtypes=block_mtypes,
        in_channels=in_channels,
        embed_dim=embed_dim,
        num_symbols=num_symbols,
        num_subcarriers=num_subcarriers,
        bias_flag=bias_flag,
    )
