"""code-reviewer 要求的 delta 微测试:_ZeroBlock 形状 / load_father_model missing 比例阈值 /
mip_select floor 缺失 raise。与 test_puzzle_father_state.py 互补。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest
import torch
import torch.nn as nn

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "workflows" / "agents" / "_puzzle_scripts"
sys.path.insert(0, str(SCRIPTS))

import puzzle_common as pc  # noqa: E402
import puzzle_blocks as pb  # noqa: E402


# ── _ZeroBlock ───────────────────────────────────────────────────────────────
def test_zero_block_preserves_shape_dtype_device_and_is_zero():
    blk = pb._ZeroBlock()
    for shape in [(2, 5, 8), (1, 16), (3, 4, 7, 9)]:
        x = torch.randn(*shape, dtype=torch.float32)
        y = blk(x)
        assert y.shape == x.shape
        assert y.dtype == x.dtype
        assert y.device == x.device
        assert torch.count_nonzero(y) == 0


def test_factory_no_op_requires_equal_io_dim():
    class _S:
        in_dim = 8
        out_dim = 8
        parent_module_path = "x"
    assert isinstance(pb.make_zero(_S()), pb._ZeroBlock)

    class _S2:
        in_dim = 8
        out_dim = 16
        parent_module_path = "x"
    with pytest.raises(ValueError):
        pb.make_zero(_S2())


# ── load_father_model missing-ratio gate ────────────────────────────────────
def _write_flat_with_build(tmp: Path) -> Path:
    flat = tmp / "flat_for_test.py"
    flat.write_text(
        """import torch.nn as nn
DUMMY_INPUT = {'shape': [1, 4], 'dtype': 'float32'}
class M(nn.Module):
    def __init__(self):
        super().__init__()
        self.a = nn.Linear(4, 4)
        self.b = nn.Linear(4, 4)
        self.c = nn.Linear(4, 4)
    def forward(self, x):
        return self.c(self.b(self.a(x)))
def build_model():
    return M()
"""
    )
    return flat


def test_load_father_model_raises_on_large_missing(tmp_path):
    flat = _write_flat_with_build(tmp_path)
    # 只给 a 的权重(3 个 Linear 中 1 个 → ~67% missing)> 20% → raise
    sd = {"a.weight": torch.zeros(4, 4), "a.bias": torch.zeros(4)}
    ckpt = tmp_path / "father.pt"
    torch.save(sd, ckpt)
    with pytest.raises(RuntimeError, match="严重不齐"):
        pc.load_father_model(str(flat), "build_model", "", str(ckpt))


def test_load_father_model_accepts_small_missing(tmp_path):
    flat = _write_flat_with_build(tmp_path)
    m = pc.load_flat_model(str(flat), "build_model", "")
    sd = m.state_dict()  # 全量 → 0 missing,接受
    ckpt = tmp_path / "father.pt"
    torch.save(sd, ckpt)
    out = pc.load_father_model(str(flat), "build_model", "", str(ckpt))
    assert isinstance(out, nn.Module)
    assert not out.training  # eval


def test_load_father_model_raises_on_missing_file(tmp_path):
    flat = _write_flat_with_build(tmp_path)
    with pytest.raises(FileNotFoundError):
        pc.load_father_model(str(flat), "build_model", "", str(tmp_path / "nope.pt"))


# ── mip_select floor-missing raise ──────────────────────────────────────────
def test_mip_raises_when_no_floor_and_no_baseline():
    import mip_select as ms
    scores = [{"layer": 0, "kind": "attention", "variant": "identity", "score": 0.0, "valid": True}]
    latency = [{"layer": 0, "kind": "attention", "variant": "identity", "latency_ms": 0.05}]
    with pytest.raises(ValueError, match="latency_floor"):
        ms._solve_mip(scores, latency, target_latency=0.4,
                      baseline_whole_latency=None, measured_floor=None)
