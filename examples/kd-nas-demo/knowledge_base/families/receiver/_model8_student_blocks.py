"""_model8_student_blocks.py —— kd-nas-demo KB 共享的【原始 model8 架构】student 积木（可配 norm/act）。

与 ``_demo_blocks.py`` 的关系：
  - ``_demo_blocks`` 是 **简化原创**（pointwise QKV/proj、去 LayerNorm），让 E2E 在 CPU 分钟级跑通。
  - 本模块是 **原始 model8 架构**（3-tap Conv1d QKV/proj + LayerNorm），逐字对齐
    ``workflows/agents/_kd_scripts/teacher_model.py`` 的 ``SignalAttention1D`` /
    ``SignalFeedForward1D`` / ``SignalTransformerBlock`` / ``SignalProcessingTransformer``，
    仅新增 ``norm_type`` / ``act_type`` 两可配维度（OCP：新组合 = 新 cfg，不改核心路径）。

为什么独立模块（DRY vs 自包含）：
  - demo KB 必须自包含便携（``_demo_blocks`` 注释同款理由），不能跨 KB import 真实
    ``knowledge_base/families/receiver/_model8_blocks.py``。
  - 真实 ``_model8_blocks`` 不可配 norm/act；本模块需要 BN/ReLU 替换以表达 user 真实场景
    （BN 代 LN / 缩层 / ReLU 代 GELU 三条轻量化路径）。架构本体逐字相同，仅多两个开关。

I/O 契约（所有变体一致；与 teacher / baseline 完全相同）：
  - 输入 ``[B, num_ports=4, num_subcarriers=48, num_symbols=64, 1]``
  - 输出同形
  - 内部自理 alpha 功率归一（``x = inp/(sqrt(mean(inp²)·2)+1e-6)``，出口 ``*alpha``）

BatchNorm 适配（关键，已 forward shape + backward 验证）：
  - 原 LayerNorm 作用于 4D（attention: ``[B, embed_dim, num_syms, num_subs]`` 经 permute；
    FFN: ``[B, num_syms, embed_dim, num_subs]``），对每 batch 的 3 个空间维归一。
  - BatchNorm1d 作用于 reshape 后的 3D ``[B*num_syms, embed_dim, num_subs]`` = ``[N, C, L]``，
    C = embed_dim（通道维），对 (N, L) 每 channel 归一——与 LN 语义不同但属标准 BN 替换
    （user 已在真实场景验证时延能达标）。
  - N = B*num_symbols = 1*64 = 64（DUMMY_INPUT 维度下），train-mode batch 统计稳健
    （无 batch=1 退化；``_demo_blocks.DilatedResBlock`` 去 BN 的 batch=1 顾虑在此不成立）。
"""

from __future__ import annotations

import torch
import torch.nn as nn

_VALID_NORM_TYPES = ("ln", "bn")
_VALID_ACT_TYPES = ("gelu", "relu")


