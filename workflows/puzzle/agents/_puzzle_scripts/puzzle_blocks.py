"""puzzle_blocks.py —— Puzzle 候选块实现库（candidate catalog 的 builtin 源）。

每条 builtin 候选对应一个 ``make_<name>(slot, **params) -> nn.Module`` 工厂。
``candidate_catalog.yaml`` 用 ``puzzle_blocks::make_<name>`` 引用本模块的工厂；
``puzzle_common.load_catalog`` 解析时用 ``functools.partial`` 绑定 params，
再统一用 ``_wrap`` 包成 ``_KwargPassthrough``（适配异构父层 forward 签名）。

依赖铁律：本模块**不**在运行时 import puzzle_common（避免循环：puzzle_common
的 ``load_catalog`` 函数内才 import 本模块）。``slot`` 参数 duck-typed，只需
暴露 ``in_dim``/``out_dim``/``num_heads``/``head_dim``/``activation``/
``original_intermediate`` 等属性。

forward 契约：单参 ``x``（``[B, L, D]``），输出末维 = ``slot.out_dim``。
维度对齐铁律：外部 ``in_dim``/``out_dim`` 固定不可搜索。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

import torch
import torch.nn as nn

if TYPE_CHECKING:  # 避免运行时循环 import
    from puzzle_common import Slot


# ── 激活解析（E23：ffn slot activation required）──────────────────────────────

_ACTIVATION_MAP: dict[str, type[nn.Module]] = {
    "gelu": nn.GELU,
    "relu": nn.ReLU,
    "silu": nn.SiLU,
    "mish": nn.Mish,
    "tanh": nn.Tanh,
    "sigmoid": nn.Sigmoid,
    "elu": nn.ELU,
    "leaky_relu": nn.LeakyReLU,
}


def resolve_activation(name: str) -> type[nn.Module]:
    """激活名 → nn.Module 类。未知激活 fail loud（E23 配套）。"""
    cls = _ACTIVATION_MAP.get(name)
    if cls is None:
        raise ValueError(
            f"未知 activation {name!r}（支持：{sorted(_ACTIVATION_MAP)}）"
        )
    return cls


# 公开反向映射：激活类 → 名称（puzzle activation 命名的 DRY 单一真相源；
# test_puzzle_catalog 验证其与 _ACTIVATION_MAP 对偶）。
ACTIVATION_CLASS_TO_NAME: dict[type, str] = {cls: name for name, cls in _ACTIVATION_MAP.items()}


# ── 适配器 / 辅助模块 ─────────────────────────────────────────────────────────


class _VanillaMHSA(nn.Module):
    """vanilla MHSA 包装：``nn.MultiheadAttention`` forward 三参 → 单参。"""

    def __init__(self, embed_dim: int, num_heads: int):
        super().__init__()
        if embed_dim % num_heads != 0:
            raise ValueError(
                f"vanilla MHSA: embed_dim={embed_dim} 必须能被 num_heads={num_heads} 整除"
            )
        self.attn = nn.MultiheadAttention(
            embed_dim=embed_dim, num_heads=num_heads, batch_first=True
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out, _ = self.attn(x, x, x, need_weights=False)
        return out


class _MaskedMHSA(nn.Module):
    """mask-aware vanilla MHSA（U6 §3 root cause F）：接受 attn_mask 的 MHSA 变体。

    causal / padding 兼容：父层 forward 传 ``attn_mask`` / ``attention_mask`` /
    ``key_padding_mask`` / ``mask`` 等常见 mask kwarg 时，本模块把它转交
    ``nn.MultiheadAttention`` 的 ``attn_mask`` 入参（标准 MHSA 的 mask 语义）。
    agent 移植时若用户的 mask 语义非标准（如 custom boolean 取反），应在 adapter 内
    适配——本 builtin 仅提供「能接 mask kwarg 的 vanilla MHSA」这一通用候选。
    """

    def __init__(self, embed_dim: int, num_heads: int):
        super().__init__()
        if embed_dim % num_heads != 0:
            raise ValueError(
                f"masked MHSA: embed_dim={embed_dim} 必须能被 num_heads={num_heads} 整除"
            )
        self.attn = nn.MultiheadAttention(
            embed_dim=embed_dim, num_heads=num_heads, batch_first=True
        )

    def forward(self, x: torch.Tensor, attn_mask: Any = None) -> torch.Tensor:
        out, _ = self.attn(x, x, x, attn_mask=attn_mask, need_weights=False)
        return out


class _MaskPassthrough(nn.Module):
    """mask-aware variant 签名适配器：从父层 kwargs 抽常见 mask key 转给 inner。

    与 ``_KwargPassthrough``（剥 kwargs）对偶：本包装识别父层 forward 传的 mask-like
    kwarg（attn_mask / attention_mask / mask / key_padding_mask），把首匹配项作为
    ``attn_mask=`` 传给 inner 的 forward。无 mask kwarg 时按 ``inner(x)`` 调用
    （兼容 mask-bearing slot 在无 mask 输入时的退化路径）。
    """

    _MASK_KEYS: tuple[str, ...] = (
        "attn_mask", "attention_mask", "mask", "key_padding_mask",
    )

    def __init__(self, inner: nn.Module):
        super().__init__()
        self.inner = inner

    def forward(self, x: torch.Tensor, *args: Any, **kwargs: Any) -> torch.Tensor:
        mask: Any = None
        for k in self._MASK_KEYS:
            if k in kwargs and kwargs[k] is not None:
                mask = kwargs[k]
                break
        if mask is not None:
            return self.inner(x, attn_mask=mask)
        return self.inner(x)


def _wrap(
    inner_factory: Callable[["Slot"], nn.Module]
) -> Callable[["Slot"], nn.Module]:
    """把 slot→module 工厂包成 slot→_KwargPassthrough(module)（统一适配异构签名）。"""

    def _w(slot: "Slot") -> nn.Module:
        return _KwargPassthrough(inner_factory(slot))

    return _w


def _wrap_mask(
    inner_factory: Callable[["Slot"], nn.Module]
) -> Callable[["Slot"], nn.Module]:
    """mask-aware wrapper：包成 _MaskPassthrough（保留 attn_mask kwarg）。"""

    def _w(slot: "Slot") -> nn.Module:
        return _MaskPassthrough(inner_factory(slot))

    return _w


class _ZeroBlock(nn.Module):
    """零输出 no_op:forward 返回 zeros_like(input)——真·删块(residual 不变,latency≈0)。

    比 nn.Identity 更正确:Identity 让 residual 变 ``x + norm(x)``(加噪),零输出使
    ``x + 0 = x``(残差不变,块被真正旁路)。对 attention/ffn 均适用(in_dim==out_dim)。
    接受 **kwargs:父层可能传 attention_mask/norm_factor 等(异构 forward 签名),忽略。
    """

    def forward(self, x: torch.Tensor, *args: Any, **kwargs: Any) -> torch.Tensor:
        return torch.zeros_like(x)


class _KwargPassthrough(nn.Module):
    """variant 签名适配器:把 inner 包成 ``forward(x, *args, **kwargs) -> inner(x)``。

    父层(异构 transformer)调 ``self_attn(src, attention_mask=...)`` 带 kwargs;
    builtin 候选的 forward(x) 不收 → TypeError。本包装忽略额外参,只把首参传给
    inner。state_dict 加 ``inner.`` 前缀,在存(BLD)/载(score/latency/build)两侧
    一致(都经 catalog factory),无需改 load 逻辑。
    """

    def __init__(self, inner: nn.Module):
        super().__init__()
        self.inner = inner

    def forward(self, x: torch.Tensor, *args: Any, **kwargs: Any) -> torch.Tensor:
        return self.inner(x)


# ── builtin factory（签名统一：factory(slot, **params) -> nn.Module）──────────
# catalog loader 用 functools.partial 绑定 params 成 factory(slot)。


def make_random_synthesizer(slot: "Slot") -> nn.Module:
    from nas_agent.blocks.random_synthesizer import ElasticRandomSynthesizerCore

    return ElasticRandomSynthesizerCore(
        super_num_heads=max(slot.num_heads, 1),
        global_dim=slot.in_dim,
        head_dim=max(slot.head_dim, 1),
        max_seq_len=512,
    )


def make_relu_attention(slot: "Slot") -> nn.Module:
    from nas_agent.blocks.relu_attention import ElasticReluAttentionCore

    return ElasticReluAttentionCore(
        super_num_heads=max(slot.num_heads, 1),
        global_dim=slot.in_dim,
        head_dim=max(slot.head_dim, 1),
    )


def make_fnet(slot: "Slot") -> nn.Module:
    from nas_agent.blocks.fnet_fourier_mixer import ElasticFNetFourierTransform

    return ElasticFNetFourierTransform()


def make_softs_star(slot: "Slot") -> nn.Module:
    from nas_agent.blocks.softs_star_mixer import ElasticSOFTSSTARMixer

    return ElasticSOFTSSTARMixer(
        super_core_dim=slot.in_dim,
        global_dim=slot.in_dim,
    )


def make_vanilla(slot: "Slot") -> nn.Module:
    return _VanillaMHSA(embed_dim=slot.in_dim, num_heads=max(slot.num_heads, 1))


def make_masked_vanilla(slot: "Slot") -> nn.Module:
    """mask-aware vanilla MHSA（U6 §3 root cause F）。

    与 ``make_vanilla`` 同构（标准 MHSA），但经 ``_wrap_mask`` 包成
    ``_MaskPassthrough`` —— 父层 forward 传 ``attn_mask`` / ``attention_mask`` /
    ``key_padding_mask`` / ``mask`` 等常见 mask kwarg 时转交 ``nn.MultiheadAttention``。
    用于 mask_load_bearing slot（causal/padding 场景），避免 mask-bearing slot
    在 catalog 全 mask-blind 时塌缩成 identity。
    """
    return _MaskedMHSA(embed_dim=slot.in_dim, num_heads=max(slot.num_heads, 1))


def make_ffn(slot: "Slot", ratio: float) -> nn.Module:
    """FFN 剪枝候选：Linear-Act-Linear。

    - E7：intermediate = original_intermediate × ratio（相对原中间维，非 in_dim）。
    - E23：activation required，slot.activation 为 None → raise。
    """
    if slot.activation is None:
        raise ValueError(
            f"ffn slot {slot.parent_module_path} 缺 activation（activation required）"
        )
    if slot.original_intermediate is None:
        raise ValueError(
            f"ffn slot {slot.parent_module_path} 缺 original_intermediate（ratio 基准，须非 None）"
        )
    act_cls = resolve_activation(slot.activation)
    intermediate = max(1, int(round(slot.original_intermediate * ratio)))
    return nn.Sequential(
        nn.Linear(slot.in_dim, intermediate),
        act_cls(),
        nn.Linear(intermediate, slot.out_dim),
    )


def make_linear(slot: "Slot") -> nn.Module:
    """单 Linear 替代（激进 FFN 剪枝）。仅适用 standard FFN（U3 is_valid 把关）。"""
    return nn.Linear(slot.in_dim, slot.out_dim)


def make_zero(slot: "Slot") -> nn.Module:
    """no_op：零输出块（in_dim==out_dim，residual 不变）。"""
    if slot.in_dim != slot.out_dim:
        raise ValueError(
            f"no_op 候选要求 in_dim==out_dim（slot {slot.parent_module_path}: "
            f"{slot.in_dim}!={slot.out_dim}）"
        )
    return _ZeroBlock()
