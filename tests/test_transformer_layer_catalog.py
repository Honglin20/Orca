"""test_transformer_layer_catalog.py —— Phase L1 layer-variant catalog 集成测试。

锁定 transformer_layer kind 候选从 catalog 加载 → factory 构造 → forward shape 的端到端
intent（design draft §2.2 / §4 / L11-L14），覆盖 happy path + fail-loud 分支。

重点（Rule 9：验证 intent 非 behavior）：
  - **catalog 加载层变体 + 不 wrap**（§4.4）：entry.factory(slot) 直接返回 _PreLNTransformerLayer
    实例（**非** _KwargPassthrough/_MaskPassthrough 包装），证明 layer 变体自包含签名适配。
  - **L11 维度从 slot 注入**（BLOCKER）：layer forward 维度 == slot 维度（in_dim→out_dim，
    序列不变），变体库零硬编码维度。
  - **L11 max_seq_len fail-loud**（BLOCKER）：random_synthesizer_layer 缺 max_seq_len → raise
    （禁 fallback 512 对短序列过参化，design draft §4.2 LV-7）。
  - **L13 mask 自适配**：vanilla_layer forward 收 attn_mask kwarg 不崩（catalog 标 mask_aware=true
    但 loader 不 wrap，layer 自处理 mask）。
  - **F1：no_op_layer 退出 MIP 候选**（整层 passthrough = 删层 = 改深度，违反「禁 gaming」铁律）：
    catalog 不注册 no_op_layer；get_default_candidates transformer_layer 无 no_op_layer；
    get_candidate("no_op_layer") raise。_NoOpLayer/make_no_op_layer 仅经直接 import 供 §6.7 floor。
  - **L13 transformer_layer kind 跳过 E8**：mask_load_bearing=True 的 transformer_layer slot
    不拒绝 mask_aware=false 候选（mask 自适配，MIP acc 自然惩罚）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "workflows" / "puzzle" / "agents" / "_puzzle_scripts"
sys.path.insert(0, str(SCRIPTS))

import puzzle_common as pc  # noqa: E402
import transformer_layer_variants as tlv  # noqa: E402


# ── duck-typed slot（factory 只读 slot 属性，design draft §4.4）────────────────


def _layer_slot(**kw) -> SimpleNamespace:
    """构造 transformer_layer slot（in_dim=out_dim 默认方形，max_seq_len 默认提供）。

    含 is_candidate_valid_for_slot 读取的全部字段（ffn_struct/mask_load_bearing 默认）。
    """
    base = dict(
        layer_idx=1,
        kind="transformer_layer",
        in_dim=32,
        out_dim=32,
        num_heads=4,
        head_dim=8,
        original_intermediate=64,
        activation="gelu",
        max_seq_len=16,
        norm_type="layernorm",
        ffn_struct="standard",
        mask_load_bearing=False,
        parent_module_path="encoder.layers.0",
    )
    base.update(kw)
    return SimpleNamespace(**base)


_LAYER_CANDIDATES = [
    "vanilla_layer",
    "random_synthesizer_layer",
    "relu_attention_layer",
    "fnet_layer",
    "softs_star_layer",
]


# ── catalog 加载层变体（§4.2 候选集 + §4.4 不 wrap）─────────────────────────────


def test_catalog_registers_transformer_layer_variants():
    """catalog 加载 5 个 transformer_layer 变体 + identity；F1：no_op_layer **不**注册。

    F1（L5 E2E 暴露）：no_op_layer 整层 passthrough = 删层 = 改深度，退出候选集。
    _NoOpLayer/make_no_op_layer 保留供 §6.7 floor 直接 import，但不经 catalog。
    """
    names = set(pc.candidate_registry)
    for name in _LAYER_CANDIDATES:
        assert name in names, f"catalog 缺 transformer_layer 候选 {name!r}"
    assert "transformer_layer" in pc.candidate_registry["identity"].kinds
    # F1：no_op_layer 不在 catalog（MIP 候选集禁删层）
    assert "no_op_layer" not in names, (
        "F1：no_op_layer 不应注册在 catalog（整层 passthrough = 删层，违反禁 gaming 铁律）"
    )


def test_catalog_transformer_layer_factories_are_not_wrapped():
    """§4.4：transformer_layer_variants 模块的 factory **不 wrap**——entry.factory(slot)
    直接返回 _PreLNTransformerLayer（非 _KwargPassthrough/_MaskPassthrough）。"""
    s = _layer_slot()
    for name in ("vanilla_layer", "random_synthesizer_layer", "relu_attention_layer",
                 "fnet_layer", "softs_star_layer"):
        entry = pc.candidate_registry[name]
        mod = entry.factory(s)
        assert isinstance(mod, tlv._PreLNTransformerLayer), (
            f"{name}: factory 产出非 _PreLNTransformerLayer（catalog loader 误 wrap？）"
        )


def test_catalog_softs_star_binds_core_dim_param():
    """L11 例外：softs_star_layer 的 core_dim 作 catalog params 绑定（算法超参，非 slot 维度）。
    partial 绑定后 factory(slot) 等价 make_softs_star_layer(slot, core_dim=64)。"""
    entry = pc.candidate_registry["softs_star_layer"]
    assert entry.params == {"core_dim": 64}
    s = _layer_slot(in_dim=32)
    mod = entry.factory(s)
    # _SoftsStarMixer.gen2: in→core_dim，core_dim=64 应反映在 gen2.out_features
    assert mod.attn.gen2.out_features == 64


def test_catalog_vanilla_layer_mask_aware_flag_recorded_but_no_wrap():
    """vanilla_layer catalog 标 mask_aware=true（语义记录），但 loader 不 wrap——
    layer forward 自处理 attn_mask（_extract_mask）。"""
    entry = pc.candidate_registry["vanilla_layer"]
    assert entry.mask_aware is True
    s = _layer_slot()
    mod = entry.factory(s)
    assert isinstance(mod, tlv._PreLNTransformerLayer)  # 非 _MaskPassthrough


# ── L11：维度从 slot 注入（forward shape 对 + 序列不变）─────────────────────────


@pytest.mark.parametrize("name", _LAYER_CANDIDATES)
def test_layer_variant_forward_shape_in_dim_to_out_dim_seq_unchanged(name):
    """L11：layer 变体 forward 维度 == slot 维度（in_dim→out_dim，序列长度不变）。"""
    import torch
    s = _layer_slot(in_dim=32, out_dim=32, max_seq_len=16)
    entry = pc.candidate_registry[name]
    layer = entry.factory(s)
    B, L, D = 2, 12, 32
    x = torch.randn(B, L, D)
    y = layer(x)
    assert y.shape == (B, L, D), f"{name}: forward shape {y.shape} != {(B, L, D)}"


def test_no_op_layer_factory_passthrough_via_direct_import():
    """F1：no_op_layer 不在 catalog，但 make_no_op_layer 经直接 import 仍可用（§6.7 floor）。

    意图（Rule 9）：锁定 floor 路径依赖的 make_no_op_layer forward = passthrough（return x，
    非零输出）——_NoOpLayer 保留供 floor，但不经 catalog（MIP 候选集无它）。
    """
    import torch
    s = _layer_slot(in_dim=32, out_dim=32)
    layer = tlv.make_no_op_layer(s)
    assert isinstance(layer, tlv._NoOpLayer)
    x = torch.randn(2, 10, 32)
    y = layer(x)
    assert torch.equal(y, x), "make_no_op_layer forward 应等于输入（层被旁路，floor 用）"


def test_make_no_op_layer_non_square_slot_raises():
    """F1 配套：make_no_op_layer 对非方 slot（in_dim != out_dim）fail loud raise。

    意图（Rule 9）：_NoOpLayer 的 in_dim==out_dim 守卫是 belt-and-suspenders——latency_table floor
    的非方预检短路在前（见 test_latency_table_floor_non_square_layer_kept_original），但作为公开
    factory 的 fail-loud 契约仍须直接锁定（防预检被删后构造期静默产非法块）。
    """
    s_square = _layer_slot(in_dim=32, out_dim=32)
    s_non_square = _layer_slot(in_dim=32, out_dim=48)
    # 方 slot 正常构造
    assert isinstance(tlv.make_no_op_layer(s_square), tlv._NoOpLayer)
    # 非方 slot → raise（passthrough 要求 in_dim==out_dim，否则残差直通维度不符）
    with pytest.raises(ValueError, match="in_dim==out_dim"):
        tlv.make_no_op_layer(s_non_square)


def test_layer_variant_requires_square_slot_residual_constraint():
    """Pre-LN transformer layer 的残差结构（``x + ffn(norm2(x))``）要求 in_dim == out_dim。

    设计上 transformer encoder layer 始终 in_dim==out_dim（attn/ffn 都是 R^d→R^d 映射，
    残差才合法）。in_dim != out_dim 是架构违例——现有 layer forward 在残差加法处 fail loud
    （broadcast error），证明违例不被静默吞掉。
    """
    import torch
    s = _layer_slot(in_dim=32, out_dim=48, max_seq_len=16)
    layer = pc.candidate_registry["vanilla_layer"].factory(s)
    x = torch.randn(2, 10, 32)
    # 残差 x + ffn(norm2(x)) 形状不匹配 → fail loud（不静默广播到错误 shape）
    with pytest.raises(RuntimeError):
        layer(x)


# ── L11 fail-loud：random_synthesizer_layer 缺 max_seq_len raise（BLOCKER）─────


def test_random_synthesizer_layer_missing_max_seq_len_raises():
    """L11 BLOCKER：random_synthesizer_layer 缺 max_seq_len → raise（禁 fallback 512）。

    fallback 512 对短序列（如 target seq=16）会过参化 mixing matrix（512² vs 16²），
    BLD 优化万倍过参矩阵（design draft §4.2 spec-reviewer LV-7）。
    """
    s = _layer_slot(max_seq_len=None)
    with pytest.raises(ValueError, match="max_seq_len"):
        pc.candidate_registry["random_synthesizer_layer"].factory(s)


def test_random_synthesizer_layer_zero_max_seq_len_raises():
    """max_seq_len=0 视同缺失 → raise（mixing matrix 至少 1×1）。"""
    s = _layer_slot(max_seq_len=0)
    with pytest.raises(ValueError, match="max_seq_len"):
        pc.candidate_registry["random_synthesizer_layer"].factory(s)


# ── L13：mask 自适配（vanilla_layer 收 attn_mask forward 不崩）───────────────────


def test_vanilla_layer_forward_with_attn_mask_kwarg_does_not_crash():
    """L13：vanilla_layer forward 收 attn_mask kwarg 不崩——nn.MultiheadAttention 真用 mask。

    layer 自包含 _extract_mask 从 kwargs 抽 mask，**不经 _MaskPassthrough wrap**。
    """
    import torch
    s = _layer_slot(in_dim=32, out_dim=32, num_heads=4)
    layer = pc.candidate_registry["vanilla_layer"].factory(s)
    B, L, D = 2, 8, 32
    x = torch.randn(B, L, D)
    attn_mask = torch.zeros(L, L)  # nn.MultiheadAttention 接受 float mask
    y = layer(x, attn_mask=attn_mask)
    assert y.shape == (B, L, D)


def test_vanilla_layer_forward_with_positional_src_mask_extracted():
    """L13：父层以 positional src_mask 调用时，_extract_mask 从首 kwarg 抽出转交 attn。

    design draft §4.4 forward 契约：forward(x, src_mask=None, *args, **kwargs)。
    """
    import torch
    s = _layer_slot(in_dim=32, out_dim=32, num_heads=4)
    layer = pc.candidate_registry["vanilla_layer"].factory(s)
    x = torch.randn(2, 6, 32)
    src_mask = torch.zeros(6, 6)
    y = layer(x, src_mask)
    assert y.shape == (2, 6, 32)


def test_mask_blind_variant_ignores_attn_mask_kwarg():
    """L13：mask-blind 变体（fnet_layer）收 attn_mask kwarg 不崩——接受但忽略。"""
    import torch
    s = _layer_slot(in_dim=32, out_dim=32)
    layer = pc.candidate_registry["fnet_layer"].factory(s)
    x = torch.randn(2, 8, 32)
    attn_mask = torch.zeros(8, 8)
    y = layer(x, attn_mask=attn_mask)
    assert y.shape == (2, 8, 32)


# ── is_candidate_valid_for_slot：L13 transformer_layer kind 跳过 E8 ─────────────


def test_transformer_layer_kind_skips_e8_mask_filter():
    """L13：transformer_layer kind 的 mask_load_bearing slot 不拒绝 mask_aware=false 候选。

    fnet_layer.mask_aware=false，但 slot.kind=transformer_layer → 不 E8 拒绝
    （mask-blind 变体由 MIP acc 自然惩罚，不硬过滤）。
    """
    s = _layer_slot(in_dim=32, out_dim=32, mask_load_bearing=True)
    assert pc.is_candidate_valid_for_slot("fnet_layer", s) is True
    assert pc.is_candidate_valid_for_slot("vanilla_layer", s) is True


def test_attention_kind_still_applies_e8_mask_filter():
    """对照：attention kind 的 mask_load_bearing slot 仍拒绝 mask_aware=false 候选（E8）。"""
    s = SimpleNamespace(
        layer_idx=0, kind="attention", in_dim=8, out_dim=8,
        num_heads=2, head_dim=4, source_class="A",
        parent_module_path="x", ffn_struct="standard",
        mask_load_bearing=True,
    )
    # fnet（attention kind，mask_aware=false）应被 E8 拒绝
    assert pc.is_candidate_valid_for_slot("fnet", s) is False
    # masked_vanilla（attention kind，mask_aware=true）应通过
    assert pc.is_candidate_valid_for_slot("masked_vanilla", s) is True


def test_no_op_layer_not_registered_in_catalog_raises():
    """F1：no_op_layer 已退出 catalog——get_candidate / is_candidate_valid_for_slot 均 raise。

    意图（Rule 9）：锁定 no_op_layer 不再是合法 MIP 候选（禁删层）。任何经 catalog 的查询
    （get_candidate 直查、is_candidate_valid_for_slot 间接经 get_candidate）都须 fail loud，
    防 no_op_layer 漏回候选集。
    """
    s = _layer_slot(in_dim=32, out_dim=32)
    with pytest.raises(ValueError, match="未在 catalog 注册"):
        pc.get_candidate("no_op_layer")
    with pytest.raises(ValueError, match="未在 catalog 注册"):
        pc.is_candidate_valid_for_slot("no_op_layer", s)


def test_transformer_layer_candidate_rejected_for_attention_slot():
    """跨 kind 适用性：transformer_layer 候选不适用 attention slot。"""
    s = SimpleNamespace(
        layer_idx=0, kind="attention", in_dim=8, out_dim=8,
        num_heads=2, head_dim=4, source_class="A",
        parent_module_path="x", ffn_struct="standard",
        mask_load_bearing=False,
    )
    assert pc.is_candidate_valid_for_slot("vanilla_layer", s) is False


# ── get_default_candidates transformer_layer kind（§2.2）────────────────────────


def test_default_candidates_transformer_layer_kind_contents():
    """默认 transformer_layer 候选含 identity（floor 锚）+ 5 真 attention 变体；F1：无 no_op_layer。"""
    d = pc.get_default_candidates()
    assert d["transformer_layer"][0] == "identity"
    assert set(d["transformer_layer"]) == {
        "identity", "vanilla_layer", "random_synthesizer_layer",
        "relu_attention_layer", "fnet_layer", "softs_star_layer",
    }
    # F1：no_op_layer 不入默认候选（整层 passthrough = 删层，违反禁 gaming 铁律）
    assert "no_op_layer" not in d["transformer_layer"]


def test_parse_block_candidates_accepts_transformer_layer():
    """parse_block_candidates 接受 transformer_layer kind + catalog 注册校验通过。"""
    import json
    raw = json.dumps({
        "transformer_layer": ["identity", "fnet_layer", "vanilla_layer"],
    })
    d = pc.parse_block_candidates(raw)
    assert d["transformer_layer"] == ["identity", "fnet_layer", "vanilla_layer"]


def test_parse_block_candidates_rejects_unknown_transformer_layer_candidate():
    """未注册的 transformer_layer 候选名 → raise（catalog 注册门）。"""
    import json
    raw = json.dumps({"transformer_layer": ["identity", "bogus_layer"]})
    with pytest.raises(ValueError, match="未在 catalog 注册"):
        pc.parse_block_candidates(raw)


def test_parse_block_candidates_rejects_no_op_layer_as_candidate():
    """F1：用户经 block_candidates 显式传 no_op_layer → raise（已退出 catalog，禁回候选集）。

    意图（Rule 9）：锁定 no_op_layer 即使被用户显式声明也不许回 MIP 候选——它整层 passthrough
    = 删层，违反「禁删计算/改深度」铁律。parse_block_candidates 经 candidate_registry 查
    no_op_layer → None → raise（catalog 注册门）。
    """
    import json
    raw = json.dumps({"transformer_layer": ["identity", "no_op_layer"]})
    with pytest.raises(ValueError, match="未在 catalog 注册"):
        pc.parse_block_candidates(raw)


def test_default_candidates_layer_set_has_no_delete_depth_option():
    """F1 intent：默认 transformer_layer 候选集 = identity（保原层）+ 5 真 attention 变体。

    每个候选都「履行层职能」（identity 保留原 attn+ffn，真变体替换 attention 机制但保留 ffn/
    norm/residual 骨架），无「删层」选项。验证候选集不存在「跳过整层计算」的 gaming 路径。
    """
    d = pc.parse_block_candidates("")  # 默认候选集（空 input → get_default_candidates）
    layer_cands = d["transformer_layer"]
    # identity + 5 真 attention 变体（每个都做计算或保留原层，非 passthrough 删层）
    assert layer_cands[0] == "identity"
    for name in layer_cands[1:]:
        entry = pc.get_candidate(name)
        assert entry.source == "builtin", f"{name} 须 builtin（真计算变体，非 passthrough）"
        assert entry.factory is not None, f"{name} 须有 factory（履行层职能）"


def test_parse_block_candidates_rejects_transformer_layer_in_wrong_kind():
    """已注册的 transformer_layer 候选放进 attention kind → raise（跨 kind 适用性守卫）。

    防止未来误改 parse_block_candidates 跳过 transformer_layer kind 的校验。
    """
    import json
    raw = json.dumps({"attention": ["identity", "vanilla_layer"]})
    with pytest.raises(ValueError, match="不适用于 kind"):
        pc.parse_block_candidates(raw)


# ── Slot schema：max_seq_len / norm_type 字段（§2.1）────────────────────────────


def test_slot_max_seq_len_and_norm_type_default_none():
    """新字段默认 None（向后兼容现有构造）。"""
    s = pc.Slot(
        layer_idx=0, kind="transformer_layer", in_dim=8, out_dim=8,
        num_heads=2, head_dim=4, source_class="A", parent_module_path="x",
    )
    assert s.max_seq_len is None
    assert s.norm_type is None


def test_block_map_json_roundtrip_preserves_layer_fields(tmp_path):
    """to_json/from_json round-trip 保留 max_seq_len / norm_type。"""
    s = pc.Slot(
        layer_idx=1, kind="transformer_layer", in_dim=32, out_dim=32,
        num_heads=4, head_dim=8, source_class="TransformerEncoderLayer",
        parent_module_path="encoder.layers.1",
        original_intermediate=64, activation="gelu",
        max_seq_len=128, norm_type="layernorm",
    )
    bm = pc.BlockMap(slots=[s])
    p = bm.to_json(tmp_path / "bm.json")
    bm2 = pc.BlockMap.from_json(p)
    assert bm2.slots[0] == s


# ── search_space_io round-trip（max_seq_len / norm_type 透传到 yaml/slot）────────


def test_search_space_yaml_roundtrip_preserves_layer_fields(tmp_path):
    """save → load round-trip：max_seq_len/norm_type 透传到 yaml + to_block_map Slot。"""
    import yaml
    yaml_path = tmp_path / "search_space.yaml"
    slot_dicts = [{
        "id": "L1_transformer_layer",
        "path": "encoder.layers.0",
        "kind": "transformer_layer",
        "layer_idx": 1,
        "in_dim": 32,
        "out_dim": 32,
        "num_heads": 4,
        "head_dim": 8,
        "source_class": "TransformerEncoderLayer",
        "original_intermediate": 64,
        "activation": "gelu",
        "max_seq_len": 128,
        "norm_type": "layernorm",
    }]
    candidates = {"transformer_layer": ["identity", "vanilla_layer", "fnet_layer"]}
    import search_space_io as sio
    sio.save_search_space_yaml(yaml_path, slot_dicts, candidates)

    # YAML 文件含新字段
    with open(yaml_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    assert raw["slots"][0]["max_seq_len"] == 128
    assert raw["slots"][0]["norm_type"] == "layernorm"

    # load 回 → to_block_map Slot 字段对齐
    loaded_dicts, _ = sio.load_search_space_yaml(yaml_path)
    bm = sio.to_block_map(loaded_dicts)
    assert bm.slots[0].max_seq_len == 128
    assert bm.slots[0].norm_type == "layernorm"
    assert bm.slots[0].kind == "transformer_layer"


def test_search_space_yaml_loads_transformer_layer_kind(tmp_path):
    """transformer_layer kind 通过 _ALLOWED_KINDS 校验（design draft §2.1）。"""
    import yaml
    yaml_path = tmp_path / "ss.yaml"
    payload = {
        "slots": [{
            "id": "L1_transformer_layer",
            "path": "enc.0",
            "kind": "transformer_layer",
            "layer_idx": 1,
            "in_dim": 32,
            "out_dim": 32,
            "num_heads": 4,
            "head_dim": 8,
        }],
        "candidates": {"transformer_layer": ["identity", "fnet_layer"]},
    }
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
    import search_space_io as sio
    slot_dicts, candidates = sio.load_search_space_yaml(yaml_path)
    assert slot_dicts[0]["kind"] == "transformer_layer"
    assert candidates["transformer_layer"] == ["identity", "fnet_layer"]


# ── _resolve_builtin_factory 直接单测（resolver 边界契约）────────────────────────


def test_resolve_builtin_factory_transformer_layer_variants_returns_unwrapped_partial():
    """resolver 边界契约（§4.4）：transformer_layer_variants::make_* 直接返回 partial，
    不经 _wrap/_wrap_mask 包装——锁定在 resolver 边界而非仅经 catalog 间接覆盖。"""
    import functools
    factory = pc._resolve_builtin_factory(
        "transformer_layer_variants::make_softs_star_layer", {"core_dim": 32}
    )
    # partial 绑定 core_dim=32，但 slot 未绑——证明返回的是 factory(slot) 不是已实例化模块
    assert isinstance(factory, functools.partial)
    assert factory.keywords == {"core_dim": 32}
    mod = factory(_layer_slot(in_dim=32))
    assert isinstance(mod, tlv._PreLNTransformerLayer)
    assert mod.attn.gen2.out_features == 32  # core_dim 绑定生效


def test_resolve_builtin_factory_puzzle_blocks_still_wraps():
    """对照：puzzle_blocks::make_* 仍走 _wrap 包装（向后兼容 attention/ffn 候选）。

    用 make_ffn（无 nas_agent 依赖）而非 make_fnet（懒加载 nas_agent.blocks），
    避免环境缺 nas_agent 时假阳性失败。
    """
    import puzzle_blocks as pb
    factory = pc._resolve_builtin_factory("puzzle_blocks::make_ffn", {"ratio": 0.5})
    s = SimpleNamespace(
        in_dim=8, out_dim=8, num_heads=2, head_dim=4,
        parent_module_path="x",
        original_intermediate=16, activation="gelu",
    )
    mod = factory(s)
    assert isinstance(mod, pb._KwargPassthrough), "puzzle_blocks factory 应被 _wrap 包装"


def test_resolve_builtin_factory_transformer_layer_variants_unknown_callable_raises(tmp_path):
    """transformer_layer_variants::make_nonexistent → AttributeError fail loud
    （belt-and-suspenders：保护新模块的 getattr 路径不退化）。"""
    p = tmp_path / "badfactory.yaml"
    p.write_text(
        "- name: bogus_layer\n  kind: [transformer_layer]\n  source: builtin\n"
        "  factory: transformer_layer_variants::make_nonexistent\n  params: {}\n"
        "- name: identity\n  kind: [transformer_layer]\n  source: passthrough\n"
        "  factory: null\n  params: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(AttributeError, match="transformer_layer_variants 无 callable"):
        pc.load_catalog(path=p)


# ── norm_type 非 dispatch 依据 intent（§2.1）────────────────────────────────────


def test_norm_type_is_not_dispatch_basis_for_factory_construction():
    """§2.1 intent：norm_type 是溯源记录**非** dispatch 依据——factory 构造不读 norm_type。

    无论 norm_type=layernorm / rmsnorm / None，factory 都用自带 LayerNorm（Pre-LN 变体
    自处理 norm，design draft R1）。验证：norm_type=None 时 vanilla_layer 仍正常构造 forward。
    """
    import torch
    s = _layer_slot(in_dim=32, out_dim=32, norm_type=None)
    layer = pc.candidate_registry["vanilla_layer"].factory(s)
    assert isinstance(layer.norm1, torch.nn.LayerNorm)  # 变体自带 LayerNorm
    x = torch.randn(2, 8, 32)
    y = layer(x)
    assert y.shape == (2, 8, 32)


# ── save_search_space_yaml None 省略（序列化决策）──────────────────────────────


def test_save_search_space_yaml_omits_none_layer_fields(tmp_path):
    """search_space_io None 省略：slot 缺 max_seq_len/norm_type 时 YAML 不落盘该 key
    （避免污染 YAML，to_block_map 默认 None 已兜底）。"""
    import yaml
    yaml_path = tmp_path / "ss.yaml"
    slot_dicts = [{
        "id": "L1_transformer_layer", "path": "enc.0",
        "kind": "transformer_layer", "layer_idx": 1,
        "in_dim": 32, "out_dim": 32,
    }]
    candidates = {"transformer_layer": ["identity", "fnet_layer"]}
    import search_space_io as sio
    sio.save_search_space_yaml(yaml_path, slot_dicts, candidates)
    with open(yaml_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    assert "max_seq_len" not in raw["slots"][0]
    assert "norm_type" not in raw["slots"][0]
