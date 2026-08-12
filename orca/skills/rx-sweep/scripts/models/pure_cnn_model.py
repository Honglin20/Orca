"""pure_cnn_model.py —— rx-sweep 纯 CNN 接收机模型族（交付物，拷进用户工程）。

逐字实现 ``orca/skills/rx-sweep/reference/contracts.md`` §1 的接口契约::

    DUMMY_INPUT  = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
    OUTPUT_SHAPE = [1, 4, 48, 64, 1]
    BUILD_FN     = "build_model"

含 4 个 variant（pure_cnn / pure_cnn_pilot / pure_cnn_lmmse / pure_cnn_pilot_lmmse），
对应 pilot 富化、LMMSE 前置两优化点的开关组合。

设计约束（why）：
- 纯 torch，自包含 —— 本文件会被拷进用户工程，
  用户工程里没有 Orca 源码，任何 Orca import 都会让用户工程崩。
- forward 逐位对齐 model8（``SignalProcessingTransformer``）的 I/O 行为：输入
  ``[B,4,48,64,1]`` → squeeze 尾维 → alpha 功率归一（``alpha=sqrt(mean(inp²)·2)``，
  出口 ``*alpha``）→ permute/reshape → 主干 → 还原 → unsqueeze。alpha/permute/reshape
  模式照抄 model8，确保 I/O shape 与功率尺度与 teacher 完全对齐（KD/对比实验的
  可比性命门，差一点 alpha 就让 FitNets 特征对齐全错位）。
- DualAxisConvBlock 用标准 dense Conv1d（**禁 depthwise/group**），昇腾 Cube 不饿死。
- fail loud：开关组合非法 / shape 不对齐 / pilot 缺失但 ``use_pilot_enrich=True`` →
  raise，绝不静默兜底（静默兜底会让 gate 看似 PASS 但实验全错）。
"""

from __future__ import annotations

from typing import Sequence

import torch
import torch.nn as nn

__all__ = [
    "DUMMY_INPUT",
    "OUTPUT_SHAPE",
    "BUILD_FN",
    "DualAxisConvBlock",
    "PilotEnrich",
    "LMMSEFront",
    "PureCNNReceiver",
    "build_model",
]


# ---------------------------------------------------------------------------
# 契约常量（contracts §1）
# ---------------------------------------------------------------------------
DUMMY_INPUT: dict = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
OUTPUT_SHAPE: list = [1, 4, 48, 64, 1]
BUILD_FN: str = "build_model"


# variant → 开关映射（contracts §1）。build_model 据此 sugar 展开。
VARIANT_MAP = {
    "pure_cnn":             {"use_pilot_enrich": False, "use_lmmse": False},
    "pure_cnn_pilot":       {"use_pilot_enrich": True,  "use_lmmse": False},
    "pure_cnn_lmmse":       {"use_pilot_enrich": False, "use_lmmse": True},
    "pure_cnn_pilot_lmmse": {"use_pilot_enrich": True,  "use_lmmse": True},
}


