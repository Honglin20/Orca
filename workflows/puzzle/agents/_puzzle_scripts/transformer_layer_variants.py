"""transformer_layer_variants.py —— 去 elastic 原生 transformer layer 变体库。

layer variant catalog（``layer_variant_catalog.yaml``）的 builtin 源。每个
``make_<name>_layer(slot, **params) -> nn.Module`` 返回**完整 transformer encoder
layer**（attention 变体 + 标准 FFN + 2×LayerNorm + 2×residual）。

去 elastic 原生化（design draft §4.1）
    取 nas-agent ``Elastic<Block>.get_active_subnet()`` 的原生结构（如
    ``nas-agent/nas_agent/blocks/random_synthesizer.py`` 的 ``RandomSynthesizerBlock``），
    **所有维度从 ``slot`` 注入**（slot 值 = pz_search_space 从原层 + 输入 trace 提取），
    不经 elastic（``set_sample_config`` / ``get_active_subnet`` / ``elastic_num_params``
    一律不进运行时）。**变体库零维度硬编码**（design draft L11）——"去 elastic" = 去运行时
    可变，非写死常量；变体对任意 transformer 项目通用。

统一标准 FFN（design draft L4）
    所有非 identity 变体的 FFN = ``Linear(in_dim, original_intermediate) → act →
    Linear(original_intermediate, out_dim)``，``act = resolve_activation(slot.activation)``，
    **不照搬 nas-agent 原版**（fnet 用 GELU、softs_star 无独立 FFN）。attention 部分保留
    各变体特色，FFN 维度/激活一律从 slot 取。寻优维度仅 attention 机制，不寻优 ffn/depth/width。

结构
    Pre-LayerNorm：``x = x + attn(norm1(x), attn_mask); x = x + ffn(norm2(x))``。
    变体自带 LayerNorm（不强制照搬原层 norm 类型，design draft R1）。

forward 契约
    ``forward(x, src_mask=None, *args, **kwargs) -> tensor``，自包含异构父层签名适配
    （父层可能 ``layer(x, src_mask=...)`` / ``layer(x, attention_mask=...)`` / ``layer(x)``）。
    按 ``_MASK_KEYS`` 顺序抽 mask-like kwarg 转交 attention；无 mask 时退化为纯 attention。
    故 catalog factory **不经外层 _wrap**（layer 自包含签名处理，区别于单块候选）。

依赖铁律
    本模块**不在运行时 import puzzle_common**（避免循环：puzzle_common 的 load_catalog
    lazy import 本模块）。``resolve_activation`` 从 ``puzzle_blocks`` import（叶子模块，
    无循环）。``slot`` duck-typed，需暴露 ``in_dim``/``out_dim``/``num_heads``/``head_dim``/
    ``original_intermediate``/``activation``/``max_seq_len`` 等属性。

维度对齐铁律：外部 ``in_dim``/``out_dim`` 固定不可搜索。
"""

from __future__ import annotations

from typing import Any, Callable

import torch
import torch.nn as nn
import torch.nn.functional as F

from puzzle_blocks import resolve_activation  # 叶子模块，无循环

if False:  # TYPE_CHECKING 等价（避免运行时循环）
    from puzzle_common import Slot


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


# ── 标准 FFN（design draft L4：统一从 slot 取，不照搬 nas-agent 原版）─────────


class _StandardFFN(nn.Module):
    """``Linear(in→intermediate) → act → Linear(intermediate→out)``。

    intermediate = ``slot.original_intermediate``（原层 FFN 中间维，非 in_dim×ratio）；
    act = ``resolve_activation(slot.activation)``（原层激活）。FFN 不进搜索空间。
    """

    def __init__(self, slot: "Slot") -> None:
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


