"""test_puzzle_u3_migration.py —— Phase U6 下游脚本迁移 + 算法增强测试。

U6 改造：脚本走 ``--adapters`` + ``--flat_model`` + ``--build_fn``，不再接
``--calib_loader_fn`` / ``--train_loader_fn`` / ``--eval_fn`` / ``--eval_kind`` /
``--father_ckpt``（双零语义）。仍保留的闭环 AC：
  - E6：is_valid_ffn_prune + is_candidate_valid_for_slot——bypass/GLU/dual FFN 拒
    ffn_75/ffn_50/linear。
  - E8：mask_load_bearing slot 拒绝 mask-blind candidate（只留 identity + masked_vanilla）。
  - §16.4：全 identity selected_arch → student forward 与 father allclose（端到端）。
  - root cause G：mip_select 不再 target-too-aggressive 早警（删该分支）。
  - root cause D：gkd_retrain 走 adapters.kd_loss + adapters.task_loss（删 is_classification）。
  - root cause I：gate_report 方向感知（lower-better metric 用 final ≤ baseline×(1+tol)）。
  - root cause E：latency_table 非方 slot floor 不 raise（用原块实测兜底）。

重点（Rule 9：验证 intent 非 behavior）：每个测试构造「违规」输入，断言 raise / 拒绝 / fail loud。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("torch")
pytest.importorskip("yaml")
pytest.importorskip("nas_agent")

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _REPO / "workflows" / "agents" / "_puzzle_scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_REPO / "tests"))


def _import(name: str):
    import importlib
    return importlib.import_module(name)


# ── E6 + E8：is_valid_*（函数级）─────────────────────────────────────────────

def _slot(**kw) -> "object":
    pc = _import("puzzle_common")
    defaults = dict(
        layer_idx=0, kind="ffn", in_dim=32, out_dim=32, num_heads=0, head_dim=0,
        source_class="FFN", parent_module_path="block.ffn",
    )
    defaults.update(kw)
    return pc.Slot(**defaults)


def test_is_valid_ffn_prune_rejects_non_standard() -> None:
    pc = _import("puzzle_common")
    assert pc.is_valid_ffn_prune(_slot(ffn_struct="standard")) is True
    assert pc.is_valid_ffn_prune(_slot(ffn_struct="bypass")) is False
    assert pc.is_valid_ffn_prune(_slot(ffn_struct="glu")) is False


def test_is_candidate_valid_for_slot_e6_filter() -> None:
    pc = _import("puzzle_common")
    bypass_slot = _slot(ffn_struct="bypass")
    standard_slot = _slot(ffn_struct="standard")
    for v in ("ffn_75", "ffn_50", "linear"):
        assert pc.is_candidate_valid_for_slot(v, standard_slot) is True, v
    for v in ("ffn_75", "ffn_50", "linear"):
        assert pc.is_candidate_valid_for_slot(v, bypass_slot) is False, v
    assert pc.is_candidate_valid_for_slot("identity", bypass_slot) is True
    assert pc.is_candidate_valid_for_slot("no_op", bypass_slot) is True


def test_is_candidate_valid_for_slot_e8_mask() -> None:
    """E8：mask_load_bearing slot 拒绝 mask-blind candidate；保留 identity + masked_vanilla。"""
    pc = _import("puzzle_common")
    mask_slot = _slot(kind="attention", mask_load_bearing=True)
    plain_slot = _slot(kind="attention", mask_load_bearing=False)

    # mask slot：mask-blind builtin（fnet / vanilla / random_synthesizer）全拒
    for v in ("fnet", "random_synthesizer", "vanilla", "ffn_75"):
        assert pc.is_candidate_valid_for_slot(v, mask_slot) is False, v
    # identity + masked_vanilla（mask_aware builtin）仍 valid（root cause F：mask-bearing
    # slot 至少能选 mask_aware 候选 + identity）
    assert pc.is_candidate_valid_for_slot("identity", mask_slot) is True
    assert pc.is_candidate_valid_for_slot("masked_vanilla", mask_slot) is True
    # 普通 slot 不拒 mask-blind
    assert pc.is_candidate_valid_for_slot("fnet", plain_slot) is True


def test_is_candidate_valid_for_slot_rejects_cross_kind() -> None:
    pc = _import("puzzle_common")
    ffn_slot = _slot(kind="ffn", ffn_struct="standard")
    attn_slot = _slot(kind="attention")
    assert pc.is_candidate_valid_for_slot("fnet", ffn_slot) is False
    assert pc.is_candidate_valid_for_slot("vanilla", ffn_slot) is False
    assert pc.is_candidate_valid_for_slot("ffn_50", attn_slot) is False
    assert pc.is_candidate_valid_for_slot("identity", ffn_slot) is True


# ── §16.4：build_selected 全 identity allclose（端到端）─────────────────────────

_TINY_FLAT_FOR_ALLCLOSE = textwrap.dedent("""
    import torch, torch.nn as nn
    DUMMY_INPUT = {"shape": [2, 4, 8], "dtype": "float32"}

    class FFN(nn.Module):
        def __init__(self, dim):
            super().__init__()
            self.fc1 = nn.Linear(dim, dim * 2)
            self.act = nn.GELU()
            self.fc2 = nn.Linear(dim * 2, dim)
        def forward(self, x):
            return self.fc2(self.act(self.fc1(x)))

    class TinyModel(nn.Module):
        def __init__(self, dim=8):
            super().__init__()
            self.embed = nn.Linear(8, dim)
            self.ffn = FFN(dim)
        def forward(self, x):
            return self.ffn(self.embed(x))

    def build_model(dim=8):
        return TinyModel(dim)