# ---------------------------------------------------------------------------
# DualAxisConvBlock —— 频率分支 + 时间分支（dilated）+ 残差
# ---------------------------------------------------------------------------
class DualAxisConvBlock(nn.Module):
    """双轴 Conv1d 残差块：频率分支（局部子载波）+ 时间分支（dilated symbol）+ 残差。

    输入输出同形 ``[B, num_symbols, embed_dim, num_subcarriers]``（与 model8 的
    ``SignalTransformerBlock`` 完全一致，便于在 ``main`` 序列里平替）。

    - **频率分支**：沿子载波轴（F）k=3 dense conv，混 3 邻域子载波，承重局部先验
      （model8 原有的局部先验不动）。
    - **时间分支**：沿 symbol 轴（S）k=3 dilation=d dense conv，混邻近 symbol，
      替 attention 的全局时间相关；dilation 跨 block 翻倍，RF 覆盖 ~64 symbol。
    - **标准 dense Conv1d，禁 depthwise/group**（昇腾 Cube 饿死）。

    两分支内部都走 ``e→2e→e`` 通道变化（``BN1d + ReLU`` 中间），输出再加残差。
    """

    def __init__(self, embed_dim: int, num_symbols: int,
                 num_subcarriers: int, dilation: int = 1):
        super().__init__()
        if embed_dim % 16 != 0:
            raise ValueError(
                f"embed_dim 必须 ÷16（昇腾 Cube 对齐），got embed_dim={embed_dim}"
            )
        if dilation < 1:
            raise ValueError(f"dilation 须 ≥ 1，got {dilation}")

        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.dilation = dilation

        # 频率分支：F 为长度轴，dense k=3 pad=1（保长度，无 dilation）。
        self.freq_branch = nn.Sequential(
            nn.Conv1d(embed_dim, 2 * embed_dim, kernel_size=3, padding=1),
            nn.BatchNorm1d(2 * embed_dim),
            nn.ReLU(inplace=True),
            nn.Conv1d(2 * embed_dim, embed_dim, kernel_size=3, padding=1),
        )

        # 时间分支：S 为长度轴，dense k=3 dilation=d pad=d（保长度）。
        self.time_branch = nn.Sequential(
            nn.Conv1d(embed_dim, 2 * embed_dim, kernel_size=3,
                      padding=dilation, dilation=dilation),
            nn.BatchNorm1d(2 * embed_dim),
            nn.ReLU(inplace=True),
            nn.Conv1d(2 * embed_dim, embed_dim, kernel_size=3,
                      padding=dilation, dilation=dilation),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, S, C, F]
        B, S, C, F_ = x.shape

        # ---- 频率分支：reshape 到 [B*S, C, F]，F 作长度轴 ----
        h_f = x.reshape(B * S, C, F_)
        h_f = self.freq_branch(h_f)
        h_f = h_f.reshape(B, S, C, F_)

        # ---- 时间分支：把 S 摆成长度轴 [B*F, C, S] ----
        # [B, S, C, F] → permute(0,3,2,1) = [B, F, C, S] → reshape [B*F, C, S]
        h_t = x.permute(0, 3, 2, 1).contiguous().reshape(B * F_, C, S)
        h_t = self.time_branch(h_t)
        # [B*F, C, S] → reshape [B, F, C, S] → permute(0,3,2,1) = [B, S, C, F]
        h_t = h_t.reshape(B, F_, C, S).permute(0, 3, 2, 1).contiguous()

        # 残差合并：x + 频率 + 时间
        return x + h_f + h_t


