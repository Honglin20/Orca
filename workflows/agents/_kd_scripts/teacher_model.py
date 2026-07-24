"""teacher_model.py —— KD-NAS teacher（10 层 model8，t1/t2 attention 交替）。

来源：``nas-agent/model8/model/baseline_model.py`` 的 ``SignalProcessingTransformer``
（4 层全 t1）。本文件是 **teacher**：10 层 ``SignalTransformerBlock``，``m_type`` 交替
``t1``（symbol 轴 attention，scale = num_subcarriers^-0.5 = 48^-0.5）与 ``t2``（subcarrier 轴
attention，scale = embed_dim^-0.5 = 16^-0.5），其余逐字同 baseline。

teacher 在 KD-NAS 中**只作 KD 软标签源**（精度基线由用户另给绝对值），由 setup 节点
从头训（``teacher_train_command``）后缓存为 ``teacher_cache.pt``。

I/O 契约（与所有 student 变体一致）：
  - 输入 ``[B, num_ports=4, num_subcarriers=48, num_symbols=64, 1]``
  - 输出同形 ``[B, 4, 48, 64, 1]``
  - 内部自理 alpha 功率归一（``x = inp/(sqrt(mean(inp²)·2)+1e-6)``，出口 ``*alpha``）
  - ``build_model(**cfg)`` 零参或带 ``num_layers`` 返回 ``nn.Module``
  - ``DUMMY_INPUT`` = 用户指定的真实输入维度（**禁硬编码回退**，见 setup 探测/用户给）
  - ``feature_hook_names()`` 供 OFD/FitNets 特征对齐（teacher 侧由 teacher_setup 注册 hook）
"""

from __future__ import annotations

import torch
import torch.nn as nn


class SignalAttention1D(nn.Module):
    def __init__(self, embed_dim, num_symbols, num_subcarriers, b_flg=True, m_type="t1"):
        super().__init__()
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.m_type = m_type

        self.s = num_subcarriers ** -0.5 if m_type == "t1" else embed_dim ** -0.5

        self.ln = nn.LayerNorm([embed_dim, num_symbols, num_subcarriers], elementwise_affine=False)
        self.sm = nn.Softmax(dim=-1)

        self.p_lyr = nn.Conv1d(
            in_channels=embed_dim,
            out_channels=3 * embed_dim,
            kernel_size=3,
            padding=1,
            bias=b_flg
        )

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
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
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
    """Teacher：``num_layers`` 个 ``SignalTransformerBlock``，``m_type`` 交替 t1/t2。

    默认 ``num_layers=10``。block i 的 ``m_type`` = ``"t1"``（i 偶）/ ``"t2"``（i 奇），
    即 symbol 轴与 subcarrier 轴 attention 轮换——「两个 attention 机制交替」。
    """

    def __init__(self, in_channels=4, embed_dim=16, num_symbols=64, num_subcarriers=48,
                 bias_flag=True, num_layers=10):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.b_flg = bias_flag
        self.num_layers = num_layers

        self.e_lyr = nn.Conv1d(in_channels=self.in_channels, out_channels=self.embed_dim,
                               kernel_size=3, padding=1, bias=self.b_flg)

        self.main = nn.Sequential(*[
            SignalTransformerBlock(self.embed_dim, self.num_symbols, self.num_subcarriers,
                                   m_type="t1" if i % 2 == 0 else "t2")
            for i in range(self.num_layers)
        ])

        self.r_out = nn.Conv1d(in_channels=self.embed_dim, out_channels=self.in_channels,
                               kernel_size=3, padding=1, bias=self.b_flg)

    def feature_hook_names(self) -> list[str]:
        """OFD/FitNets 特征对齐用的子模块名（≥1）。取首层与中间层 block。"""
        mid = max(1, self.num_layers // 2)
        return [f"main.0", f"main.{mid}"]

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


# ---------------------------------------------------------------------------
# I/O 契约（与 student 变体一致；DUMMY_INPUT 维度由用户真实模型 I/O 决定）。
# ---------------------------------------------------------------------------
BUILD_FN = "build_model"
DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}


def build_model(**cfg) -> nn.Module:
    """实例化 teacher。默认 10 层 t1/t2 交替；cfg 可覆盖结构超参（一般不动）。"""
    return SignalProcessingTransformer(**cfg)


if __name__ == "__main__":
    # 运行示例（smoke）：默认 10 层 teacher 的前向 + 输出 shape 校验。
    model = build_model()
    model.eval()
    B, num_ports, num_subcarriers, num_symbols = 1, 4, 48, 64
    dummy_input = torch.randn(B, num_ports, num_subcarriers, num_symbols, 1)
    with torch.no_grad():
        try:
            output = model(dummy_input)
            assert output.shape == dummy_input.shape, (output.shape, dummy_input.shape)
            # 块数断言：恰好 num_layers 个 SignalTransformerBlock，且交替 t1/t2。
            blocks = [m for m in model.main]
            assert len(blocks) == model.num_layers, len(blocks)
            mtypes = [b.m_a.m_type for b in blocks]
            assert mtypes == ["t1", "t2"] * (model.num_layers // 2) + (
                ["t1"] if model.num_layers % 2 else []), mtypes
            print(f"OK: {model.num_layers} blocks, m_type={mtypes[:4]}..., out={tuple(output.shape)}")
        except Exception as e:
            print(f"FAIL: {e}")
            raise