class SignalAttention1D(nn.Module):
    """model8 attention（逐字同 teacher_model.SignalAttention1D + norm_type 可配）。

    ``m_type="t1"`` = symbol 轴 attention（scale = num_subcarriers^-0.5）；
    ``"t2"`` = subcarrier 轴 attention（scale = embed_dim^-0.5）。

    norm 作用点（``forward``）：
      - ``norm_type="ln"``：permute 到 ``[B, embed_dim, num_syms, num_subs]`` 后 LayerNorm（原始）。
      - ``norm_type="bn"``：reshape 到 ``[B*num_syms, embed_dim, num_subs]`` 后 BatchNorm1d
        （通道维 = embed_dim；与 LN 平行——都作用于 conv 输入前）。
    """

    def __init__(self, embed_dim, num_symbols, num_subcarriers, b_flg=True, m_type="t1", norm_type="ln"):
        super().__init__()
        if norm_type not in _VALID_NORM_TYPES:
            raise ValueError(
                f"norm_type={norm_type!r} 非法；须 ∈ {_VALID_NORM_TYPES}"
            )
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.m_type = m_type
        self.norm_type = norm_type

        self.s = num_subcarriers ** -0.5 if m_type == "t1" else embed_dim ** -0.5

        if norm_type == "ln":
            # 原始 model8：elementwise_affine=False（逐字对齐 teacher_model）。
            self.ln = nn.LayerNorm(
                [embed_dim, num_symbols, num_subcarriers], elementwise_affine=False
            )
        else:  # "bn"
            # BatchNorm1d(embed_dim) 跑通道维；默认 affine=True + track_running_stats=True
            # （标准 BN；trainable student 用 affine 获得 scale/shift）。
            self.bn = nn.BatchNorm1d(embed_dim)

        self.sm = nn.Softmax(dim=-1)

        self.p_lyr = nn.Conv1d(
            in_channels=embed_dim,
            out_channels=3 * embed_dim,
            kernel_size=3,
            padding=1,
            bias=b_flg,
        )

    def forward(self, x):
        batch, num_syms, embed_dim, num_subs = x.shape

        if self.norm_type == "ln":
            # 原始 model8：LayerNorm 作用于 permute 后的 4D。
            x = x.permute(0, 2, 1, 3)
            x = self.ln(x)
            x = x.permute(0, 2, 1, 3)

        x_f = torch.reshape(x, [batch * num_syms, embed_dim, num_subs])

        if self.norm_type == "bn":
            # BatchNorm1d 作用于 reshape 后的 3D [N, C, L]（conv 输入前）。
            x_f = self.bn(x_f)

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
    """model8 FFN（逐字同 teacher_model.SignalFeedForward1D + norm/act 可配）。

    norm 作用点同 ``SignalAttention1D``（ln: 4D pre-reshape；bn: 3D post-reshape）。
    act：``act_type="gelu"``（原始）/ ``"relu"``（user 轻量化路径 3）。
    """

    def __init__(self, embed_dim, num_symbols, num_subcarriers, b_flg=True, norm_type="ln", act_type="gelu"):
        super().__init__()
        if norm_type not in _VALID_NORM_TYPES:
            raise ValueError(
                f"norm_type={norm_type!r} 非法；须 ∈ {_VALID_NORM_TYPES}"
            )
        if act_type not in _VALID_ACT_TYPES:
            raise ValueError(
                f"act_type={act_type!r} 非法；须 ∈ {_VALID_ACT_TYPES}"
            )
        self.embed_dim = embed_dim
        self.norm_type = norm_type
        self.act_type = act_type

        if norm_type == "ln":
            self.ln = nn.LayerNorm(
                [num_symbols, embed_dim, num_subcarriers], elementwise_affine=False
            )
        else:  # "bn"
            self.bn = nn.BatchNorm1d(embed_dim)

        self.cv1 = nn.Conv1d(
            in_channels=embed_dim, out_channels=2 * embed_dim,
            kernel_size=3, padding=1, bias=b_flg,
        )
        self.act = nn.ReLU() if act_type == "relu" else nn.GELU()
        self.cv2 = nn.Conv1d(
            in_channels=2 * embed_dim, out_channels=embed_dim,
            kernel_size=3, padding=1, bias=b_flg,
        )

    def forward(self, x):
        batch, num_syms, embed_dim, num_subs = x.shape

        if self.norm_type == "ln":
            x = self.ln(x)

        x_f = torch.reshape(x, [batch * num_syms, embed_dim, num_subs])

        if self.norm_type == "bn":
            x_f = self.bn(x_f)

        x = self.cv1(x_f)
        x = self.act(x)
        x = self.cv2(x)
        return torch.reshape(x, [batch, num_syms, embed_dim, num_subs])


