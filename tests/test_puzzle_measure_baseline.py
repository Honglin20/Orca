"""test_puzzle_measure_baseline.py —— Phase U6 measure_baseline + search_space_io 测试。

U6 改造：脚本走 ``--adapters`` + ``--flat_model`` + ``--build_fn``，不再接 ``--eval_fn`` /
``--eval_kind`` / ``--father_ckpt``（双零语义）。fidelity smoke 4 道保留 intent：
  - ckpt-load：``adapters.load_pretrained`` 返 ``_LoadResult``（root cause C：宽松）。
  - forward-determinism：``adapters.forward_model`` 两次 torch.equal。
  - per-slot identity allclose：两次 forward 逐元素 allclose。
  - eval-stability：``adapters.evaluate`` 两次，atol 读 ``EVAL_NOISE_ATOL``（root cause B）。

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
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_REPO / "tests"))

from _puzzle_test_fixtures import (  # noqa: E402
    TINY_FLAT_PY,
    search_space_payload,
    write_flat_and_adapters,
)


def _dump_yaml(payload: dict, path: Path) -> None:
    import yaml
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)


def _run(script: str, args: list[str]) -> tuple[int, str, str]:
    cmd = [sys.executable, str(_SCRIPTS_DIR / script), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _parse_result_json(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        if line.startswith("RESULT_JSON:"):
            return json.loads(line.split(":", 1)[1].strip())
    raise AssertionError(f"无 RESULT_JSON 行：\n{stdout}")


def _setup_fixture(tmp_path: Path) -> dict[str, Path]:
    """写 flat + adapters + father ckpt + search_space.yaml，返回路径。"""
    paths = write_flat_and_adapters(tmp_path)
    ss_path = tmp_path / "search_space.yaml"
    _dump_yaml(search_space_payload(num_blocks=2), ss_path)
    paths["search_space"] = ss_path
    paths["output_dir"] = tmp_path / "out"
    return paths


# ── search_space_io 单元 ──────────────────────────────────────────────────────

def test_search_space_io_roundtrip(tmp_path: Path) -> None:
    from search_space_io import load_search_space_yaml, save_search_space_yaml, to_block_map

    ss = tmp_path / "ss.yaml"
    _dump_yaml(search_space_payload(num_blocks=1), ss)
    slot_dicts, candidates = load_search_space_yaml(ss)
    assert len(slot_dicts) == 2
    assert slot_dicts[0]["parent_module_path"] == "blocks.0.attn"
    assert slot_dicts[0]["kind_evidence"]
    assert "identity" in candidates["attention"]

    bm = to_block_map(slot_dicts)
    assert bm.slots[0].parent_module_path == "blocks.0.attn"

    out_ss = tmp_path / "out_ss.yaml"
    save_search_space_yaml(out_ss, slot_dicts, candidates)
    slot_dicts2, _ = load_search_space_yaml(out_ss)
    assert slot_dicts2[0]["kind_evidence"]


def test_search_space_io_load_fail_loud(tmp_path: Path) -> None:
    from search_space_io import load_search_space_yaml

    with pytest.raises(FileNotFoundError):
        load_search_space_yaml(tmp_path / "nope.yaml")
    _dump_yaml({"slots": [{"id": "A", "path": "x", "kind": "weird", "layer_idx": 0}],
                "candidates": {"attention": ["identity"]}}, tmp_path / "bad.yaml")
    with pytest.raises(ValueError, match="kind"):
        load_search_space_yaml(tmp_path / "bad.yaml")


# ── measure_baseline：happy path 4 smokes ─────────────────────────────────────

@pytest.mark.slow
def test_measure_baseline_happy_4_smokes(tmp_path: Path) -> None:
    paths = _setup_fixture(tmp_path)
    paths["output_dir"].mkdir(parents=True, exist_ok=True)
    rc, out, err = _run("measure_baseline.py", [
        "--flat_path", str(paths["flat"]),
        "--build_fn", "build_model",
        "--build_cfg", "",
        "--adapters", str(paths["adapters"]),
        "--search_space_path", str(paths["search_space"]),
        "--latency_unit", "ms",
        "--output_dir", str(paths["output_dir"]),
        "--seed", "0",
    ])
    assert rc == 0, f"measure_baseline rc={rc}\nSTDERR:\n{err}\nSTDOUT:\n{out}"
    result = _parse_result_json(out)
    assert result["model_type_supported"] is True
    assert result["smokes_passed"] == [
        "ckpt-load", "forward-determinism",
        "per-slot-identity-allclose", "eval-stability",
    ], f"4 道 smoke 应全绿：{result['smokes_passed']}"
    assert result["baseline_acc"] > 0
    assert result["baseline_latency"] > 0
    assert result["ckpt_from_scratch"] is False
    assert (paths["output_dir"] / "block_map.json").is_file()
    assert (paths["output_dir"] / "baseline_metrics.json").is_file()
    bm = json.loads((paths["output_dir"] / "block_map.json").read_text())
    assert all(s["in_dim"] == 32 for s in bm["slots"])
    # 新增 floor + feasibility 字段（block-zero floor 早退可行性检查）
    assert result["latency_target_feasible"] is True, (
        f"默认 target=0.5，合成 transformer block 占主导，应 feasible\nSTDOUT:\n{out}"
    )
    assert result["max_achievable_reduction"] > 0, "max_reduction 应 > 0（block 非零占比）"
    assert result["latency_floor"] > 0, "floor latency 应 > 0"
    assert result["latency_floor"] < result["baseline_latency"], (
        "floor（全 block 置零）应小于 baseline——合成模型 block 是主要开销"
    )
    # baseline_metrics.json 同样落盘新字段
    bm_json = json.loads((paths["output_dir"] / "baseline_metrics.json").read_text())
    assert bm_json["latency_floor"] == result["latency_floor"]
    assert bm_json["max_achievable_reduction"] == result["max_achievable_reduction"]
    assert bm_json["latency_target_feasible"] is True
    assert bm_json["latency_reduction_target"] == 0.5  # argparse default
    assert "latency_infeasible_reason" not in bm_json  # feasible → 无 reason


# ── block-zero floor latency 单元（通用性：不假设 block 类）────────────────────

def test_measure_block_zero_floor_latency_basic(tmp_path: Path) -> None:
    """measure_block_zero_floor_latency：替换 slot 为零输出块后整模 latency < baseline。

    意图（Rule 9）：验证 floor 测量逻辑本身——setattr 替换 slot 为 _FloorZeroModule
    + forward 仍能跑（残差结构下零输出合法）+ floor_latency 数值合理（< baseline）。
    """
    import torch
    paths = _setup_fixture(tmp_path)
    import puzzle_common as pc
    from search_space_io import load_search_space_yaml, to_block_map

    slot_dicts, _ = load_search_space_yaml(paths["search_space"])
    # 简化：直接用 path 构造 block_map（in/out_dim floor 测量不依赖）
    for d in slot_dicts:
        d["in_dim"] = 32
        d["out_dim"] = 32
    block_map = to_block_map(slot_dicts)

    adapters = pc.load_puzzle_adapters(paths["adapters"])
    device = torch.device("cpu")
    model = pc.build_pretrained_model(adapters)
    forward_fn = adapters.forward_model
    dummy = pc.build_latency_dummy(adapters, device=device)
    baseline = pc.measure_whole_model_latency(model, forward_fn, dummy, device)
    floor = pc.measure_block_zero_floor_latency(adapters, block_map, device)

    assert floor > 0, "floor latency 应 > 0（非 block 开销仍存在）"
    assert floor < baseline, (
        f"floor（block 全零）应 < baseline（{baseline}），得 {floor}——合成 fixture block 占主导"
    )

    # 调用后 baseline 模型不被污染（floor 函数内部应恢复 slot；即使不恢复也是独立实例）：
    # 用 build_pretrained_model 重建一个 father，跑 forward 应不报错（验证 _FloorZeroModule
    # 替换 + 恢复的 setattr 流程没破坏模型结构）。
    fresh = pc.build_pretrained_model(adapters)
    with torch.no_grad():
        forward_fn(fresh, dummy)


def test_floor_zero_module_handles_nonsquare_shape() -> None:
    """_FloorZeroModule 对非方 slot（in_dim != out_dim）也返正确 shape 的零。

    意图：通用性铁律——floor 测量不假设 in_dim==out_dim。构造 in=8 / out=16 的 slot，
    替换后 _FloorZeroModule.forward 应返 (B, 16) 零张量（B = 输入 batch）。
    """
    import torch
    import puzzle_common as pc

    # captured output shape[1:] = (16,)（slot 输出 16 维，非方）
    mod = pc._FloorZeroModule(out_shape_tail=(16,), dtype=torch.float32)
    # 输入是 (4, 8)——非方 slot，in_dim=8 / out_dim=16
    out = mod(torch.randn(4, 8))
    assert out.shape == (4, 16), f"非方 slot：期望 (4, 16)，得 {out.shape}"
    assert torch.all(out == 0)
    # kwargs-only 调用也兼容（父层可能传 attn_mask 等）
    out2 = mod(torch.randn(2, 8), attn_mask=torch.randn(2, 2))
    assert out2.shape == (2, 16)


def test_measure_block_zero_floor_empty_blockmap_raises(tmp_path: Path) -> None:
    """空 block_map → fail loud（floor 无意义；unsupported 分支应在上游拦截）。"""
    import torch
    import puzzle_common as pc
    paths = _setup_fixture(tmp_path)
    adapters = pc.load_puzzle_adapters(paths["adapters"])
    empty_bm = pc.BlockMap(slots=[])
    with pytest.raises(RuntimeError, match="为空"):
        pc.measure_block_zero_floor_latency(
            adapters, empty_bm, torch.device("cpu")
        )


# ── §6.7 layer-passthrough floor（transformer_layer kind）────────────────────

def test_floor_layer_passthrough_returns_input() -> None:
    """_FloorLayer.forward 返回首个 tensor 输入 x（layer-passthrough，design draft §6.7）。

    意图（Rule 9）：passthrough 原样返回输入（非 zeros）——layer residual unit 的 floor
    语义。验证首参 tensor / 多参 + mask kwargs / kwargs-only / 无 tensor fail loud 四分支。
    """
    import torch
    import puzzle_common as pc

    mod = pc._FloorLayer()
    x = torch.randn(2, 16, 32)
    out = mod(x)
    assert out is x, "passthrough 应原样返回输入 tensor（非 zeros_like）"

    # 多参 + mask kwargs：返回首个 positional tensor，忽略其余
    y = torch.randn(2, 16, 32)
    out2 = mod(y, torch.randn(2, 2), attn_mask=torch.randn(2, 2))
    assert out2 is y

    # kwargs-only：返回首个 tensor kwarg
    out3 = mod(src_mask=y)
    assert out3 is y

    # 无 tensor 输入 → fail loud（Rule 12：floor 块需 tensor 才能 passthrough）
    with pytest.raises(RuntimeError, match="tensor"):
        mod(foo="bar")


def test_measure_block_zero_floor_layer_passthrough(tmp_path: Path) -> None:
    """§6.7 layer 粒度 floor：transformer_layer slot → _FloorLayer（passthrough）。

    意图（Rule 9）：layer 是 residual unit（x = x + attn(...)），整层 return 0 破坏 residual
    stream → 后续层输入全零崩溃；return x 则层被旁路（latency≈0），保 residual stream 完整。
    验证 kind-specific floor 分派 + passthrough 语义：
      1. transformer_layer slot 的 floor 用 _FloorLayer（不需捕获 output shape）。
      2. floor 后整模 forward 不崩 + 输出 finite。
      3. passthrough 保 residual stream → 输出依赖输入（两个不同输入产生不同输出；
         若误用 _FloorZeroModule，多层 zero 会使 block 输出与输入无关 → 两输入输出一致）。
    """
    import torch
    import puzzle_common as pc
    paths = write_flat_and_adapters(tmp_path)
    adapters = pc.load_puzzle_adapters(paths["adapters"])
    device = torch.device("cpu")

    # TinyBlock = norm1+attn+norm2+ffn+2residual（完整 transformer encoder layer）
    # → slot path 指向整层（blocks.N），kind=transformer_layer，in_dim==out_dim（残差直通合法）
    block_map = pc.BlockMap(slots=[
        pc.Slot(layer_idx=i, kind="transformer_layer", in_dim=32, out_dim=32,
                num_heads=4, head_dim=8, source_class="TinyBlock",
                parent_module_path=f"blocks.{i}", original_intermediate=64,
                activation="gelu")
        for i in range(2)
    ])
    forward_fn = adapters.forward_model
    dummy = pc.build_latency_dummy(adapters, device=device)

    # floor 不崩 + 返回正 latency（非 slot 开销：embed/norm/head 仍存在）
    floor = pc.measure_block_zero_floor_latency(adapters, block_map, device)
    assert floor > 0, "layer floor latency 应 > 0（非 slot 开销：embed/norm/head 仍存在）"

    # passthrough 语义：替换为 _FloorLayer 后 forward 输出 finite + 依赖输入（非坍缩）
    model = pc.build_pretrained_model(adapters).eval().to(device)
    for slot in block_map.slots:
        pc.replace_slot(model, slot.parent_module_path, pc._FloorLayer().eval())
    dummy_b = torch.randn_like(dummy)
    with torch.no_grad():
        out_a = forward_fn(model, dummy)
        out_b = forward_fn(model, dummy_b)
    assert torch.isfinite(out_a).all(), (
        "passthrough floor 后输出应 finite——若 NaN/inf 说明 residual stream 崩"
    )
    assert not torch.allclose(out_a, out_b, atol=1e-6), (
        "passthrough floor 输出应依赖输入——两输入输出一致说明误用了 zero"
        "（block zero 使整层输出与输入无关，破坏 residual stream）"
    )


def test_measure_block_zero_floor_block_kind_still_uses_zero(tmp_path: Path) -> None:
    """§6.7 block 粒度 floor 不回归：attention/ffn slot 仍用 _FloorZeroModule（zero）。

    意图（Rule 9）：kind-specific floor 分派保留 block 语义——block 在 residual 内，零输出
    = 贡献零（x + 0 = x）合法。验证 block-kind slot 的 floor 走 zero 路径（需捕获 output
    shape）+ floor latency < baseline。与上方 layer-passthrough 测试对照（同函数不同 kind 分派）。
    """
    import torch
    import puzzle_common as pc
    from search_space_io import load_search_space_yaml, to_block_map

    paths = _setup_fixture(tmp_path)
    slot_dicts, _ = load_search_space_yaml(paths["search_space"])
    for d in slot_dicts:
        d["in_dim"] = 32
        d["out_dim"] = 32
    block_map = to_block_map(slot_dicts)
    # 固件产 attention + ffn block slot（kind != transformer_layer）→ 走 zero 路径
    assert all(s.kind != "transformer_layer" for s in block_map.slots)

    adapters = pc.load_puzzle_adapters(paths["adapters"])
    device = torch.device("cpu")
    model = pc.build_pretrained_model(adapters)
    baseline = pc.measure_whole_model_latency(
        model, adapters.forward_model,
        pc.build_latency_dummy(adapters, device=device), device,
    )
    floor = pc.measure_block_zero_floor_latency(adapters, block_map, device)
    assert 0 < floor < baseline, (
        f"block floor（zero）应 > 0 且 < baseline（{baseline}），得 {floor}"
    )


def test_floor_layer_consistent_with_no_op_layer() -> None:
    """两套 §6.7 floor 实现语义一致性：_FloorLayer ≡ make_no_op_layer（均 passthrough）。

    意图（Rule 9）：puzzle_common._FloorLayer（measure_baseline 走）与
    transformer_layer_variants.make_no_op_layer（latency_table 走）是两套独立实现的
    §6.7 layer-passthrough。锁定两者 forward 输出一致——防漂移（若一方改语义，
    measure_baseline 与 latency_table 的 floor 数值会发散，下游 MIP silently 漂移）。
    """
    import torch
    import puzzle_common as pc
    import transformer_layer_variants as tlv
    from types import SimpleNamespace

    slot = SimpleNamespace(in_dim=32, out_dim=32, parent_module_path="mock")
    floor_layer = pc._FloorLayer()
    no_op_layer = tlv.make_no_op_layer(slot)
    x = torch.randn(2, 16, 32)
    # 两者都 passthrough（return x）；输出一致 + 等于输入
    assert torch.equal(floor_layer(x), no_op_layer(x)), (
        "_FloorLayer 与 make_no_op_layer 输出应一致（均 passthrough）"
    )
    assert torch.equal(floor_layer(x), x), "_FloorLayer 应 passthrough（return x）"


def test_measure_block_zero_floor_mixed_kinds(tmp_path: Path) -> None:
    """§6.7 混合 kind block_map：layer + block slot 并存的 floor 分派协作。

    意图（Rule 9）：measure_block_zero_floor_latency 的两路径径在同一 model 中并发——
    transformer_layer slot 跳过 shape 捕获（_FloorLayer passthrough），attention block slot
    捕获 shape（_FloorZeroModule zero）。验证两路径协作不崩 + floor < baseline。
    """
    import torch
    import puzzle_common as pc
    paths = write_flat_and_adapters(tmp_path)
    adapters = pc.load_puzzle_adapters(paths["adapters"])
    device = torch.device("cpu")

    block_map = pc.BlockMap(slots=[
        # transformer_layer slot：整层（passthrough，不捕获 shape）
        pc.Slot(layer_idx=0, kind="transformer_layer", in_dim=32, out_dim=32,
                num_heads=4, head_dim=8, source_class="TinyBlock",
                parent_module_path="blocks.0", original_intermediate=64,
                activation="gelu"),
        # attention block slot：子块（zero，捕获 shape）
        pc.Slot(layer_idx=1, kind="attention", in_dim=32, out_dim=32,
                num_heads=4, head_dim=8, source_class="SimpleAttention",
                parent_module_path="blocks.1.attn"),
    ])
    forward_fn = adapters.forward_model
    dummy = pc.build_latency_dummy(adapters, device=device)
    model = pc.build_pretrained_model(adapters)
    baseline = pc.measure_whole_model_latency(model, forward_fn, dummy, device)
    floor = pc.measure_block_zero_floor_latency(adapters, block_map, device)
    assert 0 < floor < baseline, (
        f"混合 kind floor 应 > 0 且 < baseline（{baseline}），得 {floor}"
    )


def test_latency_table_floor_layer_passthrough(tmp_path: Path) -> None:
    """§6.7 latency_table.py floor 循环：transformer_layer slot → make_no_op_layer 分支。

    意图（Rule 9）：latency_table.py 有独立的 floor 循环（区别于 measure_baseline 走的
    measure_block_zero_floor_latency）。F1 后 transformer_layer floor **不经 catalog**——
    no_op_layer 已退出候选集，floor 直接 import make_no_op_layer + 仅校验 in_dim==out_dim。
    验证方 transformer_layer slot 进 replaced_zero_slots（非 kept_original），证明 make_no_op_layer
    分支执行（防误退回 catalog 查 no_op_layer 致 raise / 误用 no_op 致死代码）。
    """
    import torch
    paths = write_flat_and_adapters(tmp_path)
    flat, adapters = paths["flat"], paths["adapters"]
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)
    block_library_dir = output_dir / "block_library"
    block_library_dir.mkdir(parents=True, exist_ok=True)

    # block_map：2 个 transformer_layer slot（TinyBlock = norm1+attn+norm2+ffn+2residual）
    block_map_path = output_dir / "block_map.json"
    slots = [
        {"layer_idx": i, "kind": "transformer_layer", "in_dim": 32, "out_dim": 32,
         "num_heads": 4, "head_dim": 8, "source_class": "TinyBlock",
         "parent_module_path": f"blocks.{i}", "original_intermediate": 64,
         "activation": "gelu", "forward_arity": "single", "return_arity": "single",
         "mask_load_bearing": False, "ffn_struct": "standard"}
        for i in range(2)
    ]
    block_map_path.write_text(json.dumps({"slots": slots}), encoding="utf-8")

    # block_library：identity ckpt（passthrough，main loop 不 load 内容，仅需文件存在供 glob）
    for i in range(2):
        torch.save({}, block_library_dir / f"L{i}_transformer_layer_identity.pt")

    rc, out, err = _run("latency_table.py", [
        "--block_map", str(block_map_path), "--flat_model", str(flat),
        "--build_fn", "build_model", "--build_cfg", "",
        "--block_library", str(block_library_dir), "--adapters", str(adapters),
        "--output_dir", str(output_dir),
    ])
    assert rc == 0, f"latency_table rc={rc}\nSTDERR:\n{err}\nSTDOUT:\n{out}"

    floor_path = output_dir / "latency_floor.json"
    assert floor_path.is_file(), f"latency_floor.json 未生成：\n{out}"
    floor = json.loads(floor_path.read_text())
    # transformer_layer slot 应全部 passthrough（replaced_zero），非 kept_original
    assert len(floor["replaced_zero_slots"]) == 2, (
        f"两 transformer_layer slot 应 passthrough（replaced_zero），"
        f"得 replaced={floor['replaced_zero_slots']}, kept={floor['kept_original_slots']}"
    )
    assert len(floor["kept_original_slots"]) == 0, (
        f"方 transformer_layer slot 不应 kept_original（应 passthrough），"
        f"得 kept={floor['kept_original_slots']}"
    )
    assert floor["floor_latency"] > 0


def test_latency_table_floor_non_square_layer_kept_original(tmp_path: Path) -> None:
    """F1 配套：非方 transformer_layer slot → kept_original（floor 不经 catalog，仅校验 in_dim==out_dim）。

    意图（Rule 9）：latency_table floor 的 transformer_layer 分支 F1 后「不经 catalog」——直接 import
    make_no_op_layer + 仅校验 in_dim==out_dim。非方 layer slot（in_dim != out_dim）应短路到
    kept_original（保留原块，其 latency 计入 floor），**不**调 make_no_op_layer（会对非方 raise）。
    锁定该非方守卫不被误删（删则 make_no_op_layer raise 致 latency_table 崩）。
    """
    import torch
    paths = write_flat_and_adapters(tmp_path)
    flat, adapters = paths["flat"], paths["adapters"]
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)
    block_library_dir = output_dir / "block_library"
    block_library_dir.mkdir(parents=True, exist_ok=True)

    # block_map：1 方 + 1 非方 transformer_layer slot（非方 in=32/out=48 违残差铁律，但测 floor 守卫）
    block_map_path = output_dir / "block_map.json"
    slots = [
        {"layer_idx": 0, "kind": "transformer_layer", "in_dim": 32, "out_dim": 32,
         "num_heads": 4, "head_dim": 8, "source_class": "TinyBlock",
         "parent_module_path": "blocks.0", "original_intermediate": 64,
         "activation": "gelu", "forward_arity": "single", "return_arity": "single",
         "mask_load_bearing": False, "ffn_struct": "standard"},
        {"layer_idx": 1, "kind": "transformer_layer", "in_dim": 32, "out_dim": 48,
         "num_heads": 4, "head_dim": 8, "source_class": "TinyBlock",
         "parent_module_path": "blocks.1", "original_intermediate": 64,
         "activation": "gelu", "forward_arity": "single", "return_arity": "single",
         "mask_load_bearing": False, "ffn_struct": "standard"},
    ]
    block_map_path.write_text(json.dumps({"slots": slots}), encoding="utf-8")

    for i in range(2):
        torch.save({}, block_library_dir / f"L{i}_transformer_layer_identity.pt")

    rc, out, err = _run("latency_table.py", [
        "--block_map", str(block_map_path), "--flat_model", str(flat),
        "--build_fn", "build_model", "--build_cfg", "",
        "--block_library", str(block_library_dir), "--adapters", str(adapters),
        "--output_dir", str(output_dir),
    ])
    assert rc == 0, f"latency_table rc={rc}\nSTDERR:\n{err}\nSTDOUT:\n{out}"

    floor_path = output_dir / "latency_floor.json"
    assert floor_path.is_file(), f"latency_floor.json 未生成：\n{out}"
    floor = json.loads(floor_path.read_text())
    # 非方 layer slot（blocks.1）应 kept_original；方 slot（blocks.0）应 replaced_zero（passthrough）
    assert "blocks.1" in floor["kept_original_slots"], (
        f"非方 layer slot 应 kept_original，得 kept={floor['kept_original_slots']}"
    )
    assert "blocks.0" in floor["replaced_zero_slots"], (
        f"方 layer slot 应 replaced_zero（passthrough），得 replaced={floor['replaced_zero_slots']}"
    )
    assert floor["floor_latency"] > 0


# ── 早退：latency 结构性不可达 → exit 3（区别于 unsupported 的 exit 2）─────────

@pytest.mark.slow
def test_measure_baseline_latency_infeasible_exit_3(tmp_path: Path) -> None:
    """target=0.99 极端高 → block 占比达不到 → exit 3（latency_target_feasible=false）。

    意图（Rule 9）：验证「结构性不可达」的早退分支——模型可替换 + smoke 全绿，但 block
    替换物理上限 < target。区别于 exit 2（unsupported/异常）：exit 3 是已知分支，非异常。
    """
    paths = _setup_fixture(tmp_path)
    paths["output_dir"].mkdir(parents=True, exist_ok=True)
    rc, out, err = _run("measure_baseline.py", [
        "--flat_path", str(paths["flat"]),
        "--build_fn", "build_model",
        "--adapters", str(paths["adapters"]),
        "--search_space_path", str(paths["search_space"]),
        "--latency_unit", "ms",
        "--output_dir", str(paths["output_dir"]),
        "--latency_reduction_target", "0.99",  # 极端高——block 占比达不到
        "--seed", "0",
    ])
    assert rc == 3, (
        f"target=0.99 应结构性不可达（exit 3），得 rc={rc}\nSTDERR:\n{err}\nSTDOUT:\n{out}"
    )
    result = _parse_result_json(out)
    # 模型可替换、smoke 全绿——区别于 exit 2 unsupported
    assert result["model_type_supported"] is True
    assert result["smokes_passed"] == [
        "ckpt-load", "forward-determinism",
        "per-slot-identity-allclose", "eval-stability",
    ]
    assert result["latency_target_feasible"] is False
    assert result["max_achievable_reduction"] < 0.99
    assert result["latency_floor"] > 0
    assert result["baseline_latency"] > result["latency_floor"]
    assert "block 替换最大 reduction" in result["error"]
    # baseline_metrics 落盘 reason
    bm = json.loads((paths["output_dir"] / "baseline_metrics.json").read_text())
    assert bm["latency_target_feasible"] is False
    assert bm["latency_reduction_target"] == 0.99
    assert "latency_infeasible_reason" in bm
    assert "block 占比过低" in bm["latency_infeasible_reason"]
    # 产物仍齐全（exit 3 不应跳过 artifact 写盘）
    assert (paths["output_dir"] / "block_map.json").is_file()
    assert (paths["output_dir"] / "search_space.yaml").is_file()


@pytest.mark.slow
def test_measure_baseline_latency_feasible_low_target_exit_0(tmp_path: Path) -> None:
    """target=0.05 极低 → block 占比必超 → exit 0（latency_target_feasible=true）。

    意图：feasibility 早退分支的反向 sanity——任何非零 block 占比都能过 0.05 目标。
    """
    paths = _setup_fixture(tmp_path)
    paths["output_dir"].mkdir(parents=True, exist_ok=True)
    rc, out, err = _run("measure_baseline.py", [
        "--flat_path", str(paths["flat"]),
        "--build_fn", "build_model",
        "--adapters", str(paths["adapters"]),
        "--search_space_path", str(paths["search_space"]),
        "--output_dir", str(paths["output_dir"]),
        "--latency_reduction_target", "0.05",
        "--seed", "0",
    ])
    assert rc == 0, f"target=0.05 应 feasible（exit 0）\nSTDERR:\n{err}\nSTDOUT:\n{out}"
    result = _parse_result_json(out)
    assert result["latency_target_feasible"] is True
    assert result["max_achievable_reduction"] >= 0.05


# ── floor helper 分支：tuple output / 非 tensor raise / path 失败 ──────────────

def test_capture_slot_output_shapes_handles_tuple_output(tmp_path: Path) -> None:
    """slot forward 返回 tuple/list（如 attention 返 (out, attn_weights)）→ hook 取首元素 shape。"""
    import torch
    import torch.nn as nn
    import puzzle_common as pc

    class TupleSlot(nn.Module):
        def __init__(self):
            super().__init__()
            self.lin = nn.Linear(8, 16)
        def forward(self, x):
            out = self.lin(x)
            return out, out.new_ones(out.shape)  # (out, attn_weights)

    class Host(nn.Module):
        def __init__(self):
            super().__init__()
            self.slot = TupleSlot()
        def forward(self, x):
            o, _ = self.slot(x)
            return o.sum()

    model = Host().eval()
    forward_fn = lambda m, b: m(b)
    shapes = pc._capture_slot_output_shapes(
        model, ["slot"], torch.randn(2, 8), forward_fn, torch.device("cpu")
    )
    # tuple output → hook 取首元素 (B,16) → out_shape_tail=(16,)
    assert shapes["slot"] == ((16,), torch.float32)


def test_capture_slot_output_shapes_non_tensor_raises(tmp_path: Path) -> None:
    """slot 输出非 tensor（如 list of int）→ fail loud（puzzle 契约 slot 输出须 tensor）。"""
    import torch
    import torch.nn as nn
    import puzzle_common as pc

    class WeirdSlot(nn.Module):
        def forward(self, x):
            return [1, 2, 3]  # 非 tensor

    class Host(nn.Module):
        def __init__(self):
            super().__init__()
            self.slot = WeirdSlot()
        def forward(self, x):
            # 必须真的调用 slot——否则 hook 不触发，测不到「非 tensor」分支
            self.slot(x)
            return x.sum()

    model = Host().eval()
    with pytest.raises(RuntimeError, match="非 tensor"):
        pc._capture_slot_output_shapes(
            model, ["slot"], torch.randn(2, 4),
            lambda m, b: m(b), torch.device("cpu"),
        )


def test_capture_slot_output_shapes_missing_path_raises() -> None:
    """slot path 在 model 中定位失败（get_submodule 抛 AttributeError）→ fail loud 点名。"""
    import torch
    import torch.nn as nn
    import puzzle_common as pc

    class Host(nn.Module):
        def __init__(self):
            super().__init__()
            self.real = nn.Linear(4, 4)
        def forward(self, x):
            return self.real(x).sum()

    model = Host().eval()
    with pytest.raises(AttributeError, match="nonexistent"):
        pc._capture_slot_output_shapes(
            model, ["nonexistent.path"], torch.randn(2, 4),
            lambda m, b: m(b), torch.device("cpu"),
        )


def test_measure_baseline_baseline_latency_zero_raises(tmp_path: Path) -> None:
    """baseline_latency ≤ 0（latency_script_path 异常返回 0）→ fail loud raise（不静默写 max_reduction）。

    subprocess 隔离 → monkeypatch 无效；改用真实的 latency_script_path 返 0 模拟异常。
    ONNX 单文件契约：脚本签名 fn(onnx_path) -> float（measure_baseline 导出 ONNX 后调它）。
    """
    paths = _setup_fixture(tmp_path)
    paths["output_dir"].mkdir(parents=True, exist_ok=True)
    # 写一个 latency 脚本始终返 0.0——measure_baseline 的 baseline 守卫应 raise（不写 max_reduction）
    fake_latency = tmp_path / "fake_latency.py"
    fake_latency.write_text(textwrap.dedent("""
        def measure(onnx_path):
            return 0.0
    """), encoding="utf-8")
    rc, out, err = _run("measure_baseline.py", [
        "--flat_path", str(paths["flat"]),
        "--build_fn", "build_model",
        "--adapters", str(paths["adapters"]),
        "--search_space_path", str(paths["search_space"]),
        "--output_dir", str(paths["output_dir"]),
        "--latency_reduction_target", "0",
        "--latency_script_path", f"{fake_latency}::measure",
    ])
    assert rc != 0, "baseline_latency=0 应 fail loud（rc!=0）"
    assert "baseline_latency" in err and "非正" in err, (
        f"stderr 应点名 baseline_latency 非正：\n{err}"
    )


# ── ONNX 单文件契约（latency_script_path，SPEC P2.5）───────────────────────────

def test_native_batch_to_export_args_conventions() -> None:
    """_native_batch_to_export_args 按 FORWARD_CALLING_CONVENTION 拆 native batch。

    意图（Rule 9）：契约级单测——single/positional/dict 三 convention 的 args/kwargs
    拆分正确，dict 与 convention 不符 → fail loud（不静默猜）。
    """
    import torch
    import puzzle_common as pc

    t = torch.randn(1, 4)

    # single：tensor / (tensor, labels) 都取首个 tensor
    args, kwargs = pc._native_batch_to_export_args(t, "single")
    assert args == (t,) and kwargs == {}
    args, kwargs = pc._native_batch_to_export_args((t, torch.randint(0, 3, (1,))), "single")
    assert args == (t,) and kwargs == {}

    # positional：tensor 序列 → args 全量
    t2 = torch.randn(1, 8)
    args, kwargs = pc._native_batch_to_export_args((t, t2), "positional")
    assert args == (t, t2) and kwargs == {}

    # dict：batch dict → kwargs 全量
    args, kwargs = pc._native_batch_to_export_args({"x": t, "y": t2}, "dict")
    assert args == () and kwargs == {"x": t, "y": t2}

    # dict convention 但 batch 非 dict → fail loud
    with pytest.raises(TypeError, match="dict"):
        pc._native_batch_to_export_args(t, "dict")


def test_measure_module_latency_via_onnx_script_exports_and_calls(tmp_path: Path) -> None:
    """measure_module_latency_via_onnx_script：导出单文件 ONNX → 调 fn(onnx_path) → float。

    意图（Rule 9）：验证 ONNX 单文件契约全链——用户脚本收到真实 onnx 文件路径（脚本内部
    断言文件存在），返回值被 float() 收敛。禁止 fn(model, batch) 旧契约。
    """
    import torch
    import torch.nn as nn
    import puzzle_common as pc

    class Tiny(nn.Module):
        def forward(self, x):
            return x * 2

    # 用户脚本：ONNX 契约签名 fn(onnx_path)，断言文件存在 + 返恒定时延
    prov = tmp_path / "latency_provider.py"
    prov.write_text(textwrap.dedent("""
        import os
        def measure(onnx_path):
            if not os.path.isfile(onnx_path):
                raise FileNotFoundError(f"ONNX 不存在: {onnx_path}")
            if not onnx_path.endswith(".onnx"):
                raise ValueError(f"应收到 .onnx 路径: {onnx_path}")
            return 12.5
    """), encoding="utf-8")

    val = pc.measure_module_latency_via_onnx_script(
        Tiny(), (torch.randn(1, 4),), {}, torch.device("cpu"),
        f"{prov}::measure",
    )
    assert val == 12.5


def test_measure_module_latency_via_onnx_script_device_kwarg(tmp_path: Path) -> None:
    """fn(onnx_path, device=...) 可选 kwarg 契约：声明 device 形参则传 str(device)。"""
    import torch
    import torch.nn as nn
    import puzzle_common as pc

    class Tiny(nn.Module):
        def forward(self, x):
            return x

    prov = tmp_path / "latency_provider.py"
    prov.write_text(textwrap.dedent("""
        def measure(onnx_path, device=None):
            assert device == "cpu", f"device 应传 'cpu'，得到 {device!r}"
            return 3.25
    """), encoding="utf-8")

    val = pc.measure_module_latency_via_onnx_script(
        Tiny(), (torch.randn(1, 4),), {}, torch.device("cpu"),
        f"{prov}::measure",
    )
    assert val == 3.25


def test_measure_whole_model_latency_onnx_contract(tmp_path: Path) -> None:
    """measure_whole_model_latency + latency_script_path 走 ONNX 契约（导出整模 → fn(onnx_path)）。

    意图（Rule 9）：端到端验证整模路径——forward_fn 不被调用（不再 fn(model, batch)），
    用户脚本收到真实 ONNX 路径 + 返回被采纳。
    """
    import torch
    import torch.nn as nn
    import puzzle_common as pc

    class Tiny(nn.Module):
        def forward(self, x):
            return x + 1.0

    prov = tmp_path / "latency_provider.py"
    prov.write_text(textwrap.dedent("""
        import os
        def measure(onnx_path):
            if not os.path.isfile(onnx_path):
                raise FileNotFoundError(onnx_path)
            return 7.75
    """), encoding="utf-8")

    model = Tiny()
    val = pc.measure_whole_model_latency(
        model,
        forward_fn=lambda m, b: m(b),          # 不应被调用（ONNX 契约路径）
        batch=torch.randn(1, 4),
        device=torch.device("cpu"),
        latency_script_path=f"{prov}::measure",
        convention="single",
    )
    assert val == 7.75


# ── workflow 路由层：失败分支收敛到终端 reporter pz_report（in-session 只支持 agent 节点）──

def test_puzzle_yaml_routes_cover_latency_infeasible_branch() -> None:
    """静态校验 puzzle.yaml 的 pz_baseline 路由：成功分支（build_library）/ 失败分支
    统一收敛到终端 reporter pz_report（in-session 模式只支持 kind: agent 节点，
    terminate 节点会在路由时抛 unsupported_node_kind 崩 run——ns3 模式）。

    意图（Rule 9）：验证路由 first-match 设计——
    1. model_type_supported != false AND latency_target_feasible != false → build_library
    2. fallback → pz_report（reporter 读 baseline_metrics.json 区分 latency infeasible /
       unsupported / smoke 失败）

    历史：原 pz_expand 节点拆分为 pz_ingest + pz_search_space + pz_baseline 后
    （2026-08-13 layer-variant 重构），该路由 first-match 合约由 pz_baseline 继承。
    2026-08-13 reporter 优化：删全部 terminate 节点，失败分支 → pz_report。
    """
    from orca.compile.parser import load_workflow

    yaml_path = _REPO / "workflows" / "puzzle.yaml"
    wf = load_workflow(yaml_path)
    pz_baseline = next(n for n in wf.nodes if n.name == "pz_baseline")
    targets = [r.to for r in pz_baseline.routes]
    assert targets == ["pz_build_library", "pz_report"], (
        f"pz_baseline 路由顺序/目标错误：{targets}"
    )
    # 双条件路由（first-match）—— compound expression 必须含两字段
    first_when = pz_baseline.routes[0].when or ""
    assert "model_type_supported" in first_when and "latency_target_feasible" in first_when
    # 全 workflow 无 terminate 节点（in-session 只支持 agent；ns3 reporter 模式）
    terminators = {n.name for n in wf.nodes if n.kind == "terminate"}
    assert not terminators, f"puzzle.yaml 不应有 terminate 节点（in-session 崩 run）：{terminators}"
    # pz_report 是唯一终端 reporter：路由到 $end 无条件（reporter 判终态，非 gate 路由）
    pz_report = next(n for n in wf.nodes if n.name == "pz_report")
    assert [r.to for r in pz_report.routes] == ["$end"], (
        f"pz_report 应无条件路由 $end：{[r.to for r in pz_report.routes]}"
    )
    # reporter output_schema 带 status/stage（终态判定字段）
    schema = pz_report.output_schema or {}
    assert schema.get("type") == "object"
    props = schema.get("properties", {})
    assert "status" in props and "stage" in props and "reason" in props


# ── root cause C：无可用预训练 → fail loud（BLD 需真 teacher）──────────────────

def test_measure_baseline_ckpt_from_scratch_fails_loud(tmp_path: Path) -> None:
    """adapters.load_pretrained 标记 from_scratch=True（ckpt 缺/空/schema 严重不匹配）
    → measure_baseline **fail loud**（rc!=0），不进 BLD/搜索。

    理由：BLD 把候选块蒸馏去模仿 father(teacher) I/O；随机 init teacher 产垃圾 teacher
    信号 → block_library 全错。用户须先训练预训练模型（如跑项目 train.py）再启动 puzzle。

    构造：adapters 的 _FATHER_CKPT 指向不存在的文件 → load_pretrained 返 from_scratch=True。
    """
    paths = write_flat_and_adapters(tmp_path, father_ckpt_path=tmp_path / "father.pth")
    # 不写 father.pth → adapters.load_pretrained 会 from_scratch
    ss_path = tmp_path / "search_space.yaml"
    _dump_yaml(search_space_payload(num_blocks=2), ss_path)
    out_dir = tmp_path / "out"; out_dir.mkdir(parents=True, exist_ok=True)
    rc, out, err = _run("measure_baseline.py", [
        "--flat_path", str(paths["flat"]),
        "--build_fn", "build_model",
        "--adapters", str(paths["adapters"]),
        "--search_space_path", str(ss_path),
        "--output_dir", str(out_dir),
    ])
    assert rc != 0, (
        "from_scratch 应 fail loud（rc!=0）——无可用预训练权重不许进 BLD/搜索；"
        f"STDERR:\n{err}\nSTDOUT:\n{out}"
    )
    assert "from_scratch" in err or "预训练" in err, (
        f"fail-loud 信息应点名 from_scratch/预训练：STDERR:\n{err}"
    )


# ── E22：empty slots → exit 2 + terminate_unsupported ──────────────────────────

def test_measure_baseline_empty_slots_exit_2(tmp_path: Path) -> None:
    paths = _setup_fixture(tmp_path)
    empty_ss = tmp_path / "empty.yaml"
    empty_ss.write_text("slots: []\ncandidates: {}\n", encoding="utf-8")
    paths["output_dir"].mkdir(parents=True, exist_ok=True)
    rc, out, _ = _run("measure_baseline.py", [
        "--flat_path", str(paths["flat"]),
        "--build_fn", "build_model",
        "--adapters", str(paths["adapters"]),
        "--search_space_path", str(empty_ss),
        "--output_dir", str(paths["output_dir"]),
    ])
    assert rc == 2, f"empty slots 应 exit 2，得 rc={rc}"
    result = _parse_result_json(out)
    assert result["model_type_supported"] is False


# ── root cause B：eval-stability atol 读 EVAL_NOISE_ATOL ──────────────────────

def test_measure_baseline_eval_stability_atol_from_adapter(tmp_path: Path) -> None:
    """adapters.EVAL_NOISE_ATOL 决定 eval-stability 容差（不再硬编码 1e-9）。

    构造：evaluate 内含小噪声（两次返差 ~1e-3）；adapters.EVAL_NOISE_ATOL=1e-2 容下。
    """
    import torch
    # 自定义 flat + adapters（evaluate 带小噪声）
    flat_path = tmp_path / "tiny_flat.py"
    flat_path.write_text(TINY_FLAT_PY, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    spec = importlib.util.spec_from_file_location("_tiny_flat_boot", flat_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    father = tmp_path / "father.pth"
    torch.save(mod.build_model().state_dict(), father)

    noisy_adapters = tmp_path / "puzzle_adapters.py"
    noisy_adapters.write_text(textwrap.dedent(f"""
        import importlib.util, torch, torch.nn as nn, torch.nn.functional as F
        from torch.utils.data import DataLoader, TensorDataset
        from collections import namedtuple
        _LoadResult = namedtuple("_LoadResult", ["missing", "unexpected", "from_scratch"])

        _spec = importlib.util.spec_from_file_location("_f", r"{flat_path}")
        _flat = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_flat)
        build_model = _flat.build_model
        DUMMY_INPUT = _flat.DUMMY_INPUT
        FORWARD_CALLING_CONVENTION = "single"
        METRIC_DIRECTION = "higher-better"
        EVAL_NOISE_ATOL = 1e-2  # 容下 evaluate 内 1e-3 量级噪声

        def forward_model(model, batch):
            x = batch[0] if isinstance(batch, (tuple, list)) else batch
            return model(x)

        def calib_iter(device=None):
            x = torch.randn(4, 16, 32)
            return iter(DataLoader(TensorDataset(x), batch_size=2))

        def train_iter(device=None):
            x = torch.randn(8, 16, 32); y = torch.randint(0, 10, (8,))
            return iter(DataLoader(TensorDataset(x, y), batch_size=4))

        def extract_labels(batch):
            return batch[1] if isinstance(batch, (tuple, list)) and len(batch) >= 2 else None

        def kd_loss(s_out, t_out, labels=None):
            s = s_out[0] if isinstance(s_out, (tuple, list)) else s_out
            t = t_out[0] if isinstance(t_out, (tuple, list)) else t_out
            return F.kl_div(F.log_softmax(s, -1), F.softmax(t, -1), reduction="batchmean")

        def task_loss(s_out, labels):
            return None if labels is None else F.cross_entropy(s_out, labels)

        def evaluate(model):
            model.eval()
            with torch.no_grad():
                # base 用固定 seed 的输入（确定性）；noise 模拟采样评估协议噪声
                torch.manual_seed(123)
                x = torch.randn(*DUMMY_INPUT["shape"])
                base = float(model(x).softmax(-1).max(-1).values.mean().item())
                # 1e-3 量级噪声（采样 eval 的真实写照——EVAL_NOISE_ATOL 1e-9 会拦，
                # 1e-2 放过 → root cause B：atol 来自 adapter，不硬编码）
                noise = (torch.rand(1).item() - 0.5) * 2e-3
                return base + noise

        def load_pretrained(model):
            ckpt = torch.load(r"{father}", map_location="cpu", weights_only=False)
            missing, unexpected = model.load_state_dict(ckpt, strict=False)
            return _LoadResult(list(missing), list(unexpected), len(missing) > 0.5 * len(model.state_dict()))
    """), encoding="utf-8")
    ss = tmp_path / "ss.yaml"
    _dump_yaml(search_space_payload(num_blocks=2), ss)
    out_dir = tmp_path / "out"; out_dir.mkdir(parents=True, exist_ok=True)
    rc, out, err = _run("measure_baseline.py", [
        "--flat_path", str(flat_path),
        "--build_fn", "build_model",
        "--adapters", str(noisy_adapters),
        "--search_space_path", str(ss),
        "--output_dir", str(out_dir),
    ])
    assert rc == 0, (
        f"EVAL_NOISE_ATOL=1e-2 应容下 evaluate 1e-3 噪声（root cause B）\nSTDERR:\n{err}"
    )
    result = _parse_result_json(out)
    assert "eval-stability" in result["smokes_passed"]


# ── smoke 2 fail loud：forward-determinism ─────────────────────────────────────

def test_measure_baseline_forward_determinism_fail(tmp_path: Path) -> None:
    """forward 含未固定 RNG（Dropout in eval 泄漏）→ forward-determinism smoke raise → exit 2。"""
    import torch
    nondet_flat = tmp_path / "nondet_flat.py"
    nondet_flat.write_text(textwrap.dedent("""
        import torch, torch.nn as nn, torch.nn.functional as F
        DUMMY_INPUT = {"shape": [2, 8], "dtype": "float32"}

        class NonDet(nn.Module):
            def __init__(self):
                super().__init__()
                self.fc = nn.Linear(8, 4)
            def forward(self, x):
                return self.fc(F.dropout(x, p=0.5, training=True))

        def build_model():
            return NonDet()
    """), encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    spec = importlib.util.spec_from_file_location("_nd", nondet_flat)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    father = tmp_path / "father.pth"
    torch.save(mod.build_model().state_dict(), father)

    adapters_py = tmp_path / "puzzle_adapters.py"
    adapters_py.write_text(textwrap.dedent(f"""
        import importlib.util, torch, torch.nn as nn, torch.nn.functional as F
        from torch.utils.data import DataLoader, TensorDataset
        from collections import namedtuple
        _LoadResult = namedtuple("_LoadResult", ["missing", "unexpected", "from_scratch"])
        _spec = importlib.util.spec_from_file_location("_f", r"{nondet_flat}")
        _flat = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_flat)
        build_model = _flat.build_model; DUMMY_INPUT = _flat.DUMMY_INPUT
        FORWARD_CALLING_CONVENTION = "single"
        METRIC_DIRECTION = "higher-better"
        EVAL_NOISE_ATOL = 1e-2
        def forward_model(m, b):
            x = b[0] if isinstance(b, (tuple, list)) else b
            return m(x)
        def calib_iter(device=None):
            x = torch.randn(4, 8); return iter(DataLoader(TensorDataset(x), batch_size=2))
        def train_iter(device=None):
            x = torch.randn(8, 8); y = torch.randint(0, 10, (8,))
            return iter(DataLoader(TensorDataset(x, y), batch_size=4))
        def extract_labels(b): return b[1] if isinstance(b, (tuple, list)) and len(b) >= 2 else None
        def kd_loss(s, t, labels=None):
            s = s[0] if isinstance(s, (tuple, list)) else s; t = t[0] if isinstance(t, (tuple, list)) else t
            return F.kl_div(F.log_softmax(s, -1), F.softmax(t, -1), reduction="batchmean")
        def task_loss(s, l): return None if l is None else F.cross_entropy(s, l)
        def evaluate(m):
            m.eval()
            with torch.no_grad():
                return float(m(torch.randn(*DUMMY_INPUT["shape"])).abs().mean().item())
        def load_pretrained(m):
            ck = torch.load(r"{father}", map_location="cpu", weights_only=False)
            mi, un = m.load_state_dict(ck, strict=False)
            return _LoadResult(list(mi), list(un), len(mi) > 0.5 * len(m.state_dict()))
    """), encoding="utf-8")
    ss = tmp_path / "ss.yaml"
    _dump_yaml({"slots": [{"id": "L0_attn", "path": "fc", "kind": "attention",
                           "layer_idx": 0, "num_heads": 1, "head_dim": 4}],
                "candidates": {"attention": ["identity"]}}, ss)
    out_dir = tmp_path / "out"; out_dir.mkdir(parents=True, exist_ok=True)
    rc, out, err = _run("measure_baseline.py", [
        "--flat_path", str(nondet_flat),
        "--build_fn", "build_model",
        "--adapters", str(adapters_py),
        "--search_space_path", str(ss),
        "--output_dir", str(out_dir),
    ])
    assert rc == 2, f"forward-determinism 失败应 exit 2，得 rc={rc}"
    assert ("forward-determinism" in err or "identity allclose" in err
            or "eval-stability" in err), f"stderr 应点名 determinism smoke 失败：\n{err}"


# ── smoke 4 isolated：per-slot allclose ───────────────────────────────────────

def test_per_slot_identity_allclose_branch_raises(monkeypatch) -> None:
    """smoke 4 的 allclose 检查能独立拦「两次 forward 的 slot output 不一致」。"""
    import torch
    import measure_baseline as mb

    class Tiny(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.fc = torch.nn.Linear(4, 4)
        def forward(self, x):
            return self.fc(x)

    model = Tiny()
    batch = torch.randn(2, 4)
    call_count = {"n": 0}

    def fake_hook(_model, paths, _batch, _fn, _device):
        call_count["n"] += 1
        val = torch.ones(2, 4) if call_count["n"] == 1 else torch.ones(2, 4) + 0.1
        return {p: val for p in paths}

    monkeypatch.setattr(mb, "_hook_slot_outputs", fake_hook)
    with pytest.raises(RuntimeError, match="per-slot identity allclose"):
        mb.forward_determinism_and_identity_allclose(
            model, ["fc"], batch, lambda m, b: m(b), torch.device("cpu")
        )


# ── load_puzzle_adapters fail loud（缺能力）────────────────────────────────────

def test_load_puzzle_adapters_missing_capability_fails(tmp_path: Path) -> None:
    """adapter 缺关键能力（如 calib_iter）→ load_puzzle_adapters fail loud 点名。"""
    bad = tmp_path / "bad_adapters.py"
    bad.write_text(textwrap.dedent("""
        import torch
        FORWARD_CALLING_CONVENTION = "single"
        METRIC_DIRECTION = "higher-better"
        EVAL_NOISE_ATOL = 1e-9
        DUMMY_INPUT = {"shape": [1, 2], "dtype": "float32"}
        def build_model(): return torch.nn.Linear(2, 2)
        def forward_model(m, b): return m(b)
        # 缺 calib_iter / train_iter / extract_labels / kd_loss / task_loss / evaluate / load_pretrained
    """), encoding="utf-8")
    import puzzle_common as pc
    with pytest.raises(AttributeError, match="calib_iter"):
        pc.load_puzzle_adapters(bad)


def test_load_puzzle_adapters_invalid_direction_fails(tmp_path: Path) -> None:
    """METRIC_DIRECTION 非法值 → fail loud。"""
    bad = tmp_path / "bad_dir.py"
    bad.write_text(textwrap.dedent("""
        import torch
        FORWARD_CALLING_CONVENTION = "single"
        METRIC_DIRECTION = "sideways"
        EVAL_NOISE_ATOL = 1e-9
        DUMMY_INPUT = {"shape": [1, 2], "dtype": "float32"}
        def build_model(): return torch.nn.Linear(2, 2)
        def forward_model(m, b): return m(b)
        def calib_iter(device=None): return iter([torch.randn(2, 2)])
        def train_iter(device=None): return iter([(torch.randn(2, 2), torch.tensor([0, 1]))])
        def extract_labels(b): return b[1] if isinstance(b, tuple) else None
        def kd_loss(s, t, labels=None):
            return torch.tensor(0.0, requires_grad=True)
        def task_loss(s, l): return None
        def evaluate(m): return 0.5
        def load_pretrained(m):
            from collections import namedtuple
            R = namedtuple("R", ["missing", "unexpected", "from_scratch"])
            return R([], [], False)
    """), encoding="utf-8")
    import puzzle_common as pc
    with pytest.raises(ValueError, match="METRIC_DIRECTION"):
        pc.load_puzzle_adapters(bad)
