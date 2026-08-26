"""pure_cnn_model.py —— rx-sweep fixture 的纯 CNN 族模型（DualAxisConv）。

实现 contracts.md §1 的接口契约：
  - ``DUMMY_INPUT`` / ``OUTPUT_SHAPE`` / ``BUILD_FN`` / ``build_model``
  - ``forward([B,4,48,64,1]) -> [B,4,48,64,1]``，内部 alpha 功率归一（逐位对齐 model8）
  - variant sugar：``pure_cnn`` / ``pure_cnn_pilot`` / ``pure_cnn_lmmse`` / ``pure_cnn_pilot_lmmse``
  - ``feature_hook_names() -> ["main.0", "main.<mid>"]``（恒 2 个，与 teacher 等长）

结构（DualAxisConvBlock）：频率分支 ``Conv1d-k3 → BN → ReLU → Conv1d-k3``（混 3 邻域子载波）
+ 时间分支 ``Conv1d-k3 dilation=d → BN → ReLU → Conv1d-k3 dilation=d``（混邻近 symbol）+ 残差。
**标准 dense conv，禁 DW**（昇腾 Cube 饿死）。

pilot 富化与 LMMSE 全在 forward 内部，I/O 不变。

本文件是 fixture 简化副本，权威实现见 scripts/models/，接口契约见 contracts.md。
"""

from __future__ import annotations

import torch
import torch.nn as nn

# ---------- fixture 契约 ----------
DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
OUTPUT_SHAPE = [1, 4, 48, 64, 1]
BUILD_FN = "build_model"

# variant → 开关映射（contracts §1）
_VARIANT_SWITCHES = {
    "pure_cnn":              (False, False),
    "pure_cnn_pilot":        (True,  False),
    "pure_cnn_lmmse":        (False, True),
    "pure_cnn_pilot_lmmse":  (True,  True),
}