# ---------------------------------------------------------------------------
# PilotEnrich —— [Y, Y⊙Xp*, Xp, mask] 拼多通道（固定导频栅格前提）
# ---------------------------------------------------------------------------
class PilotEnrich(nn.Module):
    """把导频信息显式拼进输入通道：``[Y, Y⊙Xp*, Xp, mask]`` 沿通道维 concat。

    输入输出（forward）：
      - ``x``:           ``[B, P, F, S]`` 实数（squeeze 尾维后的 Y）
      - ``pilot_mask``:   ``[P, F, S]`` bool，固定导频栅格
      - ``pilot_values``: ``[P, F, S]`` 实数，已知导频值 Xp
      - 返回: ``[B, 4P, F, S]`` —— 4 倍通道（Y / Y⊙Xp* / Xp / mask 各 P 通道）

    Why：
    - 把导频信息显式喂给模型，模型不用自己「找导频」，pilot 利用率提满；近零精度风险。
    - 实数域下 ``Xp* = Xp``（复共轭退化），代码仍写 ``Y * Xp`` 以表达原意。
    - ``Xp`` 与 ``mask`` 跨 batch 共享（固定栅格），``expand`` 到 batch 维度即可，
      不复制存储。

    前提：``pilot_mask`` 是**固定栅格**（跨 batch / symbol 不变）；本类不做栅格推断，
    只做 elementwise 拼接。栅格一致性由 LMMSEFront 的 LS 估计隐式依赖。
    """

    def forward(self, x: torch.Tensor,
                pilot_mask: torch.Tensor,
                pilot_values: torch.Tensor) -> torch.Tensor:
        # shape 校验（fail loud）
        if x.dim() != 4:
            raise ValueError(
                f"PilotEnrich: x 须为 4D [B,P,F,S]，got shape={tuple(x.shape)}"
            )
        B, P, F_, S = x.shape
        if pilot_mask.shape != (P, F_, S):
            raise ValueError(
                f"PilotEnrich: pilot_mask shape 应为 ({P},{F_},{S})，"
                f"got {tuple(pilot_mask.shape)}"
            )
        if pilot_values.shape != pilot_mask.shape:
            raise ValueError(
                f"PilotEnrich: pilot_values shape 须同 pilot_mask，"
                f"got {tuple(pilot_values.shape)} vs {tuple(pilot_mask.shape)}"
            )

        mask_f = pilot_mask.to(x.dtype)   # [P, F, S]
        Xp = pilot_values.to(x.dtype)     # [P, F, S]
        # Y⊙Xp*（实数：Xp*=Xp）
        Y_Xp = x * Xp.unsqueeze(0)        # [B, P, F, S]
        Xp_b = Xp.unsqueeze(0).expand(B, -1, -1, -1)
        mask_b = mask_f.unsqueeze(0).expand(B, -1, -1, -1)
        return torch.cat([x, Y_Xp, Xp_b, mask_b], dim=1)


