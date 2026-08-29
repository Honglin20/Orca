"""code-reviewer 要求的 delta 微测试：_ZeroBlock 形状 / mip_select floor 缺失 raise /
build_pretrained_model 宽松加载 / U6 select_reason enum 收缩。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import torch
import torch.nn as nn

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "workflows" / "puzzle" / "agents" / "_puzzle_scripts"
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


# ── U6：build_pretrained_model（替代 load_father_model missing-ratio gate）────

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


def test_build_pretrained_model_accepts_full_load(tmp_path):
    """adapters.load_pretrained 双零 missing/unexpected → from_scratch=False。

    U6：不再做 missing-ratio raise（root cause C：宽松）；from_scratch 标志记录到 baseline。
    """
    import importlib.util
    import textwrap
    flat = _write_flat_with_build(tmp_path)
    sys.path.insert(0, str(tmp_path))
    spec = importlib.util.spec_from_file_location("_f_load", flat)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    sd = mod.build_model().state_dict()
    father = tmp_path / "father.pt"
    torch.save(sd, father)

    adapters_py = tmp_path / "puzzle_adapters.py"
    adapters_py.write_text(textwrap.dedent(f"""
        import importlib.util, torch, torch.nn.functional as F
        from torch.utils.data import DataLoader, TensorDataset
        from collections import namedtuple
        _LoadResult = namedtuple("_LoadResult", ["missing", "unexpected", "from_scratch"])
        _spec = importlib.util.spec_from_file_location("_f", r"{flat}")
        _flat = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_flat)
        build_model = _flat.build_model; DUMMY_INPUT = _flat.DUMMY_INPUT
        FORWARD_CALLING_CONVENTION = "single"
        METRIC_DIRECTION = "higher-better"
        EVAL_NOISE_ATOL = 1e-6
        def forward_model(m, b):
            x = b[0] if isinstance(b, (tuple, list)) else b
            return m(x)
        def calib_iter(device=None):
            x = torch.randn(4, 4); return iter(DataLoader(TensorDataset(x), batch_size=2))
        def train_iter(device=None):
            x = torch.randn(8, 4); y = torch.randint(0, 10, (8,))
            return iter(DataLoader(TensorDataset(x, y), batch_size=4))
        def extract_labels(b):
            return b[1] if isinstance(b, (tuple, list)) and len(b) >= 2 else None
        def kd_loss(s, t, labels=None):
            s = s[0] if isinstance(s, (tuple, list)) else s
            t = t[0] if isinstance(t, (tuple, list)) else t
            return F.kl_div(F.log_softmax(s, -1), F.softmax(t, -1), reduction="batchmean")
        def task_loss(s, l):
            return None if l is None else F.cross_entropy(s, l)
        def evaluate(m):
            m.eval()
            with torch.no_grad():
                return float(m(torch.randn(*DUMMY_INPUT["shape"])).abs().mean().item())
        def load_pretrained(m):
            ck = torch.load(r"{father}", map_location="cpu", weights_only=False)
            mi, un = m.load_state_dict(ck, strict=False)
            return _LoadResult(list(mi), list(un), len(mi) > 0.5 * len(m.state_dict()))
    """), encoding="utf-8")
    adapters = pc.load_puzzle_adapters(adapters_py)
    out = pc.build_pretrained_model(adapters)
    assert isinstance(out, nn.Module)
    assert not out.training  # eval


# ── mip_select floor-missing raise ──────────────────────────────────────────
def test_mip_raises_when_no_floor_and_no_baseline():
    import mip_select as ms
    scores = [{"layer": 0, "kind": "attention", "variant": "identity", "score": 0.0, "valid": True}]
    latency = [{"layer": 0, "kind": "attention", "variant": "identity", "latency_ms": 0.05}]
    with pytest.raises(ValueError, match="latency_floor"):
        ms._solve_mip(scores, latency, target_latency=0.4,
                      baseline_whole_latency=None, measured_floor=None)


# ── U5 #4：no_op 非方 slot 收缩 ──────────────────────────────────────────────
def _slot(**kw):
    defaults = dict(
        layer_idx=0, kind="attention", in_dim=32, out_dim=32, num_heads=0, head_dim=0,
        source_class="Attn", parent_module_path="block.attn",
    )
    defaults.update(kw)
    return pc.Slot(**defaults)


def test_is_candidate_valid_for_slot_rejects_no_op_on_non_square_slot():
    assert pc.is_candidate_valid_for_slot("no_op", _slot(in_dim=32, out_dim=32)) is True
    assert pc.is_candidate_valid_for_slot("no_op", _slot(in_dim=32, out_dim=24)) is False
    assert pc.is_candidate_valid_for_slot("identity", _slot(in_dim=32, out_dim=24)) is True
    assert pc.is_candidate_valid_for_slot(
        "no_op", _slot(kind="ffn", ffn_struct="standard", in_dim=32, out_dim=24)
    ) is False


# ── U6 #G：mip_select select_reason enum 收缩 + 新增 reduction 参数 ──────────

def test_mip_select_cli_has_latency_reduction_target():
    """U6：mip_select 加 --latency_reduction_target（默认 0.5）+ --target-latency 改可选。"""
    import mip_select as ms
    parser = ms._build_argparser()
    actions = {a.dest: a for a in parser._actions}
    assert "latency_reduction_target" in actions
    assert actions["latency_reduction_target"].default == 0.5
    # target_latency 不再 required（root cause G：可由 reduction 推导）
    assert actions["target_latency"].required is False


def test_mip_select_no_longer_emits_target_too_aggressive():
    """U6：select_reason enum 收缩到 mip-optimal/infeasible/none。

    旧路径 ``target-too-aggressive`` 早警 + ``infeasible_reason`` 字段已退役
    （root cause G）；puzzle.yaml schema 的同步更新是 Wave 2 范围。
    本测试扫描代码字符串字面量（非 docstring）。
    """
    import ast
    import importlib
    ms = importlib.import_module("mip_select")
    src_path = Path(ms.__file__)
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    string_lits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            string_lits.add(node.value)
    # 代码字符串字面量中不应有 target-too-aggressive / infeasible_reason（
    # docstring 是 ast.Expr->Constant，也会进 string_lits——但 docstring 文本不含
    # 这两个精确值；精确值只在旧代码的 return dict 里出现）
    assert "target-too-aggressive" not in string_lits, (
        "U6 应删 target-too-aggressive 字符串字面量（root cause G）"
    )
    assert "infeasible_reason" not in string_lits


def test_gate_report_cli_has_latency_reduction_target():
    """U6：gate_report 加 --latency_reduction_target（默认 0.5），判 ratio ≤ (1 - reduction)。"""
    import gate_report as gr
    parser = gr._build_argparser()
    actions = {a.dest: a for a in parser._actions}
    assert "latency_reduction_target" in actions
    assert actions["latency_reduction_target"].default == 0.5
