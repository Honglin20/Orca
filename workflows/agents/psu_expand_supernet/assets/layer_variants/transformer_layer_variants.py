"""transformer_layer_variants.py —— transformer layer 变体库（自包含快照）。

Provenance
    自包含快照（2026-08-17 定版，本文件即唯一事实源）。相对早期版本的两处删改：
      * 内联 ``resolve_activation``（源文件跨文件 import puzzle_blocks，快照
        须零跨文件依赖——自包含是快照的存在理由）；
      * 删 ``_NoOpLayer`` / ``make_no_op_layer``（整层 passthrough 不履行层职能，
        不在 PSU 分支集内）。
    源后续演进不回灌本快照：本文件是 PSU 变体分支的唯一事实源。

概述
    每个工厂 ``make_<name>_layer(slot, **params) -> nn.Module`` 返回**完整
    transformer encoder layer**（attention 变体 + 标准 FFN + 2×LayerNorm +
    2×residual）。``slot`` duck-typed，需暴露 ``in_dim`` / ``out_dim`` /
    ``num_heads`` / ``head_dim`` / ``original_intermediate`` / ``activation`` /
    ``max_seq_len`` 属性（值 = 从原层 + 输入 trace 提取的实测事实）。

    **维度零硬编码**：所有维度从 ``slot`` 注入，变体对任意 transformer 项目
    通用。维度对齐铁律：外部 ``in_dim`` / ``out_dim`` 固定不可搜索。

统一标准 FFN
    所有变体的 FFN = ``Linear(in_dim, original_intermediate) → act →
    Linear(original_intermediate, out_dim)``，``act =
    resolve_activation(slot.activation)``（原层激活）。寻优维度仅 attention
    机制，不寻优 FFN 维度 / 激活。

结构
    Pre-LayerNorm：``x = x + attn(norm1(x), attn_mask); x = x + ffn(norm2(x))``。
    变体自带 LayerNorm（不强制照搬原层 norm 类型）。

forward 契约
    ``forward(x, src_mask=None, *args, **kwargs) -> tensor``，自包含异构父层
    签名适配（父层可能 ``layer(x, src_mask=...)`` / ``layer(x,
    attention_mask=...)`` / ``layer(x)``）。按 ``_MASK_KEYS`` 顺序抽 mask-like
    kwarg 转交 attention；无 mask 时退化为纯 attention。mask-blind 变体
    （fnet / softs_star / random_synthesizer）接受但忽略 mask。
"""

from __future__ import annotations

from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F


# ── 激活解析（内联最小子集，快照零跨文件依赖）──────────────────────────────

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
    """激活名 → nn.Module 类。未知激活 fail loud。"""
    cls = _ACTIVATION_MAP.get(name)
    if cls is None:
        raise ValueError(f"未知 activation {name!r}（支持：{sorted(_ACTIVATION_MAP)}）")
    return cls


# 父层 forward 传 mask 时常见的 kwarg 名（按优先级匹配首个非 None）。
_MASK_KEYS: tuple[str, ...] = (
    "attn_mask",
    "src_mask",
    "attention_mask",
    "mask",
    "key_padding_mask",
)


def _extract_mask(src_mask: Any, kwargs: dict[str, Any]) -> Any:
    """从 positional src_mask 或 kwargs 抽 mask-like 张量（无则 None）。"""
    if src_mask is not None:
        return src_mask
    for k in _MASK_KEYS:
        v = kwargs.get(k)
        if v is not None:
            return v
    return None


# ── 标准 FFN（维度/激活一律从 slot 取）──────────────────────────────────────