# ---------------------------------------------------------------------------
# LMMSEFront —— LS 信道估计 + 插值 + 闭式 LMMSE 滤波
# ---------------------------------------------------------------------------
class LMMSEFront(nn.Module):
    """闭式 LMMSE 前置均衡器：``X̂_LMMSE = W·Y``，``W = Ĥᴴ(ĤĤᴴ+σ²I)⁻¹``。

    流程（deterministic, differentiable）：
      1. **LS 估计**：在 pilot 位置 ``Ĥ_pilot = Y[mask] / Xp[mask]``。
      2. **插值**：沿子载波轴 linear interp 到所有 F 位置（固定栅格前提；外推用
         zero-order hold —— 边界 pilot 之外保持最近 pilot 值）。
      3. **LMMSE 滤波**：per ``(B, port, symbol)`` 把 Ĥ 视为对角信道矩阵，
         ``A = diag(Ĥ² + σ²)``，``W = solve(A, Ĥ)``，``X̂ = W ⊙ Y`` —— 数学等价于
         闭式 ``x̂[k] = Ĥ[k]·Y[k] / (Ĥ[k]² + σ²)``，但走 ``torch.linalg.solve`` 以
         可微 + 可扩展到非对角 H（多流 MIMO / ICI 等场景只改 H 构造，不动 LMMSE 公式）。

    Why matrix form：contracts §1 显式要求 ``W=Ĥᴴ(ĤĤᴴ+σ²I)⁻¹``，用 solve/pinverse，
    以便后续扩展只改 H 构造，LMMSE 公式不动。
    """

    def __init__(self, num_subcarriers: int, noise_var: float = 1e-2):
        super().__init__()
        if noise_var < 0:
            raise ValueError(f"noise_var 须 ≥ 0，got {noise_var}")
        self.num_subcarriers = int(num_subcarriers)
        self.noise_var = float(noise_var)

    @staticmethod
    def _ls_and_interp(Y: torch.Tensor,
                       pilot_mask: torch.Tensor,
                       pilot_values: torch.Tensor) -> torch.Tensor:
        """LS at pilots + linear interp along F。返回 Ĥ ∈ [B, P, F, S]。"""
        # Y: [B, P, F, S]
        B, P, F_, S = Y.shape
        # 安全除：非 pilot 处填 1（结果再 mask 掉）
        safe_Xp = torch.where(pilot_mask, pilot_values,
                              torch.ones_like(pilot_values))
        h_pilot = torch.where(pilot_mask, Y / safe_Xp,
                              torch.zeros_like(Y))  # [B, P, F, S]

        # 提取 pilot 沿 F 轴的索引（固定栅格前提：所有 (p,s) 共用）。
        # 用 mask[0, :, 0] 推断 —— 若栅格不一致，结果会近似但不崩（前提已声明）。
        pilot_idx = torch.nonzero(pilot_mask[0, :, 0] > 0,
                                  as_tuple=False).squeeze(-1).to(torch.long)
        K = pilot_idx.numel()
        if K == 0:
            raise ValueError(
                "LMMSEFront: pilot_mask 在 F 轴上无导频 —— 无法 LS 估计"
            )
        knots = pilot_idx.to(Y.dtype)  # [K]

        # 收集每个 pilot 位置的 Ĥ：[B, P, K, S]
        h_knots = h_pilot[:, :, pilot_idx, :]

        # 在 F 轴上对每个位置算 bracketing pilot 索引
        f_coords = torch.arange(F_, device=Y.device, dtype=Y.dtype)  # [F]
        idx_right = torch.searchsorted(knots, f_coords, right=True)  # [F]
        idx_left = (idx_right - 1).clamp(0, K - 1)
        idx_right = idx_right.clamp(0, K - 1)

        t_left = knots[idx_left]    # [F]
        t_right = knots[idx_right]  # [F]
        # weight = (f - t_left) / (t_right - t_left)；零除保护 + 外推 clamp 到 [0,1]
        denom = (t_right - t_left)
        safe_denom = torch.where(denom == 0, torch.ones_like(denom), denom)
        weight = (f_coords - t_left) / safe_denom
        weight = torch.where(denom == 0, torch.zeros_like(weight), weight)
        weight = weight.clamp(0.0, 1.0).view(1, 1, F_, 1)  # broadcast [B,P,F,S]

        h_left = h_knots[:, :, idx_left, :]    # [B, P, F, S]
        h_right = h_knots[:, :, idx_right, :]  # [B, P, F, S]
        return h_left * (1.0 - weight) + h_right * weight

    def forward(self, Y: torch.Tensor,
                pilot_mask: torch.Tensor,
                pilot_values: torch.Tensor) -> torch.Tensor:
        """Y: ``[B, P, F, S]``，返回 ``X̂_LMMSE`` 同形。"""
        if Y.dim() != 4:
            raise ValueError(
                f"LMMSEFront: Y 须为 4D [B,P,F,S]，got shape={tuple(Y.shape)}"
            )
        B, P, F_, S = Y.shape
        if F_ != self.num_subcarriers:
            raise ValueError(
                f"LMMSEFront: F 维 {F_} ≠ 构造时 num_subcarriers "
                f"{self.num_subcarriers}"
            )

        h_hat = self._ls_and_interp(Y, pilot_mask, pilot_values)  # [B,P,F,S]
        # 重排到 (B*P*S, F)：每个 (B,P,S) 是一个独立的 F×F 系统
        h = h_hat.permute(0, 1, 3, 2).reshape(-1, F_)  # [N, F]
        y = Y.permute(0, 1, 3, 2).reshape(-1, F_)      # [N, F]

        # A = Ĥ Ĥᴴ + σ² I（实数域：Ĥᴴ=Ĥ；对角化为 diag(h² + σ²)）
        A = torch.diag_embed(h * h + self.noise_var)   # [N, F, F]
        # W = Ĥᴴ(ĤĤᴴ+σ²I)⁻¹ —— 走 solve 而非 inv，数值稳。
        # 实数下 W = solve(A, h)；复数下应是 solve(A, conj(h))，本模型全程实数。
        W = torch.linalg.solve(A, h.unsqueeze(-1)).squeeze(-1)  # [N, F]
        x = W * y                                                # [N, F]

        x = x.reshape(B, P, S, F_).permute(0, 1, 3, 2).contiguous()  # [B,P,F,S]
        return x


