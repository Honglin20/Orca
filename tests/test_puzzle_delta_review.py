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


# ── U5 #4：no_op 非方 slot 收缩（is_candidate_valid_for_slot）─────────────────
def _slot(**kw):
    defaults = dict(
        layer_idx=0, kind="attention", in_dim=32, out_dim=32, num_heads=0, head_dim=0,
        source_class="Attn", parent_module_path="block.attn",
    )
    defaults.update(kw)
    return pc.Slot(**defaults)


def test_is_candidate_valid_for_slot_rejects_no_op_on_non_square_slot():
    """no_op（零输出块）要求 in_dim == out_dim（make_zero 契约）。

    非方 slot 的 no_op 被 is_candidate_valid_for_slot 判 invalid——收缩候选而非
    在 factory 期 raise 崩整链（intent：候选枚举处统一过滤，BLD/score/build_selected
    不再触 factory 的 square-dims guard）。
    """
    # 方 slot：no_op valid（与 identity 同列 MIP floor 候选）
    assert pc.is_candidate_valid_for_slot("no_op", _slot(in_dim=32, out_dim=32)) is True
    # 非方 slot：no_op 被 valid 拒
    assert pc.is_candidate_valid_for_slot("no_op", _slot(in_dim=32, out_dim=24)) is False
    # 非方 slot 上 identity 仍 valid（passthrough 铁律，与 dims 无关）
    assert pc.is_candidate_valid_for_slot("identity", _slot(in_dim=32, out_dim=24)) is True
    # 非方 ffn slot 的 no_op 同样被拦（square 检查在 cross-kind 之前，对所有 kind 生效）
    assert pc.is_candidate_valid_for_slot(
        "no_op", _slot(kind="ffn", ffn_struct="standard", in_dim=32, out_dim=24)
    ) is False


# ── U5 #1 BLOCKER 回归守卫：puzzle.yaml pz_select schema × mip_select.py emit 契约 ─
def test_puzzle_yaml_pz_select_schema_covers_mip_select_early_warning():
    """pz_select output_schema 必须覆盖 mip_select.py 所有 emit 路径的字段集。

    U5 #1 BLOCKER 的两层契约（任一破坏 → schema 校验 fail → node_failed catch-all，
    E12 早警 intent 被 silently 击落）：
      (a) select_reason enum ⊇ {target-too-aggressive}（原 BLOCKER：enum 漂移）。
      (b) properties ⊇ {infeasible_reason}（残留 BLOCKER：早警路径 emit 的诊断字段，
          additionalProperties:false 会拒——第一轮修复只加 enum 未加 property，被
          code-reviewer 复审抓出）。本测试锁这两层，防同类漂移回归。
    """
    import yaml
    wf = yaml.safe_load((REPO / "workflows" / "puzzle.yaml").read_text(encoding="utf-8"))
    pz_select = next(n for n in wf["nodes"] if n["name"] == "pz_select")
    props = pz_select["output_schema"]["properties"]
    # (a) enum 覆盖所有 script emit 的 select_reason 值
    enum = props["select_reason"]["enum"]
    emitted_reasons = {"mip-optimal", "infeasible", "none", "target-too-aggressive"}
    assert emitted_reasons.issubset(set(enum)), \
        f"select_reason enum 缺脚本 emit 值：{emitted_reasons - set(enum)}"
    # (b) 早警路径（target-too-aggressive）emit 的字段集 ⊆ schema properties
    #     （mip_select.py 早警块独立编写，曾漏 infeasible_reason → additionalProperties:false 拒）
    early_warning_keys = {
        "selected_arch", "total_score", "selected_latency", "feasible",
        "select_reason", "latency_unit", "infeasible_reason",
    }
    missing = early_warning_keys - set(props.keys())
    assert not missing, f"早警 emit 字段未在 schema properties：{missing}"
    # additionalProperties:false 强约束——required ⊆ properties 是必要前提
    assert set(pz_select["output_schema"]["required"]).issubset(set(props.keys()))
    assert pz_select["output_schema"]["additionalProperties"] is False
