"""_model8_blocks.py —— receiver KB 内共享的 model8 结构积木（便携，随 KB 分发）。

被同目录的变体 ``.py``（如 ``spt_t1.py``）以 ``from _model8_blocks import ...`` 复用。
**下划线前缀** = 共享模块，不是变体候选（``pick_variant.py`` glob 时排除 ``_*.py``）。

本模块**不**声明 ``DUMMY_INPUT`` / ``BUILD_FN`` / ``KNOBS``——那些是每个变体自己的契约
（DUMMY_INPUT 维度由用户真实模型 I/O 决定，禁硬编码回退）。

I/O 契约（所有 model8 变体一致）：
  - 输入 ``[B, num_ports, num_subcarriers, num_symbols, 1]``
  - 输出同形
  - 内部自理 alpha 功率归一（``x = inp/(sqrt(mean(inp²)·2)+1e-6)``，出口 ``*alpha``）
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F


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
        batch, num_syms, embed_dim, num_subs = x.shape
        x_a = self.m_a(x)
        x_f_f = torch.reshape(x_a, [batch * num_syms, -1, num_subs])
        x_p = self.proj(x_f_f)
        x_p = torch.reshape(x_p, [batch, num_syms, embed_dim, num_subs])
        x = x_p + x
        x_m_c = self.m_c(x)
        x = x_m_c + x
        return x


class SignalProcessingTransformer(nn.Module):
    """model8 主体。``block_mtypes`` 显式给出每个 block 的 attention 类型（变体完全掌控模式）。

    ``block_mtypes`` 长度 = block 数；例：``["t1"]*3``（全 t1）/ ``["t1","t2","t1"]``（交替）。
    """

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
        """OFD/FitNets 特征对齐 hook 名（**恒为 2 个**，与 teacher 等长）。

        取首层 + 中间层 block。``num_blocks=1`` 时无中间层，第二个 hook 重复
        ``main.0``——保持与 teacher（固定 2 hook）等长，否则 ``kd.compose.prepare``
        会因 student/teacher 特征数不等 raise（OFD/FitNets/RKD 要求等长）。
        """
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


class DilatedResBlock(nn.Module):
    """DeepRx 风格 dilated conv 残差块（**标准 Conv1d，禁 DW**——DW 在昇腾饿死 Cube）。

    hourglass dilation {1,2,4,8} 串联、kernel=3，等效感受野 RF=31（覆盖 ~64% 子载波），
    参数量与单层 3-tap 相同。输入输出同形 ``[B, num_symbols, embed_dim, num_subcarriers]``，
    内部 reshape 到 ``[B*num_symbols, embed_dim, num_subcarriers]`` 走 Conv1d。
    padding=d 保长度不变，残差可直接加。``num_symbols``/``num_subcarriers`` 仅 introspection
    用，forward 靠输入张量 shape 推断（故 U-Net bottleneck 可在 ``F//2`` 上复用本块）。
    """

    def __init__(self, embed_dim, num_symbols, num_subcarriers, dilations=(1, 2, 4, 8)):
        super().__init__()
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.layers = nn.ModuleList([
            nn.Sequential(
                nn.Conv1d(embed_dim, embed_dim, kernel_size=3, padding=d, dilation=d, bias=False),
                nn.BatchNorm1d(embed_dim),
                nn.ReLU(inplace=True),
            )
            for d in dilations
        ])

    def forward(self, x):
        # x: [B, num_symbols, embed_dim, num_subcarriers]
        B, S, C, F_ = x.shape
        h = x.reshape(B * S, C, F_)
        for layer in self.layers:
            h = layer(h)
        h = h.reshape(B, S, C, F_)
        return h + x


class DelayDomainSoftThreshold(nn.Module):
    """沿频率轴 FFT → soft-threshold(τ) → IFFT，补 pointwise 化丢失的频率选择性。

    物理先验：多径信道在 delay 域稀疏（ℓ1），soft-threshold 显式压小径噪声、保大径相位。
    ``τ→0`` 时早退 identity（fail-forward：训练起步不扰动、部署可关、不增推理开销）。
    输入输出 ``[B, num_symbols, embed_dim, num_subcarriers]``。

    ⚠️ 硬件：``τ≠0`` 时走 ``torch.fft`` 路径——GPU 训练 OK；若训练迁昇腾且 ``torch_npu``
    不支持 FFT（complex dtype 可能落慢路径），应冻结 ``tau.requires_grad_(False)`` 使 ``τ≡0``
    恒走 identity。部署测 latency 时 ``τ`` 收敛≈0 也不跑 FFT。
    """

    def __init__(self, embed_dim, init_tau=0.0):
        super().__init__()
        self.embed_dim = embed_dim
        self.tau = nn.Parameter(torch.tensor(float(init_tau)))

    def forward(self, x):
        if self.tau.abs() < 1e-8:
            return x
        x_f = torch.fft.rfft(x.float(), dim=-1)
        mag = torch.abs(x_f)
        phase = torch.angle(x_f)
        mag_thr = F.relu(mag - self.tau)
        x_f_thr = mag_thr * torch.exp(1j * phase)
        x_t = torch.fft.irfft(x_f_thr, n=x.shape[-1], dim=-1)
        return x_t.to(x.dtype)
