"""test_puzzle_u3_migration.py —— Phase U3 下游脚本迁移 + 算法增强测试。

锁定 SPEC v2 §8/§12/§15/§17 的闭环 AC：
  - E14（隐性 BLOCKER）：BLD calib 必须真实数据——build_real_calib_loader 抽首 batch
    物化；空 loader / 非 tensor / 不可迭代 → fail loud。
  - E6：is_valid_ffn_prune + is_candidate_valid_for_slot——bypass/GLU/dual FFN 拒
    ffn_75/ffn_50/linear，收缩到 {identity, no_op}。
  - E8：mask_load_bearing slot 拒绝 mask-blind candidate（只留 identity）。
  - §16.4：全 identity selected_arch → student forward 与 father allclose。
  - D5：gate_report ACC AC baseline-dependent（高 baseline 绝对 0.5、低 baseline 相对 10%）。
  - E12：mip_select LAT 早警——target_latency > baseline/2 → infeasible + 明确 reason。
  - E24：gkd_retrain dict/list 输出 → fail loud（flat.py 漏 output-flattening adapter）。

重点（Rule 9：验证 intent 非 behavior）：每个测试都构造一个会被测组件判为"违规"的
输入，断言 raise / 拒绝 / fail loud——而非只测 happy path。
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("torch")
pytest.importorskip("yaml")
pytest.importorskip("nas_agent")

_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "agents"
    / "_puzzle_scripts"
)
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _import(name: str):
    """sibling-import a puzzle script module（与脚本自身 sibling import 方式一致）。"""
    import importlib
    return importlib.import_module(name)


# ── E14：build_real_calib_loader ──────────────────────────────────────────────

def test_build_real_calib_loader_materializes_first_batch(tmp_path: Path) -> None:
    """E14：调外部 loader_fn → 抽首 batch → 物化为可重复迭代的 DataLoader。"""
    import torch
    from torch.utils.data import DataLoader, TensorDataset
    pc = _import("puzzle_common")

    # 写一个外部 loader 文件
    loader_py = tmp_path / "loader.py"
    loader_py.write_text(textwrap.dedent("""
        import torch
        from torch.utils.data import DataLoader, TensorDataset

        def build():
            x = torch.arange(2 * 4 * 4, dtype=torch.float32).reshape(2, 4, 4)
            return DataLoader(TensorDataset(x), batch_size=2)
    """), encoding="utf-8")

    loader = pc.build_real_calib_loader(f"{loader_py}::build", device=None)
    batches = list(loader)
    assert len(batches) == 1, "物化首 batch 后应只 1 个 batch"
    inp = batches[0]
    assert inp.shape == (2, 4, 4)
    # 值来自真实 loader（非 randn）——首元素 0、末元素 31
    assert inp.flatten()[0].item() == 0.0
    assert inp.flatten()[-1].item() == 31.0


def test_build_real_calib_loader_fail_loud_paths(tmp_path: Path) -> None:
    """E14：空 loader / 非 tensor batch / 非可迭代 → raise（禁静默回退 randn）。"""
    pc = _import("puzzle_common")

    # 空 DataLoader
    empty_py = tmp_path / "empty.py"
    empty_py.write_text(textwrap.dedent("""
        import torch
        from torch.utils.data import DataLoader, TensorDataset
        def build():
            return DataLoader(TensorDataset(torch.empty(0, 4)), batch_size=1)
    """), encoding="utf-8")
    with pytest.raises(RuntimeError, match="空 DataLoader"):
        pc.build_real_calib_loader(f"{empty_py}::build")

    # 非可迭代返回
    noniter_py = tmp_path / "noniter.py"
    noniter_py.write_text("def build():\n    return 42\n", encoding="utf-8")
    with pytest.raises(TypeError, match="未返回可迭代"):
        pc.build_real_calib_loader(f"{noniter_py}::build")


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
    """E6：bypass/GLU/dual FFN 拒剪枝（ffn_struct != 'standard' → False）。"""
    pc = _import("puzzle_common")
    assert pc.is_valid_ffn_prune(_slot(ffn_struct="standard")) is True
    assert pc.is_valid_ffn_prune(_slot(ffn_struct="bypass")) is False
    assert pc.is_valid_ffn_prune(_slot(ffn_struct="glu")) is False
    assert pc.is_valid_ffn_prune(_slot(ffn_struct="dual")) is False


def test_is_candidate_valid_for_slot_e6_filter() -> None:
    """E6：bypass FFN slot 的 ffn_75/ffn_50/linear 被 catalog × slot 联合校验拒。

    identity / no_op 仍 valid（候选收缩到 {identity, no_op}）。
    """
    pc = _import("puzzle_common")
    bypass_slot = _slot(ffn_struct="bypass")
    standard_slot = _slot(ffn_struct="standard")

    # standard FFN：剪枝候选全 valid
    for v in ("ffn_75", "ffn_50", "linear"):
        assert pc.is_candidate_valid_for_slot(v, standard_slot) is True, v
    # bypass FFN：剪枝候选被拒（catalog requires_ffn_struct=[standard]）
    for v in ("ffn_75", "ffn_50", "linear"):
        assert pc.is_candidate_valid_for_slot(v, bypass_slot) is False, v
    # identity（passthrough）+ no_op 仍 valid
    assert pc.is_candidate_valid_for_slot("identity", bypass_slot) is True
    assert pc.is_candidate_valid_for_slot("no_op", bypass_slot) is True


def test_is_candidate_valid_for_slot_e8_mask() -> None:
    """E8：mask_load_bearing slot 拒绝 mask-blind candidate（builtin 默认 mask_aware=False）。"""
    pc = _import("puzzle_common")
    mask_slot = _slot(kind="attention", mask_load_bearing=True)
    plain_slot = _slot(kind="attention", mask_load_bearing=False)

    # mask slot：mask-blind builtin（fnet / random_synthesizer / vanilla）全拒
    for v in ("fnet", "random_synthesizer", "vanilla", "ffn_75"):
        assert pc.is_candidate_valid_for_slot(v, mask_slot) is False, v
    # identity 永远 valid（passthrough 铁律）
    assert pc.is_candidate_valid_for_slot("identity", mask_slot) is True
    # 普通 slot 不拒 mask-blind
    assert pc.is_candidate_valid_for_slot("fnet", plain_slot) is True


def test_is_candidate_valid_for_slot_rejects_cross_kind() -> None:
    """catalog × slot 跨 kind 适用性：attention 候选不适用 ffn slot，反之亦然。

    本测试是 single-source-of-trust 校验器的关键 intent——``is_candidate_valid_for_slot``
    把 ``slot.kind not in entry.kinds`` 也判 False，使 build_selected 的防御性关卡
    真能拦住 kind 误配（不只 E6/E8 结构 + mask）。
    """
    pc = _import("puzzle_common")
    ffn_slot = _slot(kind="ffn", ffn_struct="standard")
    attn_slot = _slot(kind="attention")

    # attention 候选（fnet / vanilla）× ffn slot → False（kind 不匹配）
    assert pc.is_candidate_valid_for_slot("fnet", ffn_slot) is False
    assert pc.is_candidate_valid_for_slot("vanilla", ffn_slot) is False
    # ffn 候选（ffn_50 / linear）× attention slot → False
    assert pc.is_candidate_valid_for_slot("ffn_50", attn_slot) is False
    assert pc.is_candidate_valid_for_slot("linear", attn_slot) is False
    # identity 适用所有 kind（passthrough 铁律）
    assert pc.is_candidate_valid_for_slot("identity", ffn_slot) is True
    assert pc.is_candidate_valid_for_slot("identity", attn_slot) is True


# ── §16.4：build_selected 全 identity allclose（端到端）─────────────────────────

_TINY_FLAT_FOR_ALLCLOSE = textwrap.dedent("""
    import torch
    import torch.nn as nn

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