class SignalTransformerBlock(nn.Module):
    """model8 transformer block：attn → proj 残差 → FFN 残差（逐字同 teacher_model）。"""

    def __init__(self, embed_dim, num_symbols, num_subcarriers, m_type="t1",
                 norm_type="ln", act_type="gelu"):
        super().__init__()
        self.m_a = SignalAttention1D(
            embed_dim, num_symbols, num_subcarriers, m_type=m_type, norm_type=norm_type,
        )
        self.proj = nn.Conv1d(
            in_channels=embed_dim, out_channels=embed_dim,
            kernel_size=3, padding=1, bias=False,
        )
        self.m_c = SignalFeedForward1D(
            embed_dim, num_symbols, num_subcarriers,
            norm_type=norm_type, act_type=act_type,
        )

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
    """model8 主体（逐字同 teacher_model.SignalProcessingTransformer + norm/act 可配）。

    ``block_mtypes`` 显式给出每个 block 的 attention 类型（变体完全掌控 t1/t2 模式）。
    ``norm_type`` / ``act_type`` 透传到所有 block（同质——变体内全 block 同 norm/act）。
    """

    def __init__(self, block_mtypes, in_channels=4, embed_dim=16, num_symbols=64,
                 num_subcarriers=48, bias_flag=True, norm_type="ln", act_type="gelu"):
        super().__init__()
        if not block_mtypes:
            # fail loud：空 block_mtypes 会让 main 为空、feature_hook_names 返回不存在的
            # main.0。KNOBS.min=2 已在变体层挡住，直构时此校验兜底（Rule 12）。
            raise ValueError("block_mtypes 不可为空（至少 1 个 block）")
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.b_flg = bias_flag
        self.block_mtypes = list(block_mtypes)
        self.norm_type = norm_type
        self.act_type = act_type

        self.e_lyr = nn.Conv1d(
            in_channels=in_channels, out_channels=embed_dim,
            kernel_size=3, padding=1, bias=bias_flag,
        )
        self.main = nn.Sequential(*[
            SignalTransformerBlock(
                embed_dim, num_symbols, num_subcarriers, m_type=mt,
                norm_type=norm_type, act_type=act_type,
            )
            for mt in self.block_mtypes
        ])
        self.r_out = nn.Conv1d(
            in_channels=embed_dim, out_channels=in_channels,
            kernel_size=3, padding=1, bias=bias_flag,
        )

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


# ---------------------------------------------------------------------------
# Smoke（dummy input）：遍历 4 种 (norm_type, act_type) × t1/t2，校验前向 shape +
# feature_hook_names 恒 2 个 + hooks 落 distinct block。防止未来重构 norm/act 分支时
# 回归（参考 ``workflows/agents/_kd_scripts/teacher_model._smoke`` 模式）。
# ---------------------------------------------------------------------------
def _smoke() -> None:
    import torch

    B, num_ports, num_sub, num_syms = 1, 4, 48, 64
    x = torch.randn(B, num_ports, num_sub, num_syms, 1)
    combos = [("ln", "gelu"), ("ln", "relu"), ("bn", "gelu"), ("bn", "relu")]
    failures = 0
    for norm_type, act_type in combos:
        for m_pattern, mtypes in (("t1", ["t1"] * 3), ("alt", ["t1", "t2", "t1"])):
            try:
                net = SignalProcessingTransformer(
                    block_mtypes=mtypes, in_channels=num_ports, embed_dim=16,
                    num_symbols=num_syms, num_subcarriers=num_sub,
                    norm_type=norm_type, act_type=act_type,
                )
                net.train()  # BN 走 batch 统计（最严路径）
                with torch.no_grad():
                    y = net(x)
                assert y.shape == x.shape, f"shape {y.shape} != {x.shape}"
                hooks = net.feature_hook_names()
                assert len(hooks) == 2, f"hook len={len(hooks)}, want 2"
                assert hooks[0] != hooks[1], f"hooks not distinct: {hooks}"
                named = dict(net.named_modules())
                for h in hooks:
                    assert h in named, f"hook {h!r} missing"
                print(f"OK norm={norm_type} act={act_type} m={m_pattern}: "
                      f"out={tuple(y.shape)} hooks={hooks}")
            except Exception as e:  # noqa: BLE001 — smoke 聚合错误
                failures += 1
                print(f"FAIL norm={norm_type} act={act_type} m={m_pattern}: "
                      f"{type(e).__name__}: {e}")
    if failures:
        raise SystemExit(f"_smoke: {failures} combo(s) failed")


if __name__ == "__main__":
    _smoke()
