"""test_psu_layer_variants.py —— PSU 变体快照（assets/layer_variants）单测。

锁定快照的 intent（D4：源码复制零跨文件 import + 5 工厂 + no_op 不入集）：
  - 自包含：模块级 import 仅 stdlib/torch，无任何 puzzle_* / nas_agent 跨文件依赖
    （源文件 :48 的 ``from puzzle_blocks import resolve_activation`` 已内联消除）。
  - 5 个工厂可构造 + forward 形状不变（in_dim → out_dim，序列长度不变）。
  - mask kwarg 不崩（mask-blind 变体接受并忽略）。
  - random_synthesizer 缺 max_seq_len → fail loud（禁 fallback）。
  - no_op 不在快照内（不在 PSU 分支集）。
  - 文件头 provenance 声明源路径 + commit + 日期。
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = (
    REPO / "workflows" / "agents" / "psu_expand_supernet" / "assets" / "layer_variants"
    / "transformer_layer_variants.py"
)

FACTORIES = [
    "make_vanilla_layer",
    "make_random_synthesizer_layer",
    "make_relu_attention_layer",
    "make_fnet_layer",
    "make_softs_star_layer",
]


def _load_snapshot():
    spec = importlib.util.spec_from_file_location("psu_layer_variants_snapshot", SNAPSHOT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mock_slot(**kw) -> SimpleNamespace:
    base = dict(
        in_dim=32, out_dim=32, num_heads=4, head_dim=8,
        original_intermediate=64, activation="gelu",
        max_seq_len=16, parent_module_path="mock",
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── 自包含（零跨文件 import）─────────────────────────────────────────────────


def test_no_cross_file_imports():
    """模块级 import 仅 stdlib + torch（快照零跨文件依赖 = D4 的存在理由）。"""
    tree = ast.parse(SNAPSHOT.read_text(encoding="utf-8"))
    allowed_top = {"torch", "math", "copy", "typing", "types", "dataclasses", "__future__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            names = {node.module.split(".")[0]}
        else:
            continue
        bad = names - allowed_top
        assert not bad, f"快照出现非白名单 import: {bad}（须内联，禁跨文件依赖）"


def test_no_puzzle_references():
    """正文（除 provenance 头）无 puzzle 运行时依赖词（puzzle_blocks/puzzle_common/catalog/MIP）。"""
    src = SNAPSHOT.read_text(encoding="utf-8")
    body = src.split('"""', 2)[2]  # 剥模块 docstring（provenance 允许提及源路径）
    for word in ("puzzle_blocks", "puzzle_common", "candidate_catalog", "MIP", "no_op"):
        assert word not in body, f"快照正文残留 {word!r}"


def test_provenance_header_declares_source():
    """文件头 provenance：自包含快照声明 + 定版日期；禁内部仓库路径（洁净契约）。"""
    header = SNAPSHOT.read_text(encoding="utf-8").split('"""')[1]
    assert "2026-08-17" in header
    assert "唯一事实源" in header
    # 洁净契约：运行时资源不带内部仓库路径 / commit 考古
    assert "_puzzle_scripts" not in header
    assert "commit" not in header


# ── 5 工厂可构造 + forward 形状不变 ──────────────────────────────────────────


@pytest.mark.parametrize("factory_name", FACTORIES)
def test_factory_construct_and_forward_shape(factory_name):
    mod = _load_snapshot()
    fn = getattr(mod, factory_name)
    slot = _mock_slot()
    layer = fn(slot)
    B, L, D = 2, 12, 32
    x = torch.randn(B, L, D)
    y = layer(x)
    assert y.shape == (B, L, slot.out_dim)


def test_mask_kwargs_do_not_crash():
    """mask-blind 变体接受并忽略 mask；mask-aware（vanilla）透传。"""
    mod = _load_snapshot()
    L = 12
    mask = torch.tril(torch.ones(L, L, dtype=torch.bool))
    x = torch.randn(1, L, 32)
    for factory_name in FACTORIES:
        layer = getattr(mod, factory_name)(_mock_slot())
        y = layer(x, attn_mask=mask)  # kwarg 形式
        assert y.shape == (1, L, 32)
        y2 = layer(x, mask)  # positional src_mask 形式
        assert y2.shape == (1, L, 32)


def test_dims_come_from_slot_not_hardcoded():
    """维度从 slot 注入（非 32 维 slot 也产出对应形状）。"""
    mod = _load_snapshot()
    slot = _mock_slot(in_dim=64, out_dim=64, num_heads=8, head_dim=8,
                      original_intermediate=128, max_seq_len=24)
    layer = mod.make_vanilla_layer(slot)
    y = layer(torch.randn(1, 24, 64))
    assert y.shape == (1, 24, 64)


# ── fail loud ──────────────────────────────────────────────────────────────


def test_random_synthesizer_missing_max_seq_len_fails_loud():
    """缺 max_seq_len → raise（禁 fallback 大值过参化 mixing matrix）。"""
    mod = _load_snapshot()
    slot = _mock_slot(max_seq_len=None)
    with pytest.raises(ValueError, match="max_seq_len"):
        mod.make_random_synthesizer_layer(slot)


def test_unknown_activation_fails_loud():
    mod = _load_snapshot()
    slot = _mock_slot(activation="bogus_act")
    with pytest.raises(ValueError, match="activation"):
        mod.make_vanilla_layer(slot)


def test_no_op_not_in_snapshot():
    """no_op layer 不在快照（不在 PSU 分支集）。"""
    mod = _load_snapshot()
    assert not hasattr(mod, "make_no_op_layer")
    assert not hasattr(mod, "_NoOpLayer")