@pytest.mark.slow
def test_build_selected_all_identity_allclose_vs_father(tmp_path: Path) -> None:
    """§16.4：全 identity selected_arch → student forward 必须与 father allclose。

    语义：identity 候选承诺零侵入——所有 slot 都选 identity 时，student 实际就是
    father 本身，forward 必须逐元素 allclose（非 per-slot 近似，是跨模型整模验证）。
    """
    import torch
    pc = _import("puzzle_common")

    flat = tmp_path / "flat.py"
    flat.write_text(_TINY_FLAT_FOR_ALLCLOSE, encoding="utf-8")
    father_model = pc.load_flat_model(str(flat), "build_model", "")
    # 填入 sentinel 权重以验证 father_state 真注入
    with torch.no_grad():
        father_model.embed.weight.fill_(2.5)
    father_state = tmp_path / "father_state.pt"
    torch.save(father_model.state_dict(), father_state)

    slot = pc.Slot(
        layer_idx=0, kind="ffn", in_dim=8, out_dim=8, num_heads=0, head_dim=0,
        source_class="FFN", parent_module_path="ffn",
        original_intermediate=16, activation="gelu", ffn_struct="standard",
    )
    block_map = pc.BlockMap(slots=[slot])
    block_map_path = tmp_path / "block_map.json"
    block_map.to_json(block_map_path)
    block_lib = tmp_path / "block_library"
    block_lib.mkdir()

    # 全 identity selected_arch
    selected_arch_path = tmp_path / "selected_arch.json"
    selected_arch_path.write_text(json.dumps({
        "selected_arch": {"0": {"ffn": "identity"}},
    }), encoding="utf-8")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "build_selected.py"),
         "--selected_arch", str(selected_arch_path),
         "--block_map", str(block_map_path),
         "--flat_model", str(flat),
         "--build_fn", "build_model",
         "--block_library", str(block_lib),
         "--father_state", str(father_state),
         "--output_dir", str(out_dir)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"全 identity build_selected 应 rc=0（allclose 应通过）\n"
        f"STDERR:\n{proc.stderr}\nSTDOUT:\n{proc.stdout}"
    )
    # selected_model.pt 落盘
    assert (out_dir / "selected_model.pt").is_file()


