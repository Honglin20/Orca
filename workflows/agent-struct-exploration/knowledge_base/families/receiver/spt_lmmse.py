"""spt_lmmse.py —— model8 变体（线性前置 + NN 残差，D10 LMMSE 的简化版）。

KD-NAS student 候选：可学线性前置（近似 LMMSE 的线性均衡先验）+ NN 残差。输出 = 线性粗重建
+ β·NN 残差（β 初始小，先依赖线性、再学非线性残差）。思想同 D10 residual-around-LMMSE
（KB direction + raw ``residual_around_lmmse.py.md``），但**简化**：

- 不用真 LMMSE 闭式解（需 pilot + 信道统计协方差，是用户物理设定），改用**可学 1×1 线性层**
  近似 LMMSE 的线性先验（per-subcarrier 的 ports 线性组合 ≈ 线性均衡）。
- **输出信号重建口径**（≈ 干净发送信号），与 teacher 同语义（KB raw 原输出信道估计 ĥ，语义错位、
  无法直接 KD）；pilot 抽取步骤移除（无 pilot 依赖）。

代价：失去 LMMSE 闭式解的物理稳定性，``lin_front`` 靠训练学一个近 LMMSE 均衡。后续若有 pilot +
信道统计，可升级回真 LMMSE 前置。

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


class LinearResidualReceiver(nn.Module):
    """线性前置（近似 LMMSE 均衡）+ NN 残差。

    ``out = lin_front(x) + β · r_out(main(e_lyr(x)))``；``lin_front`` 是 Conv1d(P,P,k=1)
    per-subcarrier 线性均衡（近似 LMMSE 先验）；``β`` 初始 0.1，让训练起步 out ≈ 线性粗重建，
    NN 只补非线性残差。
    """

    def __init__(self, in_channels=_IN_CHANNELS, embed_dim=16, num_symbols=_NUM_SYMBOLS,
                 num_subcarriers=_NUM_SUBCARRIERS, bias_flag=True, num_blocks=4):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.lin_front = nn.Conv1d(in_channels, in_channels, kernel_size=1, bias=bias_flag)
        self.e_lyr = nn.Conv1d(in_channels, embed_dim, kernel_size=3, padding=1, bias=bias_flag)
        self.main = nn.Sequential(*[
            DilatedResBlock(embed_dim, num_symbols, num_subcarriers) for _ in range(num_blocks)
        ])
        self.r_out = nn.Conv1d(embed_dim, in_channels, kernel_size=3, padding=1, bias=bias_flag)
        self.beta = nn.Parameter(torch.tensor(0.1))

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
        h = x.permute(0, 3, 1, 2).reshape(B * S, P, F_)        # [B*S, P, F]
        lin = self.lin_front(h)                                 # [B*S, P, F] 线性粗重建
        e = self.e_lyr(h).reshape(B, S, -1, F_)                 # [B, S, C, F]
        e = self.main(e).reshape(B * S, -1, F_)                 # [B*S, C, F]
        delta = self.r_out(e)                                   # [B*S, P, F] NN 残差
        out = lin + self.beta * delta                           # [B*S, P, F]
        out = out.reshape(B, S, P, F_).permute(0, 2, 3, 1)      # [B, P, F, S]
        out = out * alpha
        return torch.unsqueeze(out, dim=-1)


def build_model(**cfg) -> nn.Module:
    """实例化线性残差变体（D10 简化版）。cfg 取 num_blocks / embed_dim。"""
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    return LinearResidualReceiver(embed_dim=embed_dim, num_blocks=num_blocks)