class _StandardFFN(nn.Module):
    """``Linear(in→intermediate) → act → Linear(intermediate→out)``。

    intermediate = ``slot.original_intermediate``（原层 FFN 中间维）；
    act = ``resolve_activation(slot.activation)``（原层激活）。FFN 不进搜索空间。
    """

    def __init__(self, slot: Any) -> None:
        super().__init__()
        if slot.original_intermediate is None:
            raise ValueError(
                f"layer slot {getattr(slot, 'parent_module_path', '?')} 缺 "
                f"original_intermediate（标准 FFN 的中间维基准，须从原层提取）"
            )
        if slot.activation is None:
            raise ValueError(
                f"layer slot {getattr(slot, 'parent_module_path', '?')} 缺 "
                f"activation（标准 FFN 的激活，须从原层提取）"
            )
        intermediate = max(1, int(slot.original_intermediate))
        act_cls = resolve_activation(slot.activation)
        self.fc1 = nn.Linear(slot.in_dim, intermediate)
        self.act = act_cls()
        self.fc2 = nn.Linear(intermediate, slot.out_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.fc2(self.act(self.fc1(x)))


# ── attention 变体 core（forward(x, attn_mask=None)）────────────────────────


class _VanillaAttention(nn.Module):
    """标准 MHSA（``nn.MultiheadAttention``），对照基线。attn_mask 透传。"""

    def __init__(self, in_dim: int, num_heads: int) -> None:
        super().__init__()
        if in_dim % num_heads != 0:
            raise ValueError(
                f"vanilla attention: in_dim={in_dim} 必须被 num_heads={num_heads} 整除"
            )
        self.attn = nn.MultiheadAttention(
            embed_dim=in_dim, num_heads=num_heads, batch_first=True
        )

    def forward(self, x: torch.Tensor, attn_mask: Any = None) -> torch.Tensor:
        out, _ = self.attn(x, x, x, attn_mask=attn_mask, need_weights=False)
        return out


class _RandomSynthesizerAttention(nn.Module):
    """学习型 token 混合矩阵（无 QK）。

    value_proj: ``in_dim → attn_dim``（attn_dim = num_heads × head_dim）；
    mixing matrix ``[1, max_seq_len, max_seq_len]`` 运行时 slice 到 ``[:L, :L]``；
    out_proj: ``attn_dim → in_dim``。
    """

    def __init__(self, in_dim: int, num_heads: int, head_dim: int, max_seq_len: int) -> None:
        super().__init__()
        attn_dim = max(num_heads, 1) * max(head_dim, 1)
        self.value_proj = nn.Linear(in_dim, attn_dim)
        self.out_proj = nn.Linear(attn_dim, in_dim)
        self.max_seq_len = max(1, int(max_seq_len))
        self.attention = nn.Parameter(torch.empty(1, self.max_seq_len, self.max_seq_len))
        nn.init.xavier_uniform_(self.attention)

    def forward(self, x: torch.Tensor, attn_mask: Any = None) -> torch.Tensor:
        length = x.size(1)
        if length > self.max_seq_len:
            raise ValueError(
                f"random_synthesizer: 序列长度 {length} 超过 max_seq_len {self.max_seq_len}"
                f"（mixing matrix 预分配上限；max_seq_len 须 trace 原层输入序列长度回填 slot，禁 fallback）"
            )
        attn = self.attention[:, :length, :length]
        value = self.value_proj(x)
        out = torch.matmul(attn, value)
        return self.out_proj(out)


class _ReluAttention(nn.Module):
    """ReLU(logits)/L 归一 attention（替 softmax）。"""

    def __init__(self, in_dim: int, num_heads: int, head_dim: int) -> None:
        super().__init__()
        self.num_heads = max(num_heads, 1)
        self.head_dim = max(head_dim, 1)
        attn_dim = self.num_heads * self.head_dim
        self.qkv_proj = nn.Linear(in_dim, attn_dim * 3)
        self.out_proj = nn.Linear(attn_dim, in_dim)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x: torch.Tensor, attn_mask: Any = None) -> torch.Tensor:
        batch, length, _ = x.shape
        qkv = self.qkv_proj(x)
        q, k, v = qkv.reshape(batch, length, 3 * self.num_heads, self.head_dim).split(
            self.num_heads, dim=2
        )
        # q/k: [batch, length, num_heads, head_dim] → [batch, num_heads, length, head_dim]
        q = q.permute(0, 2, 1, 3)
        k = k.permute(0, 2, 1, 3)
        v = v.permute(0, 2, 1, 3)
        attn = self.relu((q @ k.transpose(-2, -1)) * (self.head_dim ** -0.5))
        attn = attn / max(attn.size(-1), 1)
        out = (attn @ v).permute(0, 2, 1, 3).contiguous().view(batch, length, -1)
        return self.out_proj(out)