@pytest.mark.slow
def test_build_selected_non_all_identity_skips_allclose(tmp_path: Path) -> None:
    """§16.4 反例：非全 identity（有意替换）→ allclose 不适用，build 正常完成。"""
    import torch
    pc = _import("puzzle_common")

    flat = tmp_path / "flat.py"
    flat.write_text(_TINY_FLAT_FOR_ALLCLOSE, encoding="utf-8")
    father = pc.load_flat_model(str(flat), "build_model", "")
    father_state = tmp_path / "father_state.pt"
    torch.save(father.state_dict(), father_state)

    slot = pc.Slot(
        layer_idx=0, kind="ffn", in_dim=8, out_dim=8, num_heads=0, head_dim=0,
        source_class="FFN", parent_module_path="ffn",
        original_intermediate=16, activation="gelu", ffn_struct="standard",
    )
    block_map = pc.BlockMap(slots=[slot])
    block_map_path = tmp_path / "block_map.json"
    block_map.to_json(block_map_path)

    # 给 ffn_50 蒸一个 ckpt（满足 build_selected 的载入）
    block_lib = tmp_path / "block_library"
    block_lib.mkdir()
    from puzzle_common import variant_file_name
    entry = pc.get_candidate("ffn_50")
    variant_module = entry.factory(slot)
    ckpt_path = block_lib / variant_file_name(0, "ffn", "ffn_50")
    torch.save({"state_dict": variant_module.state_dict(), "variant": "ffn_50"},
               ckpt_path)

    # 非全 identity（ffn_50）
    selected_arch_path = tmp_path / "selected_arch.json"
    selected_arch_path.write_text(json.dumps({
        "selected_arch": {"0": {"ffn": "ffn_50"}},
    }), encoding="utf-8")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "build_selected.py"),
         "--selected_arch", str(selected_arch_path),
         "--block_map", str(block_map_path),
         "--flat_model", str(flat),
         "--build_fn", "build_model",
         "--block_library", str(block_lib),
         "--father_state", str(father_state),
         "--output_dir", str(out_dir)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"非全 identity（有意替换）应跳过 allclose，正常 build\n"
        f"STDERR:\n{proc.stderr}"
    )


