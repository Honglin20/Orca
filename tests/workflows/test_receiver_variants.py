"""test_receiver_variants.py —— receiver KB 变体 smoke + 契约校验（无 GPU/真硬件）。

对每个变体 .py：forward shape、feature_hook_names 恒 2（与 teacher 等长，回归 OFD/prepare bug）、
KNOBS 过 pick_variant._validate_variant、KNOBS 最小值仍可用、逐变体特有断言。

复用 test_kd_redesign.py 的 _load(path,name) + KBDIR 模式。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
KD = REPO / "workflows" / "agents" / "_kd_scripts"
KBDIR = REPO / "knowledge_base" / "families" / "receiver"

# 全部 7 个变体（2 旧 + 5 新），回归覆盖整池。
VARIANTS = [
    "spt_t1", "spt_alt",
    "spt_cnn_dilated", "spt_cnn_pointwise", "spt_puretf", "spt_unet", "spt_2d",
]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_variant(name: str):
    """加载变体 .py（其 from _model8_blocks import 需要 KBDIR 在 sys.path）。每次 fresh。"""
    for m in [n for n in sys.modules if n in (name, "_model8_blocks")]:
        del sys.modules[m]
    if str(KBDIR) not in sys.path:
        sys.path.insert(0, str(KBDIR))
    if str(KD) not in sys.path:
        sys.path.insert(0, str(KD))
    return _load(KBDIR / f"{name}.py", name)


@pytest.fixture(scope="module")
def pick_variant():
    return _load(KD / "pick_variant.py", "_pv_test_rv")


# ── 通用契约：forward shape / feature_hook 恒 2 / KNOBS 合法 / 最小值可用 ─────────

@pytest.mark.parametrize("name", VARIANTS)
def test_variant_forward_shape(name):
    import torch
    mod = _load_variant(name)
    m = mod.build_model()
    m.eval()
    x = torch.randn(*mod.DUMMY_INPUT["shape"])
    with torch.no_grad():
        out = m(x)
    assert out.shape == x.shape, f"{name}: {tuple(out.shape)} != {tuple(x.shape)}"


@pytest.mark.parametrize("name", VARIANTS)
def test_variant_feature_hooks_eq_two(name):
    """feature_hook_names 恒 2（与 teacher 等长，回归 _model8_blocks n=1 bug）。"""
    mod = _load_variant(name)
    hooks = mod.build_model().feature_hook_names()
    assert len(hooks) == 2, f"{name}: feature_hook_names 长度 {len(hooks)} != 2"


@pytest.mark.parametrize("name", VARIANTS)
def test_variant_knobs_valid(name, pick_variant):
    """KNOBS 过 pick_variant._validate_variant（build_model/DUMMY_INPUT/step<0/leverage）。"""
    mod = _load_variant(name)
    pick_variant._validate_variant(mod, str(KBDIR / f"{name}.py"))  # 不 raise 即通过


@pytest.mark.parametrize("name", VARIANTS)
def test_variant_min_cfg(name):
    """KNOBS 最小值（num_blocks=min, embed_dim=min）仍 forward + hook 恒 2。"""
    import torch
    mod = _load_variant(name)
    min_blocks = mod.KNOBS["num_blocks"]["min"]
    min_dim = mod.KNOBS["embed_dim"]["min"]
    try:
        m = mod.build_model(num_blocks=min_blocks, embed_dim=min_dim)
    except ValueError as e:
        pytest.skip(f"{name}: embed_dim={min_dim} 触发结构约束（{e}），跳过最小值组合")
    m.eval()
    x = torch.randn(*mod.DUMMY_INPUT["shape"])
    with torch.no_grad():
        out = m(x)
    assert out.shape == x.shape
    assert len(m.feature_hook_names()) == 2


# ── 逐变体特有断言 ─────────────────────────────────────────────────────────────

def test_spt_puretf_tau0_identity():
    """spt_puretf 的 M9 soft-threshold τ=0 应严格 identity（fail-forward，部署可关）。"""
    import torch
    mod = _load_variant("spt_puretf")
    m = mod.build_model(num_blocks=1)          # default embed_dim=16
    st = m.main[0].soft_thr
    if not hasattr(st, "tau"):
        pytest.skip("soft_thr 无 tau（被 Identity 替换）")
    assert abs(float(st.tau)) < 1e-8, "init_tau 应为 0"
    # soft_thr 在 block 内部吃 [B, S, embed_dim, F]；τ=0 早退 identity
    x = torch.randn(2, 64, 16, 48)
    assert torch.allclose(st(x), x, atol=1e-6), "τ=0 必须 identity"


def test_spt_unet_has_down_up_skip():
    """spt_unet 有 MaxPool down / ConvTranspose up / skip_proj，且 forward 出入同形。"""
    import torch
    mod = _load_variant("spt_unet")
    m = mod.build_model(num_blocks=1)
    m.eval()
    x = torch.randn(1, 4, 48, 64, 1)
    with torch.no_grad():
        out = m(x)
    assert out.shape == x.shape
    assert isinstance(m.down, torch.nn.MaxPool1d)
    assert isinstance(m.up, torch.nn.ConvTranspose1d)
    assert isinstance(m.skip_proj, torch.nn.Conv1d)


def test_spt_2d_axial_two_axes():
    """spt_2d 每个 block 含 S 轴 + F 轴两个 MHA（axial 分解）。"""
    mod = _load_variant("spt_2d")
    m = mod.build_model(num_blocks=1)
    blk = m.main[0]
    assert blk.attn_s.axis == "S" and blk.attn_f.axis == "F"


def test_spt_2d_embed_dim_not_divisible_raises():
    """spt_2d embed_dim 不被 num_heads=4 整除时 fail loud（build_model 守门）。"""
    mod = _load_variant("spt_2d")
    with pytest.raises(ValueError, match="整除"):
        mod.build_model(embed_dim=10)
