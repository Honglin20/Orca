"""test_puzzle_catalog.py —— Phase U1 契约层单元测试。

锁定 candidate catalog loader / builtin factory / Slot schema 的 intent（SPEC v2
§4/§5/§17 闭环 E1/E3/E4/E6/E7/E8/E15/E23），覆盖 happy path + fail-loud 分支。

重点（Rule 9：验证 intent 非 behavior）：
  - **E7 ratio 数学**（BLOCKER）：make_ffn 的 intermediate = original_intermediate × ratio，
    不是 v1 错误的 in_dim × ratio。若无断言，回归 v1 bug 不会报警。
  - **E23/E7 fail-loud**（BLOCKER）：activation=None / original_intermediate=None → raise。
  - **load_catalog fail-loud**（MAJOR）：新生产代码 ~10 条 raise 分支需独立覆盖。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "workflows" / "agents" / "_puzzle_scripts"
sys.path.insert(0, str(SCRIPTS))

import puzzle_common as pc  # noqa: E402
import puzzle_blocks as pb  # noqa: E402


def _slot(**kw) -> SimpleNamespace:
    """duck-typed slot（factory 只读 in_dim/out_dim/activation/... 属性）。"""
    base = dict(
        in_dim=8, out_dim=8, num_heads=4, head_dim=2,
        parent_module_path="x", activation=None, original_intermediate=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── make_ffn E7：ratio 相对 original_intermediate（不是 in_dim）──────────────


def test_make_ffn_e7_ratio_uses_original_intermediate_not_in_dim():
    """E7（SPEC §7.3）：intermediate = original_intermediate × ratio。

    original_intermediate=384, in_dim=96, ratio=0.5 → 192（正确）；
    v1 bug（in_dim × ratio）会给 48。本测试锁定 SPEC v2 的 headline fix。
    """
    s = _slot(in_dim=96, out_dim=96, original_intermediate=384, activation="gelu")
    blk = pb.make_ffn(s, ratio=0.5)
    # nn.Sequential: [Linear(in→intermediate), Act, Linear(intermediate→out)]
    assert blk[0].out_features == 192, "E7: intermediate 须=original_intermediate*ratio=192"
    assert blk[0].in_features == 96
    assert blk[2].in_features == 192
    assert blk[2].out_features == 96


def test_make_ffn_e7_ratio_075():
    s = _slot(in_dim=32, out_dim=32, original_intermediate=64, activation="relu")
    blk = pb.make_ffn(s, ratio=0.75)
    assert blk[0].out_features == 48  # 64*0.75


# ── make_ffn E23 / E7 fail-loud ──────────────────────────────────────────────


def test_make_ffn_e23_activation_none_raises():
    """E23：ffn slot 缺 activation → raise（SPEC §4.1/§7.3）。"""
    s = _slot(activation=None, original_intermediate=16)
    with pytest.raises(ValueError, match="activation"):
        pb.make_ffn(s, ratio=0.5)


def test_make_ffn_e7_original_intermediate_none_raises():
    """E7：ffn slot 缺 original_intermediate（ratio 基准）→ raise。"""
    s = _slot(activation="gelu", original_intermediate=None)
    with pytest.raises(ValueError, match="original_intermediate"):
        pb.make_ffn(s, ratio=0.5)


# ── resolve_activation ───────────────────────────────────────────────────────


def test_resolve_activation_returns_correct_class():
    assert pb.resolve_activation("gelu") is __import__("torch").nn.GELU
    assert pb.resolve_activation("relu") is __import__("torch").nn.ReLU


def test_resolve_activation_unknown_raises():
    """未知激活名 fail-loud（E23 配套）。"""
    with pytest.raises(ValueError, match="未知 activation"):
        pb.resolve_activation("bogus_act")


def test_activation_class_to_name_map_is_inverse():
    """公开反向映射与 _ACTIVATION_MAP 互逆（DRY 单一真相源）。"""
    for name, cls in pb._ACTIVATION_MAP.items():
        assert pb.ACTIVATION_CLASS_TO_NAME[cls] == name


# ── make_zero / _ZeroBlock ───────────────────────────────────────────────────


def test_make_zero_requires_equal_io_dim():
    assert isinstance(pb.make_zero(_slot(in_dim=8, out_dim=8)), pb._ZeroBlock)
    with pytest.raises(ValueError, match="in_dim==out_dim"):
        pb.make_zero(_slot(in_dim=8, out_dim=16))


def test_make_zero_block_outputs_zeros_with_kwargs():
    """_ZeroBlock 接受 kwargs（异构父层 forward 签名），输出零。"""
    import torch
    blk = pb._ZeroBlock()
    x = torch.randn(2, 5, 8)
    y = blk(x, attention_mask=None, norm_factor=0.1)  # kwargs 被忽略
    assert torch.count_nonzero(y) == 0
    assert y.shape == x.shape


# ── _KwargPassthrough 剥 kwargs（SPEC §5.2）──────────────────────────────────


def test_kwarg_passthrough_strips_kwargs():
    """_KwargPassthrough 调 inner(x)，忽略父层传的 kwargs（异构签名适配）。"""
    import torch
    import torch.nn as nn
    inner = nn.Linear(8, 8)
    w = pb._KwargPassthrough(inner)
    x = torch.randn(2, 8)
    y = w(x, attention_mask="ignored", key_padding_mask="ignored")
    assert torch.equal(y, inner(x))


# ── get_candidate ────────────────────────────────────────────────────────────


def test_get_candidate_returns_entry():
    e = pc.get_candidate("fnet")
    assert e.name == "fnet"
    assert "attention" in e.kinds


def test_get_candidate_unregistered_raises():
    with pytest.raises(ValueError, match="未在 catalog 注册"):
        pc.get_candidate("nonexistent")


def test_get_candidate_identity_factory_is_none():
    """identity（passthrough）factory=None——永不实例化（SPEC §3）。"""
    assert pc.get_candidate("identity").factory is None
    assert pc.get_candidate("identity").source == "passthrough"


# ── catalog E4：functools.partial 绑定 params + _wrap ────────────────────────


def test_catalog_ffn_50_factory_binds_ratio_and_wraps():
    """E4：catalog loader 用 functools.partial 绑定 params 成 factory(slot)。

    ffn_50 entry.factory(slot) 应产出 _KwargPassthrough(Sequential)，且
    intermediate = original_intermediate × 0.5（E7 经 partial 绑定后仍生效）。
    """
    entry = pc.candidate_registry["ffn_50"]
    s = _slot(in_dim=32, out_dim=32, original_intermediate=64, activation="gelu")
    mod = entry.factory(s)
    assert isinstance(mod, pb._KwargPassthrough)
    assert mod.inner[0].out_features == 32  # 64 * 0.5


# ── load_catalog fail-loud 分支（MAJOR）──────────────────────────────────────


def test_load_catalog_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="candidate_catalog.yaml"):
        pc.load_catalog(path=tmp_path / "nope.yaml")


def test_load_catalog_top_level_non_list_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("name: fnet\nkind: [attention]\n", encoding="utf-8")  # dict 非 list
    with pytest.raises(ValueError, match="顶层须为 list"):
        pc.load_catalog(path=p)


def test_load_catalog_entry_missing_required_fields_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    # 缺 source / factory
    p.write_text("- name: fnet\n  kind: [attention]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺字段"):
        pc.load_catalog(path=p)


def test_load_catalog_unknown_source_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text(
        "- name: foo\n  kind: [attention]\n  source: mystery\n  factory: null\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="source 须为 builtin\\|passthrough"):
        pc.load_catalog(path=p)


def test_load_catalog_missing_identity_raises(tmp_path, monkeypatch):
    """E1 catalog-level：catalog 缺 identity → raise。"""
    p = tmp_path / "noidentity.yaml"
    p.write_text(
        "- name: fnet\n  kind: [attention]\n  source: builtin\n"
        "  factory: puzzle_blocks::make_fnet\n  params: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="passthrough 候选 'identity'"):
        pc.load_catalog(path=p)


def test_load_catalog_unresolvable_builtin_factory_raises(tmp_path):
    """builtin factory 函数不存在 → raise。"""
    p = tmp_path / "badfactory.yaml"
    p.write_text(
        "- name: fnet\n  kind: [attention]\n  source: builtin\n"
        "  factory: puzzle_blocks::make_nonexistent\n  params: {}\n"
        "- name: identity\n  kind: [attention]\n  source: passthrough\n"
        "  factory: null\n  params: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(AttributeError, match="无 callable 'make_nonexistent'"):
        pc.load_catalog(path=p)


def test_load_catalog_builtin_factory_wrong_module_raises(tmp_path):
    """builtin factory 模块不在白名单 → raise，错误消息列出全部合法模块（catalog 契约边界）。

    regex 同时匹配 puzzle_blocks + transformer_layer_variants：防止消息措辞回归
    （如误删 transformer_layer_variants 致用户看不到该模块合法）。
    """
    p = tmp_path / "badmod.yaml"
    p.write_text(
        "- name: fnet\n  kind: [attention]\n  source: builtin\n"
        "  factory: other_mod::make_fnet\n  params: {}\n"
        "- name: identity\n  kind: [attention]\n  source: passthrough\n"
        "  factory: null\n  params: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"puzzle_blocks.*transformer_layer_variants"):
        pc.load_catalog(path=p)


def test_load_catalog_happy_path_loads_all_entries():
    """模块级 candidate_registry（= load_catalog()）已加载全集。"""
    names = set(pc.candidate_registry)
    # attention + ffn + identity builtins
    assert {"fnet", "random_synthesizer", "relu_attention", "softs_star", "vanilla"} <= names
    assert {"ffn_75", "ffn_50", "linear"} <= names
    assert {"no_op", "identity"} <= names


# ── Slot schema / BlockMap round-trip（MAJOR-4 / MINOR-1）────────────────────


def test_slot_defaults_for_minimal_construction():
    """5 个新字段有合理默认（向后兼容部分 JSON + attention slot 无 ffn meta）。"""
    s = pc.Slot(
        layer_idx=0, kind="attention", in_dim=8, out_dim=8,
        num_heads=4, head_dim=2, source_class="A", parent_module_path="x",
    )
    assert s.return_arity == "single"
    assert s.original_intermediate is None
    assert s.activation is None
    assert s.ffn_struct == "standard"
    assert s.mask_load_bearing is False


def test_block_map_json_roundtrip_preserves_new_fields(tmp_path):
    """to_json/from_json round-trip 保留全部新字段（E3/E6/E7/E8/E15）。"""
    s = pc.Slot(
        layer_idx=0, kind="ffn", in_dim=8, out_dim=8, num_heads=0, head_dim=0,
        source_class="FFN", parent_module_path="block.ffn",
        return_arity="multi", original_intermediate=32, activation="gelu",
        ffn_struct="glu", mask_load_bearing=True,
    )
    bm = pc.BlockMap(slots=[s])
    p = bm.to_json(tmp_path / "bm.json")
    bm2 = pc.BlockMap.from_json(p)
    assert bm2.slots[0] == s  # dataclass __eq__ 全字段比对


# ── parse_block_candidates E1（补强：conv/moe/custom 默认 + D4）──────────────


def test_parse_default_candidates_include_all_kinds():
    """默认候选集含 6 个 kind（D4 + transformer_layer），每个都含 identity（E1）。"""
    d = pc.parse_block_candidates("")
    assert set(d) == {"attention", "ffn", "conv", "moe", "custom", "transformer_layer"}
    for kind, cands in d.items():
        assert "identity" in cands, f"{kind} 缺 identity（E1）"
    assert d["conv"] == ["identity"]
    assert d["moe"] == ["identity"]