class _FNetMixer(nn.Module):
    """零参 2D-DFT mixer（实部）。

    DFT basis 运行时按 (length, hidden) 构造并缓存（non-persistent buffer）——
    无可训练参数。
    """

    def __init__(self) -> None:
        super().__init__()
        self._cached_length: int = 0
        self._cached_hidden: int = 0
        self.register_buffer("_seq_cos", torch.empty(0), persistent=False)
        self.register_buffer("_seq_sin", torch.empty(0), persistent=False)
        self.register_buffer("_hidden_cos", torch.empty(0), persistent=False)
        self.register_buffer("_hidden_sin", torch.empty(0), persistent=False)

    def _build_dft_basis(self, size: int, device: torch.device) -> tuple[torch.Tensor, torch.Tensor]:
        idx = torch.arange(size, device=device, dtype=torch.float32)
        angle = 2.0 * torch.pi * idx[:, None] * idx[None, :] / float(size)
        return torch.cos(angle), torch.sin(angle)

    def _ensure_basis(self, length: int, hidden: int, device: torch.device) -> None:
        if (
            length == self._cached_length
            and hidden == self._cached_hidden
            and self._seq_cos.device == device
        ):
            return
        self._seq_cos, self._seq_sin = self._build_dft_basis(length, device)
        self._hidden_cos, self._hidden_sin = self._build_dft_basis(hidden, device)
        self._cached_length = length
        self._cached_hidden = hidden

    def forward(self, x: torch.Tensor, attn_mask: Any = None) -> torch.Tensor:
        length, hidden = x.size(1), x.size(-1)
        self._ensure_basis(length, hidden, x.device)
        dtype = x.dtype
        xf = x.float()
        real = torch.matmul(torch.matmul(self._seq_cos, xf), self._hidden_cos)
        real = real - torch.matmul(torch.matmul(self._seq_sin, xf), self._hidden_sin)
        return real.to(dtype)


class _SoftsStarMixer(nn.Module):
    """SOFTS STAR 聚合-重分配 mixer。

    gen1: in→in, gen2: in→core_dim, gen3: in+core_dim→in, gen4: in→in.
    softmax 跨 token 聚合 + 重分配。core_dim 为算法超参（非项目维度）。
    """

    def __init__(self, in_dim: int, core_dim: int) -> None:
        super().__init__()
        self.gen1 = nn.Linear(in_dim, in_dim)
        self.gen2 = nn.Linear(in_dim, core_dim)
        self.gen3 = nn.Linear(in_dim + core_dim, in_dim)
        self.gen4 = nn.Linear(in_dim, in_dim)

    def forward(self, x: torch.Tensor, attn_mask: Any = None) -> torch.Tensor:
        core = F.gelu(self.gen1(x))
        core = self.gen2(core)
        weight = F.softmax(core, dim=1)
        core = torch.sum(core * weight, dim=1, keepdim=True).expand(-1, x.size(1), -1)
        fused = torch.cat([x, core], dim=-1)
        fused = F.gelu(self.gen3(fused))
        return self.gen4(fused)


# ── Pre-LN transformer layer 骨架（attn 变体 + 标准 FFN + 2×norm + 2×residual）──


class _PreLNTransformerLayer(nn.Module):
    """Pre-LayerNorm transformer encoder layer。

    ``x = x + attn(norm1(x), attn_mask); x = x + ffn(norm2(x))``。
    forward 自包含异构父层签名适配（positional src_mask 或 mask-like kwarg）。
    """

    def __init__(self, slot: Any, attention: nn.Module) -> None:
        super().__init__()
        self.in_dim = int(slot.in_dim)
        self.norm1 = nn.LayerNorm(self.in_dim)
        self.norm2 = nn.LayerNorm(self.in_dim)
        self.attn = attention
        self.ffn = _StandardFFN(slot)

    def forward(self, x: torch.Tensor, src_mask: Any = None, *args: Any, **kwargs: Any) -> torch.Tensor:
        mask = _extract_mask(src_mask, kwargs)
        x = x + self.attn(self.norm1(x), attn_mask=mask)
        x = x + self.ffn(self.norm2(x))
        return x