""")


def _write_allclose_adapters(tmp_path: Path, flat_path: Path, father_ckpt: Path) -> Path:
    """写最小 adapters（单 forward / calib / evaluate / load_pretrained）。"""
    adapters = tmp_path / "puzzle_adapters.py"
    adapters.write_text(textwrap.dedent(f"""
        import importlib.util, torch, torch.nn.functional as F
        from torch.utils.data import DataLoader, TensorDataset
        from collections import namedtuple
        _LoadResult = namedtuple("_LoadResult", ["missing", "unexpected", "from_scratch"])
        _spec = importlib.util.spec_from_file_location("_f", r"{flat_path}")
        _flat = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_flat)
        build_model = _flat.build_model; DUMMY_INPUT = _flat.DUMMY_INPUT
        FORWARD_CALLING_CONVENTION = "single"
        METRIC_DIRECTION = "higher-better"
        EVAL_NOISE_ATOL = 1e-6
        def forward_model(m, b):
            x = b[0] if isinstance(b, (tuple, list)) else b
            return m(x)
        def calib_iter(device=None):
            x = torch.randn(4, 4, 8); return iter(DataLoader(TensorDataset(x), batch_size=2))
        def train_iter(device=None):
            x = torch.randn(8, 4, 8); y = torch.randint(0, 10, (8,))
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
            ck = torch.load(r"{father_ckpt}", map_location="cpu", weights_only=False)
            mi, un = m.load_state_dict(ck, strict=False)
            return _LoadResult(list(mi), list(un), len(mi) > 0.5 * len(m.state_dict()))
    """), encoding="utf-8")
    return adapters


@pytest.mark.slow
def test_build_selected_all_identity_allclose_vs_father(tmp_path: Path) -> None:
    """§16.4：全 identity selected_arch → student forward 必须与 father allclose。"""
    import torch
    pc = _import("puzzle_common")

    flat = tmp_path / "flat.py"
    flat.write_text(_TINY_FLAT_FOR_ALLCLOSE, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    spec = importlib.util.spec_from_file_location("_tiny_flat_ac", flat)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    father_model = mod.build_model()
    with torch.no_grad():
        father_model.embed.weight.fill_(2.5)
    father_state = tmp_path / "father_state.pt"
    torch.save(father_model.state_dict(), father_state)

    adapters = _write_allclose_adapters(tmp_path, flat, father_state)

    slot = pc.Slot(
        layer_idx=0, kind="ffn", in_dim=8, out_dim=8, num_heads=0, head_dim=0,
        source_class="FFN", parent_module_path="ffn",
        original_intermediate=16, activation="gelu", ffn_struct="standard",
    )
    block_map = pc.BlockMap(slots=[slot])
    block_map_path = tmp_path / "block_map.json"
    block_map.to_json(block_map_path)
    block_lib = tmp_path / "block_library"; block_lib.mkdir()

    selected_arch_path = tmp_path / "selected_arch.json"
    selected_arch_path.write_text(json.dumps({
        "selected_arch": {"0": {"ffn": "identity"}},
    }), encoding="utf-8")
    out_dir = tmp_path / "out"; out_dir.mkdir()
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "build_selected.py"),
         "--selected_arch", str(selected_arch_path),
         "--block_map", str(block_map_path),
         "--flat_model", str(flat),
         "--build_fn", "build_model",
         "--adapters", str(adapters),
         "--block_library", str(block_lib),
         "--output_dir", str(out_dir)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"全 identity build_selected 应 rc=0（allclose 应通过）\nSTDERR:\n{proc.stderr}\nSTDOUT:\n{proc.stdout}"
    )
    assert (out_dir / "selected_model.pt").is_file()


@pytest.mark.slow
def test_build_selected_non_all_identity_skips_allclose(tmp_path: Path) -> None:
    """§16.4 反例：非全 identity（有意替换）→ allclose 不适用，build 正常完成。"""
    import torch
    pc = _import("puzzle_common")
    flat = tmp_path / "flat.py"
    flat.write_text(_TINY_FLAT_FOR_ALLCLOSE, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    spec = importlib.util.spec_from_file_location("_tiny_flat_ac2", flat)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    father_state = tmp_path / "father_state.pt"
    torch.save(mod.build_model().state_dict(), father_state)
    adapters = _write_allclose_adapters(tmp_path, flat, father_state)

    slot = pc.Slot(
        layer_idx=0, kind="ffn", in_dim=8, out_dim=8, num_heads=0, head_dim=0,
        source_class="FFN", parent_module_path="ffn",
        original_intermediate=16, activation="gelu", ffn_struct="standard",
    )
    block_map = pc.BlockMap(slots=[slot])
    block_map_path = tmp_path / "block_map.json"
    block_map.to_json(block_map_path)
    block_lib = tmp_path / "block_library"; block_lib.mkdir()
    from puzzle_common import variant_file_name
    entry = pc.get_candidate("ffn_50")
    variant_module = entry.factory(slot)
    ckpt_path = block_lib / variant_file_name(0, "ffn", "ffn_50")
    torch.save({"state_dict": variant_module.state_dict(), "variant": "ffn_50"}, ckpt_path)

    selected_arch_path = tmp_path / "selected_arch.json"
    selected_arch_path.write_text(json.dumps({
        "selected_arch": {"0": {"ffn": "ffn_50"}},
    }), encoding="utf-8")
    out_dir = tmp_path / "out"; out_dir.mkdir()
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "build_selected.py"),
         "--selected_arch", str(selected_arch_path),
         "--block_map", str(block_map_path),
         "--flat_model", str(flat),
         "--build_fn", "build_model",
         "--adapters", str(adapters),
         "--block_library", str(block_lib),
         "--output_dir", str(out_dir)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"非全 identity 应跳过 allclose\nSTDERR:\n{proc.stderr}"


# ── E6 端到端：bld.py 拒 bypass FFN 的剪枝候选 ───────────────────────────────

def test_bld_e6_filter_bypass_ffn_rejects_prune(tmp_path: Path) -> None:
    """E6 端到端：bypass FFN slot 经 is_valid 过滤后候选收缩到 {identity, no_op}。"""
    import torch
    pc = _import("puzzle_common")
    flat = tmp_path / "flat.py"
    flat.write_text(_TINY_FLAT_FOR_ALLCLOSE, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    spec = importlib.util.spec_from_file_location("_tiny_flat_e6", flat)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    father_state = tmp_path / "father_state.pt"
    torch.save(mod.build_model().state_dict(), father_state)
    adapters = _write_allclose_adapters(tmp_path, flat, father_state)

    slot = pc.Slot(
        layer_idx=0, kind="ffn", in_dim=8, out_dim=8, num_heads=0, head_dim=0,
        source_class="FFN", parent_module_path="ffn",
        original_intermediate=16, activation="gelu", ffn_struct="bypass",
    )
    block_map = pc.BlockMap(slots=[slot])
    block_map_path = tmp_path / "block_map.json"
    block_map.to_json(block_map_path)

    out_dir = tmp_path / "out"; out_dir.mkdir()
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "bld.py"),
         "--block_map", str(block_map_path),
         "--flat_model", str(flat),
         "--build_fn", "build_model",
         "--adapters", str(adapters),
         "--block_candidates",
         json.dumps({"ffn": ["identity", "ffn_50", "no_op"]}),
         "--epochs", "1",
         "--output_dir", str(out_dir)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"bypass FFN 经 is_valid 过滤后应正常跑\nSTDERR:\n{proc.stderr}"
    )
    block_library_dir = out_dir / "block_library"
    variants = sorted(p.stem.split("_", 2)[2] for p in block_library_dir.glob("*.pt"))
    assert "ffn_50" not in variants, f"ffn_50 应被 is_valid 过滤：{variants}"
    assert "identity" in variants
    assert "no_op" in variants


# ── root cause I：gate_report 方向感知 ────────────────────────────────────────

def test_gate_metric_direction_higher_better_thresholds() -> None:
    """L12（design §0 L12，闭环 LV-1 BLOCKER）：高 baseline max(abs-0.5, rel-1%)；低 baseline 10% 比例保护。"""
    gate = _import("gate_report")
    # 高 baseline：max(0.97-0.5, 0.97×0.99) = max(0.47, 0.9603) = 0.9603（mnist）
    threshold, kind = gate._acc_threshold_higher_better(0.97)
    assert kind == "l12-strict"
    assert threshold == pytest.approx(0.9603, abs=1e-6)
    assert gate._acc_pass_higher_better(0.97, 0.9603)[0] is True
    assert gate._acc_pass_higher_better(0.97, 0.9602)[0] is False
    # 低 baseline：0.085 × 0.9 = 0.0765（近随机，比例保护）
    threshold_low, kind_low = gate._acc_threshold_higher_better(0.085)
    assert kind_low == "proportional"
    assert threshold_low == pytest.approx(0.0765, abs=1e-6)


def test_gate_acc_threshold_l12_target_and_boundary_cases() -> None:
    """L12 AC 公式：target/边界全覆盖（单一真相源 = 验收 AC）。

    验证 LV-1 BLOCKER 修复——v2 D5 绝对容差让 target 0.9919 pass 阈值仅 0.4919，
    改 L12 后必须 0.9819（等价精度损失 <1%）。
    """
    gate = _import("gate_report")
    # target 0.9919 → max(0.4919, 0.9919×0.99=0.981981) = 0.981981（≈0.9819，等价 <1%）
    t, kind = gate._acc_threshold_higher_better(0.9919)
    assert kind == "l12-strict"
    assert t == pytest.approx(0.981981, abs=1e-6)
    # 0.99 → max(0.49, 0.9801) = 0.9801（相对分支取严）
    t, _ = gate._acc_threshold_higher_better(0.99)
    assert t == pytest.approx(0.9801, abs=1e-6)
    # 0.6 → max(0.1, 0.594) = 0.594（相对分支取严）
    t, _ = gate._acc_threshold_higher_better(0.6)
    assert t == pytest.approx(0.594, abs=1e-6)
    # 理论上限 1.0 → max(0.5, 0.99) = 0.99（相对分支取严）
    t, _ = gate._acc_threshold_higher_better(1.0)
    assert t == pytest.approx(0.99, abs=1e-6)
    # 边界 0.5：max(0, 0.495) = 0.495（相对分支取严；≥0.5 入高 baseline 路径）。
    # 注意 0.5 处是有意 regime-switch：0.5→0.495（1% 跌幅）vs 0.499→0.4491（10% 跌幅），
    # 阈值不连续是 L12 高/低 baseline 双策略的设计语义，非 bug。
    t, k = gate._acc_threshold_higher_better(0.5)
    assert k == "l12-strict"
    assert t == pytest.approx(0.495, abs=1e-6)
    # 边界 0.499：<0.5 走比例保护 → 0.499×0.9 = 0.4491
    t, k = gate._acc_threshold_higher_better(0.499)
    assert k == "proportional"
    assert t == pytest.approx(0.4491, abs=1e-6)


def test_gate_acc_threshold_default_relative_dominates_for_accuracy() -> None:
    """默认 _ACC_ABS_TOL=0.5 时，对 accuracy ∈ [0.5, 1.0] 相对支（×0.99）恒取严。

    数学：baseline−0.5 > baseline×0.99 ⟺ baseline > 50（accuracy 域外）→ 绝对支永不取严。
    这是 design L12 max() 在 accuracy 域的「防御性 floor」语义——绝对支只在
    --accuracy_tolerance CLI override 调小时激活（见 override 测试）。
    钉死该不变式，防未来误改 _ACC_ABS_TOL 期望生效却不报警。
    """
    gate = _import("gate_report")
    assert gate._ACC_ABS_TOL == 0.5  # 防御性 floor 默认值，被 max() 包住
    for baseline in (0.5, 0.6, 0.7, 0.8, 0.9, 0.97, 0.99, 0.9919, 1.0):
        threshold, kind = gate._acc_threshold_higher_better(baseline)
        assert kind == "l12-strict"
        # 相对支取严 = 阈值 == baseline × 0.99（不是 baseline − 0.5）
        assert threshold == pytest.approx(baseline * 0.99, abs=1e-6), (
            f"baseline={baseline} 应走相对支（×0.99），实际 threshold={threshold}"
        )


def test_gate_acc_threshold_accuracy_tolerance_override_tightens(monkeypatch) -> None:
    """--accuracy_tolerance CLI override 调小时绝对支激活收紧 gate（证明 CLI 非 no-op）。

    main() 经 ``global _ACC_ABS_TOL`` 注入 CLI 值（gate_report.py:113-114）。默认 0.5
    时绝对支恒被相对支取严；调小到 ``--accuracy_tolerance 0`` 时 max(0.97, 0.9603)=0.97，
    要求 final 精确等于 baseline——收紧 gate。
    """
    gate = _import("gate_report")
    # 默认：baseline=0.97 → max(0.47, 0.9603) = 0.9603（相对支取严）
    assert gate._acc_threshold_higher_better(0.97)[0] == pytest.approx(0.9603)
    # 模拟 --accuracy_tolerance 0：max(0.97, 0.9603) = 0.97（绝对支取严，收紧）
    monkeypatch.setattr(gate, "_ACC_ABS_TOL", 0.0)
    t, kind = gate._acc_threshold_higher_better(0.97)
    assert kind == "l12-strict"
    assert t == pytest.approx(0.97, abs=1e-6)
    # 收紧后 final=0.965（默认 0.9603 阈值下 pass）现在 fail
    assert gate._acc_pass_higher_better(0.97, 0.965)[0] is False


def test_acc_pass_higher_better_returns_full_tuple_contract() -> None:
    """_acc_pass_higher_better 返回 (bool, kind, threshold)；threshold==threshold 函数返回值。"""
    gate = _import("gate_report")
    passed, kind, thr = gate._acc_pass_higher_better(0.97, 0.965)
    assert passed is True
    assert kind == "l12-strict"
    expected_thr, _ = gate._acc_threshold_higher_better(0.97)
    assert thr == pytest.approx(expected_thr, abs=1e-6)


def test_gate_metric_direction_lower_better_pass() -> None:
    """lower-better：final ≤ baseline × (1 + tol) → pass。"""
    gate = _import("gate_report")
    # baseline loss=1.0, final=1.05, tol=0.1 → 1.05 ≤ 1.1 → pass
    assert gate._lower_better_pass(1.0, 1.05, rel_tol=0.1) is True
    # final=1.2 > 1.1 → fail
    assert gate._lower_better_pass(1.0, 1.2, rel_tol=0.1) is False
    # baseline=0.5, final=0.4（lower）, tol=0.1 → 0.4 ≤ 0.55 → pass
    assert gate._lower_better_pass(0.5, 0.4, rel_tol=0.1) is True


# ── root cause G：mip_select 不再 target-too-aggressive（已在 smoke 测覆；此为函数级补强）──

def test_mip_select_reason_enum_no_target_too_aggressive() -> None:
    """select_reason enum 收缩到 mip-optimal/infeasible/none（不再 target-too-aggressive）。"""
    mip = _import("mip_select")
    # 检 _solve_mip 的所有返回路径都不会产出 target-too-aggressive
    scores_rows = [{"layer": 0, "kind": "attention", "variant": "identity",
                    "score": 0.0, "valid": True}]
    latency_rows = [{"layer": 0, "kind": "attention", "variant": "identity",
                     "latency_ms": 1.0}]
    result = mip._solve_mip(scores_rows, latency_rows, target_latency=10.0,
                            baseline_whole_latency=100.0, measured_floor=0.0)
    assert result["select_reason"] in {"mip-optimal", "infeasible", "none"}
    assert result["select_reason"] != "target-too-aggressive"


# ── root cause E：latency_table 非方 slot floor 不 raise ─────────────────────

def test_latency_table_non_square_slot_floor_does_not_raise(tmp_path: Path) -> None:
    """latency_table floor 循环：非方 slot（in_dim != out_dim）不 raise，用原块实测兜底。

    构造：单一 ffn slot，in_dim=8, out_dim=8（方）；再改 in_dim=8, out_dim=4（非方）
    重跑——旧逻辑 make_zero 对非方 slot raise；新逻辑保留原块。验证 floor 路径不崩。
    """
    import torch
    import torch.nn as nn
    pc = _import("puzzle_common")
    # 自定义 flat：FFN in_dim=8 → out_dim=4（非方 slot）
    nonsq_flat = tmp_path / "nonsq_flat.py"
    nonsq_flat.write_text(textwrap.dedent("""
        import torch, torch.nn as nn
        DUMMY_INPUT = {"shape": [2, 4, 8], "dtype": "float32"}
        class FFN(nn.Module):
            def __init__(self):
                super().__init__()
                self.fc1 = nn.Linear(8, 16); self.act = nn.GELU(); self.fc2 = nn.Linear(16, 4)
            def forward(self, x): return self.fc2(self.act(self.fc1(x)))
        class M(nn.Module):
            def __init__(self):
                super().__init__()
                self.fc = nn.Linear(8, 8); self.ffn = FFN()
            def forward(self, x): return self.ffn(self.fc(x))
        def build_model(): return M()
    """), encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    spec = importlib.util.spec_from_file_location("_nonsq", nonsq_flat)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    father_state = tmp_path / "father.pt"
    torch.save(mod.build_model().state_dict(), father_state)
    adapters = _write_allclose_adapters(tmp_path, nonsq_flat, father_state)

    # 非方 slot: in_dim=8, out_dim=4
    slot = pc.Slot(
        layer_idx=0, kind="ffn", in_dim=8, out_dim=4, num_heads=0, head_dim=0,
        source_class="FFN", parent_module_path="ffn",
        original_intermediate=16, activation="gelu", ffn_struct="standard",
    )
    block_map = pc.BlockMap(slots=[slot])
    block_map_path = tmp_path / "block_map.json"
    block_map.to_json(block_map_path)
    block_lib = tmp_path / "block_library"; block_lib.mkdir()
    # identity passthrough ckpt
    torch.save({"state_dict": {}, "variant": "identity", "passthrough": True},
               block_lib / "L0_ffn_identity.pt")
    # no_op ckpt 不会被 is_valid 选（in_dim != out_dim）→ BLD/score/latency 不进

    out_dir = tmp_path / "out"; out_dir.mkdir()
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "latency_table.py"),
         "--block_map", str(block_map_path),
         "--flat_model", str(nonsq_flat),
         "--build_fn", "build_model",
         "--adapters", str(adapters),
         "--block_library", str(block_lib),
         "--output_dir", str(out_dir)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"非方 slot floor 不应 raise（root cause E：用原块实测兜底）\nSTDERR:\n{proc.stderr}"
    )
    floor = json.loads((out_dir / "latency_floor.json").read_text())
    # 非方 slot 被记入 kept_original_slots（未替换为 zero）
    assert "kept_original_slots" in floor


# ── root cause D：gkd_retrain 走 adapters.kd_loss + task_loss ─────────────────

def test_gkd_retrain_runs_with_adapter_losses(tmp_path: Path) -> None:
    """gkd_retrain 走 adapters.kd_loss + adapters.task_loss（删 is_classification / 写死 CE）。

    构造合成 fixture，跑 gkd 1 epoch；断言 progress.jsonl 含 ``kd`` 字段、可选 ``task``。
    """
    from _puzzle_test_fixtures import write_flat_and_adapters, search_space_payload
    import yaml
    output_dir = tmp_path / "out"; output_dir.mkdir(parents=True, exist_ok=True)
    paths = write_flat_and_adapters(tmp_path)
    # 跑 measure_baseline → bld → build_selected → gkd（简化链：全 identity arch）
    ss_path = tmp_path / "ss.yaml"
    with open(ss_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(search_space_payload(num_blocks=1), f, allow_unicode=True, sort_keys=False)
    proc = subprocess.run([sys.executable, str(_SCRIPTS_DIR / "measure_baseline.py"),
        "--flat_path", str(paths["flat"]), "--build_fn", "build_model",
        "--adapters", str(paths["adapters"]),
        "--search_space_path", str(ss_path), "--output_dir", str(output_dir),
        "--latency_reduction_target", "0"],
        capture_output=True, text=True)
    assert proc.returncode == 0, f"measure_baseline 失败：\n{proc.stderr}"
    block_map_path = output_dir / "block_map.json"
    block_lib = output_dir / "block_library"; block_lib.mkdir()
    proc = subprocess.run([sys.executable, str(_SCRIPTS_DIR / "bld.py"),
        "--block_map", str(block_map_path), "--flat_model", str(paths["flat"]),
        "--build_fn", "build_model", "--adapters", str(paths["adapters"]),
        "--block_candidates", json.dumps({"attention": ["identity"], "ffn": ["identity"]}),
        "--epochs", "1", "--output_dir", str(output_dir)],
        capture_output=True, text=True)
    assert proc.returncode == 0, f"bld 失败：\n{proc.stderr}"
    selected_arch = output_dir / "selected_arch.json"
    # 构造 selected_arch（全 identity）
    bm = json.loads(block_map_path.read_text())
    arch = {}
    for s in bm["slots"]:
        arch.setdefault(str(s["layer_idx"]), {})[s["kind"]] = "identity"
    selected_arch.write_text(json.dumps({"selected_arch": arch}))
    proc = subprocess.run([sys.executable, str(_SCRIPTS_DIR / "build_selected.py"),
        "--selected_arch", str(selected_arch), "--block_map", str(block_map_path),
        "--flat_model", str(paths["flat"]), "--build_fn", "build_model",
        "--adapters", str(paths["adapters"]), "--block_library", str(block_lib),
        "--output_dir", str(output_dir)],
        capture_output=True, text=True)
    assert proc.returncode == 0, f"build_selected 失败：\n{proc.stderr}"
    selected_model = output_dir / "selected_model.pt"

    # materialize 产 optimized_flat（gkd 的 student 执行基底）
    proc = subprocess.run([sys.executable, str(_SCRIPTS_DIR / "materialize_optimized.py"),
        "--flat_model", str(paths["flat"]), "--build_fn", "build_model", "--build_cfg", "",
        "--selected_arch", str(selected_arch), "--block_map", str(block_map_path),
        "--selected_model", str(selected_model), "--adapters", str(paths["adapters"]),
        "--block_library", str(block_lib), "--output_dir", str(output_dir), "--base_name", "tiny"],
        capture_output=True, text=True)
    assert proc.returncode == 0, f"materialize 失败：\n{proc.stderr}"
    optimized_flat = output_dir / "tiny_optimized_flat.py"

    proc = subprocess.run([sys.executable, str(_SCRIPTS_DIR / "gkd_retrain.py"),
        "--selected_model", str(selected_model),
        "--optimized_flat", str(optimized_flat),
        "--adapters", str(paths["adapters"]),
        "--epochs", "1", "--output_dir", str(output_dir)],
        capture_output=True, text=True)
    assert proc.returncode == 0, f"gkd_retrain 失败：\n{proc.stderr}"
    progress = (output_dir / "runs" / "retrain" / "progress.jsonl").read_text().splitlines()
    assert len(progress) >= 1
    row = json.loads(progress[0])
    assert "kd" in row["metrics"]  # kd_loss 字段（root cause D：不再写死 CE 分支）


# ── root cause D：gkd 不再有 _flatten_model_output / is_classification（API 退役）──

def test_gkd_retrain_no_longer_has_flatten_model_output() -> None:
    """U6：``_flatten_model_output`` 已删（forward 形态由 adapters 消化）。"""
    gkd = _import("gkd_retrain")
    assert not hasattr(gkd, "_flatten_model_output"), (
        "U6 应删 _flatten_model_output（root cause A/K：forward 形态由 adapters 消化）"
    )


def test_measure_baseline_no_longer_has_strict_load_double_zero() -> None:
    """U6 root cause C：measure_baseline 不再有 strict_load_father（双零硬门）。"""
    mb = _import("measure_baseline")
    assert not hasattr(mb, "strict_load_father"), (
        "U6 应删 strict_load_father（root cause C：宽松 ckpt 经 adapters.load_pretrained）"
    )


def test_puzzle_common_no_longer_has_build_real_calib_loader() -> None:
    """U6 root cause A：build_real_calib_loader 已删（calib 经 adapters.calib_iter）。"""
    pc = _import("puzzle_common")
    assert not hasattr(pc, "build_real_calib_loader"), (
        "U6 应删 build_real_calib_loader（root cause A：calib 经 adapters.calib_iter）"
    )
    assert not hasattr(pc, "load_father_model"), (
        "U6 应删 load_father_model（father 经 adapters.build_model + load_pretrained）"
    )
    assert not hasattr(pc, "resolve_eval_fn"), (
        "U6 应删 resolve_eval_fn（evaluate 经 adapters.evaluate）"
    )


# ── E6 is_valid 收缩：build_selected 拒选无效 variant（防御性）──────────────────

def test_build_selected_rejects_invalid_arch_for_slot(tmp_path: Path) -> None:
    import torch
    pc = _import("puzzle_common")
    flat = tmp_path / "flat.py"
    flat.write_text(_TINY_FLAT_FOR_ALLCLOSE, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    spec = importlib.util.spec_from_file_location("_tiny_flat_invalid", flat)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    father_state = tmp_path / "father_state.pt"
    torch.save(mod.build_model().state_dict(), father_state)
    adapters = _write_allclose_adapters(tmp_path, flat, father_state)

    slot = pc.Slot(
        layer_idx=0, kind="ffn", in_dim=8, out_dim=8, num_heads=0, head_dim=0,
        source_class="FFN", parent_module_path="ffn",
        original_intermediate=16, activation="gelu", ffn_struct="bypass",
    )
    block_map = pc.BlockMap(slots=[slot])
    block_map_path = tmp_path / "block_map.json"
    block_map.to_json(block_map_path)
    block_lib = tmp_path / "block_library"; block_lib.mkdir()
    from puzzle_common import variant_file_name
    entry = pc.get_candidate("ffn_50")
    variant_module = entry.factory(slot)
    torch.save({"state_dict": variant_module.state_dict(), "variant": "ffn_50"},
               block_lib / variant_file_name(0, "ffn", "ffn_50"))

    selected_arch_path = tmp_path / "selected_arch.json"
    selected_arch_path.write_text(json.dumps({
        "selected_arch": {"0": {"ffn": "ffn_50"}},  # bypass slot 选 ffn_50 无效
    }), encoding="utf-8")
    out_dir = tmp_path / "out"; out_dir.mkdir()
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "build_selected.py"),
         "--selected_arch", str(selected_arch_path),
         "--block_map", str(block_map_path),
         "--flat_model", str(flat),
         "--build_fn", "build_model",
         "--adapters", str(adapters),
         "--block_library", str(block_lib),
         "--output_dir", str(out_dir)],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0, (
        "build_selected 应拒掉 bypass slot 选 ffn_50（fail loud）"
    )
    assert "结构无效" in proc.stderr or "is_valid" in proc.stderr.lower()