# ── E6 端到端：bld.py 拒 bypass FFN 的剪枝候选 ───────────────────────────────

def test_bld_e6_filter_bypass_ffn_rejects_prune(tmp_path: Path) -> None:
    """E6 端到端：bypass FFN slot 经 is_valid 过滤后候选收缩到 {identity, no_op}，
    ffn_50 不进 BLD（block_library 不含 ffn_50 ckpt）。
    """
    import torch
    pc = _import("puzzle_common")

    flat = tmp_path / "flat.py"
    flat.write_text(_TINY_FLAT_FOR_ALLCLOSE, encoding="utf-8")
    father_state = tmp_path / "father_state.pt"
    torch.save(pc.load_flat_model(str(flat), "build_model", "").state_dict(), father_state)

    # bypass FFN slot
    slot = pc.Slot(
        layer_idx=0, kind="ffn", in_dim=8, out_dim=8, num_heads=0, head_dim=0,
        source_class="FFN", parent_module_path="ffn",
        original_intermediate=16, activation="gelu", ffn_struct="bypass",
    )
    block_map = pc.BlockMap(slots=[slot])
    block_map_path = tmp_path / "block_map.json"
    block_map.to_json(block_map_path)

    # 写 calib loader
    loader_py = tmp_path / "loader.py"
    loader_py.write_text(textwrap.dedent("""
        import torch
        from torch.utils.data import DataLoader, TensorDataset
        def build():
            return DataLoader(TensorDataset(torch.randn(2, 4, 8)), batch_size=2)
    """), encoding="utf-8")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # candidates 含 ffn_50——但 is_valid 应过滤
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "bld.py"),
         "--block_map", str(block_map_path),
         "--flat_model", str(flat),
         "--build_fn", "build_model",
         "--block_candidates",
         json.dumps({"ffn": ["identity", "ffn_50", "no_op"]}),
         "--calib_loader_fn", f"{loader_py}::build",
         "--father_state", str(father_state),
         "--epochs", "1",
         "--output_dir", str(out_dir)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"bypass FFN 经 is_valid 过滤后应正常跑（ffn_50 被拒，留 identity+no_op）\n"
        f"STDERR:\n{proc.stderr}"
    )
    block_library_dir = out_dir / "block_library"
    variants = sorted(p.stem.split("_", 2)[2] for p in block_library_dir.glob("*.pt"))
    assert "ffn_50" not in variants, f"ffn_50 应被 is_valid 过滤不进 BLD：{variants}"
    assert "identity" in variants, f"identity 应保留：{variants}"
    assert "no_op" in variants, f"no_op 应保留：{variants}"


# ── D5：gate_report baseline-dependent ACC AC（函数级）──────────────────────────

def test_d5_acc_threshold_high_baseline_uses_absolute() -> None:
    """D5：高 baseline（mnist 0.97）→ 绝对容差 0.5 → threshold=0.47。"""
    gate = _import("gate_report")
    threshold, kind = gate._acc_threshold(0.97)
    assert kind == "absolute"
    assert threshold == pytest.approx(0.47, abs=1e-6)
    # final=0.47 pass、final=0.46 fail
    assert gate._acc_pass(0.97, 0.47)[0] is True
    assert gate._acc_pass(0.97, 0.469)[0] is False