class DualAxisConvBlock(nn.Module):
    """双轴卷积残差块：频率轴 + 时间轴（dilation）混邻域，替 attention 的全局相关。

    输入输出 ``[B, num_symbols, embed_dim, num_subcarriers]``。
    """

    def __init__(self, embed_dim, num_symbols, num_subcarriers, dilation=1):
        super().__init__()
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        # 频率分支：spatial = 子载波轴 F
        self.freq_branch = nn.Sequential(
            nn.Conv1d(embed_dim, embed_dim, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm1d(embed_dim),
            nn.ReLU(inplace=True),
            nn.Conv1d(embed_dim, embed_dim, kernel_size=3, padding=1, bias=False),
        )
        # 时间分支：spatial = symbol 轴 S，dilation 混邻近 symbol
        pad = dilation
        self.time_branch = nn.Sequential(
            nn.Conv1d(embed_dim, embed_dim, kernel_size=3, padding=pad, dilation=dilation, bias=False),
            nn.BatchNorm1d(embed_dim),
            nn.ReLU(inplace=True),
            nn.Conv1d(embed_dim, embed_dim, kernel_size=3, padding=pad, dilation=dilation, bias=False),
        )

    def forward(self, x):
        # x: [B, S, C, F]
        B, S, C, F_ = x.shape
        # 频率分支
        h_f = x.reshape(B * S, C, F_)
        h_f = self.freq_branch(h_f).reshape(B, S, C, F_)
        # 时间分支（把 S 拿到 spatial 末位）
        x_sf = x.permute(0, 3, 2, 1).reshape(B * F_, C, S)
        h_t = self.time_branch(x_sf).reshape(B, F_, C, S).permute(0, 3, 2, 1)
        # 双残差并联求和
        return x + h_f + h_t


class PureCNN(nn.Module):
    """纯 CNN 族主体。forward I/O 逐位对齐 model8（含 alpha 功率归一）。"""

    def __init__(self, num_blocks=4, embed_dim=16, dilations=(1, 2, 4, 8),
                 use_pilot_enrich=False, use_lmmse=False, noise_var=1e-2,
                 pilot_mask=None, pilot_values=None,
                 in_channels=4, num_symbols=64, num_subcarriers=48):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.use_pilot_enrich = use_pilot_enrich
        self.use_lmmse = use_lmmse
        self.noise_var = float(noise_var)
        self.dilations = tuple(dilations)

        # 固定导频栅格（buffer，随 .to(device) 走）。未给则构造确定性默认栅格。
        if pilot_mask is None:
            pilot_mask = self._default_pilot_mask(in_channels, num_subcarriers, num_symbols)
        if pilot_values is None:
            pilot_values = self._default_pilot_values(in_channels, num_subcarriers, num_symbols)
        self.register_buffer("pilot_mask", pilot_mask.bool())
        self.register_buffer("pilot_values", pilot_values.float())

        # pilot 富化把通道扩 4 倍：[Y, Y⊙Xp*, Xp, mask]
        e_in = in_channels * 4 if use_pilot_enrich else in_channels
        self.e_lyr = nn.Conv1d(in_channels=e_in, out_channels=embed_dim,
                               kernel_size=3, padding=1, bias=True)
        self.main = nn.Sequential(*[
            DualAxisConvBlock(embed_dim, num_symbols, num_subcarriers,
                              dilation=self.dilations[i % len(self.dilations)])
            for i in range(num_blocks)
        ])
        self.r_out = nn.Conv1d(in_channels=embed_dim, out_channels=in_channels,
                               kernel_size=3, padding=1, bias=True)

    @staticmethod
    def _default_pilot_mask(num_ports, num_subcarriers, num_symbols):
        """确定性默认导频栅格：每 4 子载波 × 每 8 symbol 取一个 pilot。"""
        mask = torch.zeros(num_ports, num_subcarriers, num_symbols, dtype=torch.bool)
        mask[:, ::4, ::8] = True
        return mask

    @staticmethod
    def _default_pilot_values(num_ports, num_subcarriers, num_symbols):
        """确定性默认导频值：pilot 位置取 1.0（单位幅度），其余 0。"""
        v = torch.zeros(num_ports, num_subcarriers, num_symbols)
        v[:, ::4, ::8] = 1.0
        return v

    def feature_hook_names(self) -> list[str]:
        """KD/FitNets 特征对齐 hook 名（恒 2 个，与 teacher 等长）。"""
        n = len(self.main)
        mid = max(1, n // 2) if n > 1 else 0
        second = f"main.{mid}" if n > 1 else "main.0"
        return ["main.0", second]

    def _lmmse_equalize(self, x):
        """闭式 LMMSE 均衡（fixture 简化版，非真实信号处理）。

        pilot 位置：H_est = Y·Xp / (Xp² + σ²)（收缩估计）；
        非 pilot 位置：identity（真实代码应做 H 的频域插值，fixture 跳过）。
        NN 学残差 = Y - H_est_on_pilot。
        """
        xp = self.pilot_values  # [P, F, S]
        denom = xp * xp + self.noise_var
        h_est = x * xp / denom   # xp=0 处 h_est=0 → 残差 = x 自身
        return x - h_est

    def _pilot_enrich(self, x):
        """pilot 富化：[Y, Y⊙Xp*, Xp, mask] 沿通道维 concat（实数 fixture：conj=Xp）。"""
        mask_f = self.pilot_mask.to(x.dtype)
        y_xp = x * self.pilot_values.unsqueeze(0)   # Y⊙Xp*
        xp_exp = self.pilot_values.unsqueeze(0).expand_as(x)
        mask_exp = mask_f.unsqueeze(0).expand_as(x)
        return torch.cat([x, y_xp, xp_exp, mask_exp], dim=1)

    def forward(self, inp: torch.Tensor):
        # [B, P, F, S, 1] → [B, P, F, S]
        if inp.dim() == 5 and inp.shape[-1] == 1:
            inp = torch.squeeze(inp, dim=-1)
        B, P, F_, S = inp.shape
        alpha = torch.sqrt(torch.mean(inp ** 2, dim=[1, 2, 3], keepdim=True) * 2)
        x = inp / (alpha + 1e-6)

        if self.use_lmmse:
            x = self._lmmse_equalize(x)
        if self.use_pilot_enrich:
            x = self._pilot_enrich(x)   # [B, P*4, F, S]

        # 对齐 model8 内部布局：[B, S, C, F]
        x = x.permute(0, 3, 1, 2)
        C_in = x.shape[2]
        x = torch.reshape(x, [B * S, C_in, F_])
        x = self.e_lyr(x)
        x = torch.reshape(x, [B, S, -1, F_])
        x = self.main(x)
        x = torch.reshape(x, [B * S, -1, F_])
        x = self.r_out(x)
        x = torch.reshape(x, [B, S, P, F_])
        x = x.permute(0, 2, 3, 1)
        x = x * alpha
        x = torch.unsqueeze(x, dim=-1)
        return x


def build_model(**cfg) -> nn.Module:
    """零参用默认；cfg 覆盖。contracts §1 的全部 cfg key 都接受。

    variant sugar：``variant="pure_cnn_pilot"`` 等价于设 ``use_pilot_enrich=True``；
    显式开关 override variant sugar。
    """
    variant = cfg.get("variant", "pure_cnn")
    if variant not in _VARIANT_SWITCHES:
        raise ValueError(
            f"pure_cnn_model.build_model: unknown variant={variant!r}; "
            f"allowed={list(_VARIANT_SWITCHES)}"
        )
    default_pilot, default_lmmse = _VARIANT_SWITCHES[variant]

    use_pilot_enrich = cfg.get("use_pilot_enrich", default_pilot)
    use_lmmse = cfg.get("use_lmmse", default_lmmse)
    num_blocks = cfg.get("num_blocks", 4)
    embed_dim = cfg.get("embed_dim", 16)
    dilations = cfg.get("dilations", (1, 2, 4, 8))
    noise_var = cfg.get("noise_var", 1e-2)
    pilot_mask = cfg.get("pilot_mask", None)
    pilot_values = cfg.get("pilot_values", None)
    in_channels = cfg.get("in_channels", 4)
    num_symbols = cfg.get("num_symbols", 64)
    num_subcarriers = cfg.get("num_subcarriers", 48)

    return PureCNN(
        num_blocks=num_blocks,
        embed_dim=embed_dim,
        dilations=dilations,
        use_pilot_enrich=use_pilot_enrich,
        use_lmmse=use_lmmse,
        noise_var=noise_var,
        pilot_mask=pilot_mask,
        pilot_values=pilot_values,
        in_channels=in_channels,
        num_symbols=num_symbols,
        num_subcarriers=num_subcarriers,
    )