# ── attention 变体 core（去 elastic 原生，forward(x, attn_mask=None)）─────────
# 取自 nas-agent 各 ElasticCore 的 forward 计算结构，固定维度从 slot 取，去 sample_config。


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
    """学习型 token 混合矩阵（无 QK）。源自 nas-agent RandomSynthesizerCore。

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
                f"（mixing matrix 预分配上限；pz_baseline 应 trace 原层真实序列长度回填 slot）"
            )
        attn = self.attention[:, :length, :length]
        value = self.value_proj(x)
        out = torch.matmul(attn, value)
        return self.out_proj(out)


class _ReluAttention(nn.Module):
    """ReLU(logits)/L 归一 attention（替 softmax）。源自 nas-agent ReluAttentionCore。"""

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
    """零参 2D-DFT mixer（实部）。源自 nas-agent FNetFourierTransform。

    DFT basis 运行时按 (length, hidden) 构造并缓存（non-persistent buffer）——
    无可训练参数，BLD 只算 loss 不优化。
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
    """SOFTS STAR 聚合-重分配 mixer。源自 nas-agent SOFTSSTARMixer。

    gen1: in→in, gen2: in→core_dim, gen3: in+core_dim→in, gen4: in→in。
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

    def __init__(self, slot: "Slot", attention: nn.Module) -> None:
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
# catalog loader 用 functools.partial 绑定 **非维度类算法超参**（如 softs_star 的 core_dim）
# 成统一 factory(slot)。所有维度从 slot 取，零硬编码（L11）。不经外层 _wrap（layer 自包含签名）。


def make_vanilla_layer(slot: "Slot") -> nn.Module:
    """标准 MHSA layer（对照基线）。"""
    attn = _VanillaAttention(in_dim=slot.in_dim, num_heads=max(slot.num_heads, 1))
    return _PreLNTransformerLayer(slot, attn)


def make_random_synthesizer_layer(slot: "Slot") -> nn.Module:
    """学习型 token 混合矩阵 layer。

    max_seq_len **必须**从 slot 取（pz_baseline trace 原层输入序列长度回填），禁 fallback——
    预分配 mixing matrix ``[1, max_seq_len, max_seq_len]``，fallback 512 对短序列（如 target
    seq=16）会过参化 2.6M 参数（应 256），BLD 优化万倍过参矩阵（spec-reviewer LV-7）。
    """
    max_seq_len = getattr(slot, "max_seq_len", None)
    if not max_seq_len or int(max_seq_len) <= 0:
        raise ValueError(
            f"random_synthesizer_layer: slot {getattr(slot, 'parent_module_path', '?')} "
            f"缺 max_seq_len（须 pz_baseline trace 原层输入序列长度回填，禁 fallback）"
        )
    attn = _RandomSynthesizerAttention(
        in_dim=slot.in_dim,
        num_heads=max(slot.num_heads, 1),
        head_dim=max(slot.head_dim, 1),
        max_seq_len=int(max_seq_len),
    )
    return _PreLNTransformerLayer(slot, attn)


def make_relu_attention_layer(slot: "Slot") -> nn.Module:
    """ReLU(logits)/L 归一 attention layer。"""
    attn = _ReluAttention(
        in_dim=slot.in_dim,
        num_heads=max(slot.num_heads, 1),
        head_dim=max(slot.head_dim, 1),
    )
    return _PreLNTransformerLayer(slot, attn)


def make_fnet_layer(slot: "Slot") -> nn.Module:
    """零参 2D-DFT mixer layer。"""
    return _PreLNTransformerLayer(slot, _FNetMixer())


def make_softs_star_layer(slot: "Slot", core_dim: int = 64) -> nn.Module:
    """SOFTS STAR 聚合-重分配 layer。core_dim 为算法超参（非项目维度，可 catalog 覆盖）。"""
    attn = _SoftsStarMixer(in_dim=slot.in_dim, core_dim=max(1, int(core_dim)))
    return _PreLNTransformerLayer(slot, attn)


# no_op layer：整层退化为纯残差直通（forward 返回输入 x，跳过 attn/ffn/norm，latency≈0）。
# layer 粒度语义区别于 block 粒度 _ZeroBlock：block 在 residual 内，零输出使 x+0=x；
# layer 的 residual 在层内，整层返回零会破坏后续层输入 → layer no_op = passthrough。
class _NoOpLayer(nn.Module):
    """no_op layer：整层纯残差直通（forward 返回输入 x）。层被旁路、跳过全部计算。

    layer 粒度下 no_op = passthrough（**非零输出**）：整层 residual 在层内
    （``x = x + attn(...)``），返回零会让该层输出恒零、破坏后续层输入；返回 ``x`` 则层被
    旁路（latency≈0，不履行层职能）。MIP best-effort 排除 no_op（保逻辑铁律：no_op 不履行
    层职能，仅作 floor 锚 / best-effort 兜底）。
    """

    def __init__(self, slot: "Slot") -> None:
        super().__init__()
        if slot.in_dim != slot.out_dim:
            raise ValueError(
                f"no_op layer 要求 in_dim==out_dim（slot "
                f"{getattr(slot, 'parent_module_path', '?')}: {slot.in_dim}!={slot.out_dim}）"
            )

    def forward(self, x: torch.Tensor, *args: Any, **kwargs: Any) -> torch.Tensor:
        return x


def make_no_op_layer(slot: "Slot") -> nn.Module:
    """no_op layer：整层纯残差直通（passthrough，非零输出；保逻辑下供 MIP floor / best-effort 排除）。"""
    return _NoOpLayer(slot)


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
        ("no_op", make_no_op_layer),
    ]:
        layer = fn(mock_slot)
        y = layer(x)
        assert y.shape == (B, L, D), f"{name}: shape {y.shape} != {(B, L, D)}"
        # kwargs mask 不崩（mask-blind 变体忽略，mask-aware 透传）
        y2 = layer(x, src_mask=None)
        assert y2.shape == (B, L, D)
        print(f"[Pass] {name}_layer forward shape OK ({y.shape})")
    print(">>> transformer_layer_variants smoke tests passed!")