def test_d5_acc_threshold_low_baseline_uses_relative() -> None:
    """D5：低 baseline（target 0.085）→ 相对容差 10% → threshold=0.0765。"""
    gate = _import("gate_report")
    threshold, kind = gate._acc_threshold(0.085)
    assert kind == "relative"
    assert threshold == pytest.approx(0.0765, abs=1e-6)
    # 近随机 0.001 fail（用户 §16.9 AC）
    passed, _, _ = gate._acc_pass(0.085, 0.001)
    assert passed is False, "近随机 0.001 应 fail（D5 比例保护）"
    # final 略高于 threshold → pass（避免浮点边界 0.0765 == 0.0765000001 的歧义）
    assert gate._acc_pass(0.085, 0.077)[0] is True


def test_d5_boundary_acc_base_half() -> None:
    """D5 边界：acc_base == 0.5 走绝对（threshold=0.0）；acc_base == 0.499 走相对。"""
    gate = _import("gate_report")
    t_high, kind_high = gate._acc_threshold(0.5)
    assert kind_high == "absolute" and t_high == pytest.approx(0.0)
    t_low, kind_low = gate._acc_threshold(0.499)
    assert kind_low == "relative"
    assert t_low == pytest.approx(0.499 * 0.9, abs=1e-6)


# ── E12：mip_select LAT 早警 ──────────────────────────────────────────────────

def test_e12_mip_select_target_too_aggressive(tmp_path: Path) -> None:
    """E12：target_latency > baseline_latency/2 → infeasible + select_reason
    target-too-aggressive + infeasible_reason（不浪费 build_selected/retrain 算力）。
    """
    scores_path = tmp_path / "scores.jsonl"
    latency_path = tmp_path / "latency_table.jsonl"
    with open(scores_path, "w") as f:
        f.write(json.dumps({"layer": 0, "kind": "attention", "variant": "identity",
                            "score": 0.0, "valid": True}) + "\n")
    with open(latency_path, "w") as f:
        f.write(json.dumps({"layer": 0, "kind": "attention", "variant": "identity",
                            "latency_ms": 1.0}) + "\n")
    baseline_metrics = tmp_path / "baseline_metrics.json"
    baseline_metrics.write_text(json.dumps({"baseline_latency": 10.0}))

    # target = 6.0 > baseline/2 = 5.0 → 早警
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "mip_select.py"),
         "--scores", str(scores_path),
         "--latency-table", str(latency_path),
         "--target-latency", "6.0",
         "--baseline-metrics", str(baseline_metrics),
         "--output_dir", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"早警是合法 rc=0 分支\nSTDERR:\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["feasible"] is False
    assert result["select_reason"] == "target-too-aggressive"
    assert "E12" in result["infeasible_reason"]
    assert "baseline_latency/2" in result["infeasible_reason"]


def test_e12_mip_select_target_ok_proceeds_to_mip(tmp_path: Path) -> None:
    """E12 反例：target_latency ≤ baseline/2 → 不触发早警，正常跑 MIP。"""
    scores_path = tmp_path / "scores.jsonl"
    latency_path = tmp_path / "latency_table.jsonl"
    with open(scores_path, "w") as f:
        f.write(json.dumps({"layer": 0, "kind": "attention", "variant": "identity",
                            "score": 0.0, "valid": True}) + "\n")
    with open(latency_path, "w") as f:
        f.write(json.dumps({"layer": 0, "kind": "attention", "variant": "identity",
                            "latency_ms": 1.0}) + "\n")
    baseline_metrics = tmp_path / "baseline_metrics.json"
    baseline_metrics.write_text(json.dumps({"baseline_latency": 10.0}))

    # target = 4.0 ≤ baseline/2 = 5.0 → 正常跑
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "mip_select.py"),
         "--scores", str(scores_path),
         "--latency-table", str(latency_path),
         "--target-latency", "4.0",
         "--baseline-metrics", str(baseline_metrics),
         "--output_dir", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["select_reason"] != "target-too-aggressive"
    # 不该有 infeasible_reason 字段（仅早警分支有）
    assert "infeasible_reason" not in result


