"""test_puzzle_father_state.py —— Puzzle father 权重贯穿契约的单元测试。

锁定 ``puzzle_common.load_father_model`` + ``_extract_state_dict`` +
``build_student_from_arch(father_state_path=...)`` 的 intent:

  - ``_extract_state_dict``: wrapper ``{state_dict: ...}`` 解包 / 裸 state_dict 直通 / 非 dict 直通。
  - ``load_father_model``: 空 father_state → WARN + 随机 init 回退;文件缺 → raise;
    正常路径 → 权重真载入 + ``.eval()``。
  - ``build_student_from_arch(father_state_path=...)``: identity（passthrough）slot
    在 father_state 提供时保留预训练父权重（而非随机 init）—— Puzzle delta 的核心契约。

不接真 fixture——合成最小 nn.Module + 临时 state_dict 文件,torch CPU 可用。
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("torch")
pytest.importorskip("nas_agent")

_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "agents"
    / "_puzzle_scripts"
)


def _import_puzzle_common():
    """sibling import 契约:把 _puzzle_scripts 加进 sys.path 再 import。

    与 puzzle 脚本自身的 sibling import 方式一致（puzzle_common.py 顶部注释允许）,
    测试复用同一路径解析。
    """
    here = str(_SCRIPTS_DIR)
    if here not in sys.path:
        sys.path.insert(0, here)
    import importlib

    mod = importlib.import_module("puzzle_common")
    return mod


_TINY_MODEL_PY = textwrap.dedent(
    """
    import torch
    import torch.nn as nn

    DUMMY_INPUT = {"shape": [2, 4, 8], "dtype": "float32"}


    class SimpleAttention(nn.Module):
        def __init__(self, dim: int):
            super().__init__()
            self.proj = nn.Linear(dim, dim)

        def forward(self, x):
            return self.proj(x)


    # 结构形如 Linear-Act-Linear（命中 _is_ffn_module 的结构识别分支）
    class FeedForward(nn.Module):
        def __init__(self, dim: int):
            super().__init__()
            self.fc1 = nn.Linear(dim, dim * 2)
            self.act = nn.GELU()
            self.fc2 = nn.Linear(dim * 2, dim)

        def forward(self, x):
            return self.fc2(self.act(self.fc1(x)))


    class TinyBlock(nn.Module):
        def __init__(self, dim: int):
            super().__init__()
            self.attn = SimpleAttention(dim)
            self.ffn = FeedForward(dim)

        def forward(self, x):
            return x + self.attn(x) + self.ffn(x)


    class TinyTransformer(nn.Module):
        def __init__(self, dim=8):
            super().__init__()
            self.embed = nn.Linear(8, dim)
            self.block = TinyBlock(dim)

        def forward(self, x):
            return self.block(self.embed(x))


    def build_model(dim=8):
        return TinyTransformer(dim)
    """
)


def _write_tiny_flat_model(tmp_path: Path) -> Path:
    """把合成 flat model 写到 tmp_path/flat_model.py 并返回该路径。"""
    p = tmp_path / "flat_model.py"
    p.write_text(_TINY_MODEL_PY, encoding="utf-8")
    return p


# ── _extract_state_dict ──────────────────────────────────────────────────────


def test_extract_state_dict_unwraps_wrapper(tmp_path):
    pc = _import_puzzle_common()
    import torch

    inner = {"layer.weight": torch.tensor([1.0, 2.0])}
    wrapper = {"state_dict": inner, "epoch": 5, "metric": "acc"}
    # wrapper 顶层无 blocks./patch_embed. 键 → 应解包
    out = pc._extract_state_dict(wrapper)
    assert out is inner, "wrapper 形态应返回内层 state_dict"


def test_extract_state_dict_passthrough_plain_state_dict():
    """裸 state_dict（顶层键是模型层名,即便含 'state_dict' 子键也不解包）。"""
    pc = _import_puzzle_common()
    import torch

    plain = {
        "blocks.0.attn.proj.weight": torch.zeros(2, 2),
        "state_dict": torch.zeros(1),  # 故意的同名键,但因 blocks. 出现顶层,不解包
    }
    out = pc._extract_state_dict(plain)
    assert out is plain, "顶层含 blocks./patch_embed. 的裸 state_dict 应原样返回"


def test_extract_state_dict_passthrough_non_dict():
    pc = _import_puzzle_common()
    # 非 dict（如裸 list / tensor）→ 原样返回,不试图解包
    for non_dict in ([1, 2, 3], "not a dict", 42):
        out = pc._extract_state_dict(non_dict)
        assert out is non_dict


# ── load_father_model ────────────────────────────────────────────────────────


def test_load_father_model_missing_file_raises(tmp_path):
    pc = _import_puzzle_common()
    flat = _write_tiny_flat_model(tmp_path)
    missing = tmp_path / "does_not_exist.pt"
    with pytest.raises(FileNotFoundError, match="father_state 文件不存在"):
        pc.load_father_model(str(flat), "build_model", "", str(missing))


def test_load_father_model_empty_warns_and_returns_random(tmp_path, capsys):
    """空 father_state → 回退随机 init + stderr WARN（向后兼容契约）。"""
    pc = _import_puzzle_common()
    flat = _write_tiny_flat_model(tmp_path)
    # 空串
    m1 = pc.load_father_model(str(flat), "build_model", "", "")
    assert m1.training is False, "load_father_model 应 eval()"
    err = capsys.readouterr().err
    assert "father_state_path 空" in err and "WARN" in err
    # None 等价
    m2 = pc.load_father_model(str(flat), "build_model", "", None)
    assert m2.training is False


def test_load_father_model_loads_pretrained_weights(tmp_path, capsys):
    """father_state 非空文件 → 权重真载入（用 sentinel 值验证）。"""
    pc = _import_puzzle_common()
    import torch

    flat = _write_tiny_flat_model(tmp_path)

    # 1) 构造一个 father ckpt:用 build 出的 model 填入 sentinel 值后保存
    base_model = pc.load_flat_model(str(flat), "build_model", "")
    sentinel_val = 3.1415
    with torch.no_grad():
        base_model.embed.weight.fill_(sentinel_val)
    father_ckpt = tmp_path / "father.pt"
    torch.save(base_model.state_dict(), father_ckpt)

    # 2) load_father_model 载入后 embed.weight 应为 sentinel
    capsys.readouterr()  # 清空之前可能累积的 stderr
    loaded = pc.load_father_model(str(flat), "build_model", "", str(father_ckpt))
    assert torch.allclose(
        loaded.embed.weight,
        torch.full_like(loaded.embed.weight, sentinel_val),
    ), "father 权重未真载入（embed.weight 应为 sentinel 值）"
    assert loaded.training is False
    # missing/unexpected keys 应为空（同源 schema）
    err = capsys.readouterr().err
    assert "missing keys" not in err and "unexpected keys" not in err


def test_load_father_model_unwraps_wrapper_format(tmp_path):
    """father ckpt 形如 {state_dict: {...}, ...} 的 wrapper → 正确解包载入。"""
    pc = _import_puzzle_common()
    import torch

    flat = _write_tiny_flat_model(tmp_path)
    base_model = pc.load_flat_model(str(flat), "build_model", "")
    sentinel_val = 2.718
    with torch.no_grad():
        base_model.embed.weight.fill_(sentinel_val)
    wrapper_ckpt = tmp_path / "father_wrapper.pt"
    torch.save(
        {"state_dict": base_model.state_dict(), "epoch": 10, "foo": "bar"},
        wrapper_ckpt,
    )

    loaded = pc.load_father_model(str(flat), "build_model", "", str(wrapper_ckpt))
    assert torch.allclose(
        loaded.embed.weight,
        torch.full_like(loaded.embed.weight, sentinel_val),
    ), "wrapper 形态 father ckpt 应解包后载入"


# ── build_student_from_arch identity slot 保留 father 权重（核心 intent）─────


def test_build_student_from_arch_identity_retains_father_weights(tmp_path):
    """delta 核心契约:identity（passthrough）slot 在 father_state 提供时
    保留预训练父权重,而非随机 init。

    构造:selected_arch 全 identity → build_student 后,被 passthrough 的 attention
    /ffn 模块的权重应等于 father_state 的对应权重。
    """
    pc = _import_puzzle_common()
    import torch

    flat = _write_tiny_flat_model(tmp_path)

    # 1) 准备 father_state:填入 sentinel
    father_model = pc.load_flat_model(str(flat), "build_model", "")
    sentinel_val = 1.234
    with torch.no_grad():
        father_model.block.attn.proj.weight.fill_(sentinel_val)
        father_model.block.ffn.fc1.weight.fill_(sentinel_val)
    father_state_path = tmp_path / "father_state.pt"
    torch.save(father_model.state_dict(), father_state_path)

    # 2) 构造 block_map:单层单 attention + 单 ffn(用 Slot dataclass)
    slot_att = pc.Slot(
        layer_idx=0,
        slot_type="attention",
        in_dim=8,
        out_dim=8,
        num_heads=4,
        head_dim=2,
        source_class="SimpleAttention",
        parent_module_path="block.attn",
    )
    slot_ffn = pc.Slot(
        layer_idx=0,
        slot_type="ffn",
        in_dim=8,
        out_dim=8,
        num_heads=0,
        head_dim=0,
        source_class="FeedForward",
        parent_module_path="block.ffn",
    )
    block_map = pc.BlockMap(slots=[slot_att, slot_ffn])

    # 3) identity-only selected_arch（passthrough,puzzle 候选集的 identity）
    selected_arch = {"selected_arch": {"0": {"attention": "identity", "ffn": "identity"}}}
    block_library_dir = tmp_path / "block_library"
    block_library_dir.mkdir()

    # 4) 不传 father_state_path → identity slot 的权重是随机 init（sentinel 不存在）
    student_random = pc.build_student_from_arch(
        flat_model_path=str(flat),
        build_fn="build_model",
        build_cfg="",
        block_map=block_map,
        selected_arch=selected_arch,
        block_library_dir=block_library_dir,
        device=torch.device("cpu"),
        father_state_path=None,
    )
    # identity passthrough → attn 模块仍是 flat model 原生对象（随机 init）
    assert not torch.allclose(
        student_random.block.attn.proj.weight,
        torch.full_like(student_random.block.attn.proj.weight, sentinel_val),
    ), "father_state_path=None 时 identity slot 应为随机 init（非 father 权重）"

    # 5) 传 father_state_path → identity slot 保留 father 权重（核心契约）
    student_father = pc.build_student_from_arch(
        flat_model_path=str(flat),
        build_fn="build_model",
        build_cfg="",
        block_map=block_map,
        selected_arch=selected_arch,
        block_library_dir=block_library_dir,
        device=torch.device("cpu"),
        father_state_path=str(father_state_path),
    )
    assert torch.allclose(
        student_father.block.attn.proj.weight,
        torch.full_like(student_father.block.attn.proj.weight, sentinel_val),
    ), "father_state_path 提供时 identity（attention）slot 必须保留 father 权重"
    assert torch.allclose(
        student_father.block.ffn.fc1.weight,
        torch.full_like(student_father.block.ffn.fc1.weight, sentinel_val),
    ), "father_state_path 提供时 identity（ffn）slot 必须保留 father 权重"