# ---------------------------------------------------------------------------
# PureCNNReceiver —— 主模型
# ---------------------------------------------------------------------------
class PureCNNReceiver(nn.Module):
    """rx-sweep 纯 CNN 接收机。

    forward I/O 逐位对齐 model8（``SignalProcessingTransformer``）：输入
    ``[B,4,48,64,1]`` → squeeze → alpha 功率归一 → permute/reshape → 主干
    → 还原 → ``*alpha`` → unsqueeze → ``[B,4,48,64,1]``。

    主干：``stem Conv1d → N × DualAxisConvBlock → out Conv1d``，全 dense Conv1d。
    优化点（forward 内部，I/O 不变）：
      - **use_pilot_enrich**：在 stem 前把输入 ``[Y, Y⊙Xp*, Xp, mask]`` 拼 16 通道。
      - **use_lmmse**：在 stem 前算 ``X̂_LMMSE``，主干学残差，最终输出
        ``X̂_LMMSE + NN残差``（均在 alpha 归一化域内，再 ``*alpha`` 出口）。
    """

    def __init__(
        self,
        num_blocks: int = 4,
        embed_dim: int = 16,
        dilations: Sequence[int] = (1, 2, 4, 8),
        use_pilot_enrich: bool = False,
        use_lmmse: bool = False,
        noise_var: float = 1e-2,
        pilot_mask: "torch.Tensor | None" = None,
        pilot_values: "torch.Tensor | None" = None,
        num_ports: int = 4,
        num_subcarriers: int = 48,
        num_symbols: int = 64,
    ):
        super().__init__()

        # ---- 参数校验（fail loud）----
        if embed_dim % 16 != 0:
            raise ValueError(
                f"embed_dim 必须 ÷16（昇腾 Cube 对齐），got embed_dim={embed_dim}"
            )
        if num_blocks < 1:
            raise ValueError(
                f"num_blocks 须 ≥ 1（main Sequential 至少 1 块才能挂 hook），"
                f"got {num_blocks}"
            )
        dilations = tuple(int(d) for d in dilations)
        if len(dilations) == 0:
            raise ValueError("dilations 不能为空")
        if any(d < 1 for d in dilations):
            raise ValueError(f"dilations 各元素须 ≥ 1，got {dilations}")

        # pilot/lmmse 开关组合的强校验
        if use_pilot_enrich and (pilot_mask is None or pilot_values is None):
            raise ValueError(
                "use_pilot_enrich=True 但 pilot_mask/pilot_values 为 None；"
                "pilot 富化需固定导频栅格 + 已知导频值。"
            )
        if use_lmmse and (pilot_mask is None or pilot_values is None):
            raise ValueError(
                "use_lmmse=True 但 pilot_mask/pilot_values 为 None；"
                "LMMSE 需导频位置 + 已知导频值做 LS 估计。"
            )

        # ---- 形参存档 ----
        self.num_blocks = int(num_blocks)
        self.embed_dim = int(embed_dim)
        self.dilations = dilations
        self.use_pilot_enrich = bool(use_pilot_enrich)
        self.use_lmmse = bool(use_lmmse)
        self.noise_var = float(noise_var)
        self.num_ports = int(num_ports)
        self.num_subcarriers = int(num_subcarriers)
        self.num_symbols = int(num_symbols)
        # 原始输入通道数（= num_ports；r_out 出口通道数）
        self.in_channels = int(num_ports)

        # ---- pilot 配置存 buffer（仅 enrich/lmmse 任一开时存，否则不污染 state_dict）----
        if (self.use_pilot_enrich or self.use_lmmse):
            # 此时 pilot_mask/pilot_values 已确保非 None（上面校验）
            self.register_buffer(
                "pilot_mask",
                _to_bool_buffer(pilot_mask,
                                (num_ports, num_subcarriers, num_symbols)),
            )
            self.register_buffer(
                "pilot_values",
                _to_real_buffer(pilot_values,
                                (num_ports, num_subcarriers, num_symbols)),
            )

        # ---- 子模块 ----
        if self.use_pilot_enrich:
            self.pilot_enrich = PilotEnrich()
            stem_in = self.in_channels * 4  # [Y, Y⊙Xp*, Xp, mask] 4 倍通道
        else:
            self.pilot_enrich = None
            stem_in = self.in_channels

        if self.use_lmmse:
            self.lmmse_front = LMMSEFront(num_subcarriers, noise_var=noise_var)
        else:
            self.lmmse_front = None

        # stem / out Conv1d（与 model8 的 e_lyr / r_out 同模式：dense k=3 pad=1）
        self.e_lyr = nn.Conv1d(stem_in, embed_dim,
                               kernel_size=3, padding=1, bias=True)
        # main：N × DualAxisConvBlock，dilation 按 dilations[i % len] 轮换
        self.main = nn.Sequential(*[
            DualAxisConvBlock(
                embed_dim, num_symbols, num_subcarriers,
                dilation=self.dilations[i % len(self.dilations)],
            )
            for i in range(self.num_blocks)
        ])
        self.r_out = nn.Conv1d(embed_dim, self.in_channels,
                               kernel_size=3, padding=1, bias=True)

    # ------------------------------------------------------------------
    # KD hook（contracts §1）—— 恒 2 个，与 model8 teacher 等长
    # ------------------------------------------------------------------
    def feature_hook_names(self) -> list[str]:
        """FitNets 特征 hook 名：恒 2 个（与 model8 teacher 等长）。

        取 ``main`` 首层 + 中间层；``num_blocks=1`` 时无中间层，第二个 hook 重复
        ``main.0``（保持与 teacher 等长，否则 ``kd.compose.prepare`` 会因
        student/teacher 特征数不等 raise）。
        """
        n = len(self.main)
        mid = max(1, n // 2) if n > 1 else 0
        second = f"main.{mid}" if n > 1 else "main.0"
        return ["main.0", second]

    # ------------------------------------------------------------------
    # forward
    # ------------------------------------------------------------------
    def forward(self, inp: torch.Tensor) -> torch.Tensor:
        # ---- 0. squeeze 尾维（[B,P,F,S,1] → [B,P,F,S]）----
        if inp.dim() == 5 and inp.shape[-1] == 1:
            inp = torch.squeeze(inp, dim=-1)
        if inp.dim() != 4:
            raise ValueError(
                f"forward: 期望 5D [B,P,F,S,1] 或 4D [B,P,F,S]，"
                f"got shape={tuple(inp.shape)}"
            )
        B, num_ports, num_subcarriers, num_symbols = inp.shape
        if (num_ports != self.num_ports
                or num_subcarriers != self.num_subcarriers
                or num_symbols != self.num_symbols):
            raise ValueError(
                f"forward: 输入 shape {tuple(inp.shape)} 与构造时 "
                f"(P,F,S)=({self.num_ports},{self.num_subcarriers},"
                f"{self.num_symbols}) 不符"
            )

        # ---- 1. alpha 功率归一（逐位对齐 model8）----
        alpha = torch.sqrt(torch.mean(inp ** 2, dim=[1, 2, 3], keepdim=True) * 2)
        x = inp / (alpha + 1e-6)  # [B, P, F, S]

        # ---- 2. optional LMMSE 前置（在 alpha 归一域内算）----
        # contracts §1：LMMSEFront 在 stem 前算，主干学残差。
        x_lmmse = None
        if self.use_lmmse:
            x_lmmse = self.lmmse_front(x, self.pilot_mask, self.pilot_values)

        # ---- 3. optional PilotEnrich（在 alpha 归一域内拼通道）----
        if self.use_pilot_enrich:
            x_in = self.pilot_enrich(x, self.pilot_mask, self.pilot_values)
        else:
            x_in = x
        # x_in: [B, stem_in, F, S]

        # ---- 4. permute/reshape 到 [B*S, stem_in, F]（照抄 model8）----
        x_in = x_in.permute(0, 3, 1, 2)  # [B, S, stem_in, F]
        x_in = torch.reshape(
            x_in, [B * num_symbols, x_in.shape[2], num_subcarriers]
        )

        # ---- 5. stem Conv1d ----
        x_in = self.e_lyr(x_in)  # [B*S, embed_dim, F]
        x_in = torch.reshape(
            x_in, [B, num_symbols, self.embed_dim, num_subcarriers]
        )

        # ---- 6. N × DualAxisConvBlock ----
        x_in = self.main(x_in)  # [B, S, embed_dim, F]

        # ---- 7. reshape + out Conv1d ----
        x_in = torch.reshape(
            x_in, [B * num_symbols, self.embed_dim, num_subcarriers]
        )
        x_in = self.r_out(x_in)  # [B*S, in_channels, F]
        x_in = torch.reshape(x_in, [B, num_symbols, num_ports, num_subcarriers])
        x_in = x_in.permute(0, 2, 3, 1)  # [B, P, F, S]

        # ---- 8. optional LMMSE 残差合并（X̂_LMMSE + NN残差）----
        if self.use_lmmse:
            x_in = x_lmmse + x_in

        # ---- 9. *alpha + unsqueeze（逐位对齐 model8）----
        x_in = x_in * alpha
        x_in = torch.unsqueeze(x_in, dim=-1)
        return x_in


# ---------------------------------------------------------------------------
# 辅助：pilot_mask / pilot_values 转模型 buffer（强 shape 校验）
# ---------------------------------------------------------------------------
def _to_bool_buffer(t: torch.Tensor, expected_shape: tuple) -> torch.Tensor:
    if not isinstance(t, torch.Tensor):
        raise TypeError(
            f"pilot_mask 须为 torch.Tensor，got {type(t).__name__}"
        )
    if t.shape != expected_shape:
        raise ValueError(
            f"pilot_mask shape 应为 {expected_shape}，got {tuple(t.shape)}"
        )
    return t.to(torch.bool)


def _to_real_buffer(t: torch.Tensor, expected_shape: tuple) -> torch.Tensor:
    if not isinstance(t, torch.Tensor):
        raise TypeError(
            f"pilot_values 须为 torch.Tensor，got {type(t).__name__}"
        )
    if t.shape != expected_shape:
        raise ValueError(
            f"pilot_values shape 应为 {expected_shape}，got {tuple(t.shape)}"
        )
    return t.to(torch.float32)


# ---------------------------------------------------------------------------
# build_model —— contracts §1 入口
# ---------------------------------------------------------------------------
def build_model(**cfg) -> nn.Module:
    """按 contracts §1 解析 cfg，构造 ``PureCNNReceiver``。

    - ``variant`` sugar 展开成 ``use_pilot_enrich`` / ``use_lmmse`` 组合
      （见 ``VARIANT_MAP``）。
    - 显式 ``use_pilot_enrich`` / ``use_lmmse`` override variant。
    - 零参用默认（``variant="pure_cnn"``，``num_blocks=4``，``embed_dim=16``，...）。
    - 未知 cfg key → raise（fail loud，防 typo 静默生效）。
    """
    cfg = dict(cfg)  # 不污染调用方

    variant = cfg.pop("variant", "pure_cnn")
    if variant not in VARIANT_MAP:
        raise ValueError(
            f"build_model: unknown variant {variant!r}；"
            f"预期 {sorted(VARIANT_MAP)}"
        )
    base = VARIANT_MAP[variant]

    # 显式开关 override variant
    use_pilot_enrich = cfg.pop("use_pilot_enrich", base["use_pilot_enrich"])
    use_lmmse = cfg.pop("use_lmmse", base["use_lmmse"])

    num_blocks = cfg.pop("num_blocks", 4)
    embed_dim = cfg.pop("embed_dim", 16)
    dilations = cfg.pop("dilations", (1, 2, 4, 8))
    noise_var = cfg.pop("noise_var", 1e-2)
    pilot_mask = cfg.pop("pilot_mask", None)
    pilot_values = cfg.pop("pilot_values", None)

    # 兼容 num_ports / num_subcarriers / num_symbols 显式覆盖（默认 4/48/64）
    num_ports = cfg.pop("num_ports", 4)
    num_subcarriers = cfg.pop("num_subcarriers", 48)
    num_symbols = cfg.pop("num_symbols", 64)

    if cfg:
        raise ValueError(
            f"build_model: 未识别的 cfg keys: {sorted(cfg)}（防 typo 静默生效）"
        )

    return PureCNNReceiver(
        num_blocks=num_blocks,
        embed_dim=embed_dim,
        dilations=dilations,
        use_pilot_enrich=use_pilot_enrich,
        use_lmmse=use_lmmse,
        noise_var=noise_var,
        pilot_mask=pilot_mask,
        pilot_values=pilot_values,
        num_ports=num_ports,
        num_subcarriers=num_subcarriers,
        num_symbols=num_symbols,
    )


# ---------------------------------------------------------------------------
# smoke：python pure_cnn_model.py 验全 variant
# ---------------------------------------------------------------------------
def _format_shape(shape) -> str:
    return "[" + ",".join(str(int(d)) for d in shape) + "]"


def _format_dilations(dilations) -> str:
    return "(" + ",".join(str(int(d)) for d in dilations) + ")"


def _smoke() -> int:
    torch.manual_seed(0)

    num_ports, num_subcarriers, num_symbols = 4, 48, 64

    # 固定导频栅格：每 4 个子载波一个 pilot，所有 (port, symbol) 共用
    pilot_mask = torch.zeros(num_ports, num_subcarriers, num_symbols,
                             dtype=torch.bool)
    pilot_mask[:, ::4, :] = True
    pilot_values = torch.ones(num_ports, num_subcarriers, num_symbols,
                              dtype=torch.float32)

    dummy = torch.randn(*DUMMY_INPUT["shape"], dtype=torch.float32)
    expected = list(OUTPUT_SHAPE)

    all_pass = True
    for variant in ["pure_cnn", "pure_cnn_pilot",
                    "pure_cnn_lmmse", "pure_cnn_pilot_lmmse"]:
        model = build_model(
            variant=variant,
            pilot_mask=pilot_mask, pilot_values=pilot_values,
        )
        # eval 模式跑（BN 用 running stats，单样本 smoke 也稳）
        model.eval()
        with torch.no_grad():
            out = model(dummy)

        flags = VARIANT_MAP[variant]
        pilot_on = "on" if flags["use_pilot_enrich"] else "off"
        lmmse_on = "on" if flags["use_lmmse"] else "off"

        passed = (list(out.shape) == expected)
        gate = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False

        print(
            f"[RX-GATE] variant={variant} pilot={pilot_on} lmmse={lmmse_on} "
            f"kd=off num_blocks={model.num_blocks} embed_dim={model.embed_dim} "
            f"dilations={_format_dilations(model.dilations)} "
            f"noise_var={model.noise_var:g} "
            f"io_in={_format_shape(dummy.shape)} "
            f"io_out={_format_shape(out.shape)} gate={gate}"
        )

    return 0 if all_pass else 1


if __name__ == "__main__":
    import sys
    sys.exit(_smoke())