# ── E24：gkd_retrain dict/list 输出 fail loud ──────────────────────────────────

def test_e24_flatten_model_output_rejects_dict() -> None:
    """E24：dict 输出 → raise（flat.py 须加 output-flattening adapter）。"""
    import torch
    gkd = _import("gkd_retrain")
    with pytest.raises(RuntimeError, match="dict"):
        gkd._flatten_model_output({"logits": torch.zeros(2, 3)})


def test_e24_flatten_model_output_handles_tuple_and_tensor() -> None:
    """E24：tensor 直通；tuple/list 取首 tensor。"""
    import torch
    gkd = _import("gkd_retrain")
    t = torch.zeros(2, 3)
    assert gkd._flatten_model_output(t) is t
    out = gkd._flatten_model_output((t, torch.ones(2, 3)))
    assert torch.equal(out, t)
    # 空 tuple → raise
    with pytest.raises(RuntimeError, match="空"):
        gkd._flatten_model_output(())
    # tuple 首元素非 tensor → raise
    with pytest.raises(RuntimeError, match="非 tensor"):
        gkd._flatten_model_output(("not_a_tensor",))


# ── E6 is_valid 收缩：build_selected 拒选无效 variant（防御性）──────────────────

def test_build_selected_rejects_invalid_arch_for_slot(tmp_path: Path) -> None:
    """build_selected 防御性 is_valid：selected_arch 选了 bypass slot 的 ffn_50 → raise。"""
    import torch
    pc = _import("puzzle_common")

    flat = tmp_path / "flat.py"
    flat.write_text(_TINY_FLAT_FOR_ALLCLOSE, encoding="utf-8")
    father_state = tmp_path / "father_state.pt"
    torch.save(pc.load_flat_model(str(flat), "build_model", "").state_dict(), father_state)

    # bypass FFN slot（ffn_50 不适用）
    slot = pc.Slot(
        layer_idx=0, kind="ffn", in_dim=8, out_dim=8, num_heads=0, head_dim=0,
        source_class="FFN", parent_module_path="ffn",
        original_intermediate=16, activation="gelu", ffn_struct="bypass",
    )
    block_map = pc.BlockMap(slots=[slot])
    block_map_path = tmp_path / "block_map.json"
    block_map.to_json(block_map_path)

    block_lib = tmp_path / "block_library"
    block_lib.mkdir()
    # 塞一个 ffn_50 ckpt（假装 score.py 误判 valid=True 让 MIP 选了它）
    from puzzle_common import variant_file_name
    entry = pc.get_candidate("ffn_50")
    variant_module = entry.factory(slot)
    torch.save(
        {"state_dict": variant_module.state_dict(), "variant": "ffn_50"},
        block_lib / variant_file_name(0, "ffn", "ffn_50"),
    )

    selected_arch_path = tmp_path / "selected_arch.json"
    selected_arch_path.write_text(json.dumps({
        "selected_arch": {"0": {"ffn": "ffn_50"}},  # 选了 ffn_50（对 bypass slot 无效）
    }), encoding="utf-8")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "build_selected.py"),
         "--selected_arch", str(selected_arch_path),
         "--block_map", str(block_map_path),
         "--flat_model", str(flat),
         "--build_fn", "build_model",
         "--block_library", str(block_lib),
         "--father_state", str(father_state),
         "--output_dir", str(out_dir)],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0, (
        "build_selected 应拒掉 bypass slot 选 ffn_50 的 selected_arch（fail loud）"
    )
    assert "结构无效" in proc.stderr or "is_valid" in proc.stderr.lower()