# ── builtin factory（签名统一：factory(slot, **params) -> nn.Module）─────────
# 所有维度从 slot 取，零硬编码。


def make_vanilla_layer(slot: Any) -> nn.Module:
    """标准 MHSA layer（对照基线）。"""
    attn = _VanillaAttention(in_dim=slot.in_dim, num_heads=max(slot.num_heads, 1))
    return _PreLNTransformerLayer(slot, attn)


def make_random_synthesizer_layer(slot: Any) -> nn.Module:
    """学习型 token 混合矩阵 layer。

    max_seq_len **必须**从 slot 取（trace 原层输入序列长度回填），禁 fallback——
    预分配 mixing matrix ``[1, max_seq_len, max_seq_len]``，fallback 大值对短序列
    会过参化数百万参数（应 = 序列长度平方量级）。
    """
    max_seq_len = getattr(slot, "max_seq_len", None)
    if not max_seq_len or int(max_seq_len) <= 0:
        raise ValueError(
            f"random_synthesizer_layer: slot {getattr(slot, 'parent_module_path', '?')} "
            f"缺 max_seq_len（须 trace 原层输入序列长度回填，禁 fallback）"
        )
    attn = _RandomSynthesizerAttention(
        in_dim=slot.in_dim,
        num_heads=max(slot.num_heads, 1),
        head_dim=max(slot.head_dim, 1),
        max_seq_len=int(max_seq_len),
    )
    return _PreLNTransformerLayer(slot, attn)


def make_relu_attention_layer(slot: Any) -> nn.Module:
    """ReLU(logits)/L 归一 attention layer。"""
    attn = _ReluAttention(
        in_dim=slot.in_dim,
        num_heads=max(slot.num_heads, 1),
        head_dim=max(slot.head_dim, 1),
    )
    return _PreLNTransformerLayer(slot, attn)


def make_fnet_layer(slot: Any) -> nn.Module:
    """零参 2D-DFT mixer layer。"""
    return _PreLNTransformerLayer(slot, _FNetMixer())


def make_softs_star_layer(slot: Any, core_dim: int = 64) -> nn.Module:
    """SOFTS STAR 聚合-重分配 layer。core_dim 为算法超参（非项目维度）。"""
    attn = _SoftsStarMixer(in_dim=slot.in_dim, core_dim=max(1, int(core_dim)))
    return _PreLNTransformerLayer(slot, attn)


if __name__ == "__main__":
    # 烟雾测试：每个变体能构造 + forward 产出正确 shape（维度从 mock slot 取）。
    from types import SimpleNamespace

    mock_slot = SimpleNamespace(
        in_dim=128, out_dim=128, num_heads=4, head_dim=32,
        original_intermediate=256, activation="relu",
        max_seq_len=64, parent_module_path="mock",
    )
    B, L, D = 2, 16, 128
    x = torch.randn(B, L, D)
    for name, fn in [
        ("vanilla", make_vanilla_layer),
        ("random_synthesizer", make_random_synthesizer_layer),
        ("relu_attention", make_relu_attention_layer),
        ("fnet", make_fnet_layer),
        ("softs_star", make_softs_star_layer),
    ]:
        layer = fn(mock_slot)
        y = layer(x)
        assert y.shape == (B, L, D), f"{name}: shape {y.shape} != {(B, L, D)}"
        # kwargs mask 不崩（mask-blind 变体忽略，mask-aware 透传）
        y2 = layer(x, src_mask=None)
        assert y2.shape == (B, L, D)
        print(f"[Pass] {name}_layer forward shape OK ({y.shape})")
    print(">>> transformer_layer_variants smoke tests passed!")
