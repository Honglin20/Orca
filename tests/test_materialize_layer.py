"""test_materialize_layer.py —— materialize_optimized layer 粒度装配测试（design draft §6.4 + §4）。

覆盖 layer 粒度重写后的死不变量：
  1. dispatcher 全 6 layer variant 分支：_build_variant 镜像 vs transformer_layer_variants.make_*_layer
     的 state_dict key 对齐（防 _VARIANT_CONSTRUCTION/dispatcher 与变体源数据漂移；6 分支不靠 e2e）。
  2. dispatcher 边界：全 identity 桩 / no_op-only 早返回 / 未知 variant fail loud / max_seq_len 缺失 fail loud。
  3. materialize 端到端（parametrize 全 6 variant）：key_alignment + standalone forward + 自包含 +
     build_model shape 保持 + load_model strict。
  4. main() 拒未知 variant；build_cfg 烘入；全 identity 零侵入（optimized forward == 父 forward）。
  5. _check_key_alignment 检 shape_mismatch（catalog core_dim 漂移兜底）。
  6. 确定性装配（两次产出 md5 一致）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("torch")

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _REPO / "workflows" / "agents" / "_puzzle_scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_REPO / "tests"))

from _puzzle_test_fixtures import write_flat_and_adapters  # noqa: E402

# flat build_model 默认产 2 blocks，head num_classes=10 → 输出 [B, 10]
_DEFAULT_HEAD_DIM = 10


# ── 共享 helper ────────────────────────────────────────────────────────────────

def _dispatcher_ns() -> dict:
    """exec dispatcher 源所需的名字空间（= optimized_flat 内联后的运行时环境）。"""
    from types import SimpleNamespace

    import transformer_layer_variants as tlv
    from puzzle_blocks import _ACTIVATION_MAP, resolve_activation
    return {
        "SimpleNamespace": SimpleNamespace,
        "_MASK_KEYS": tlv._MASK_KEYS, "_extract_mask": tlv._extract_mask,
        "_StandardFFN": tlv._StandardFFN,
        "_VanillaAttention": tlv._VanillaAttention,
        "_RandomSynthesizerAttention": tlv._RandomSynthesizerAttention,
        "_ReluAttention": tlv._ReluAttention,
        "_FNetMixer": tlv._FNetMixer, "_SoftsStarMixer": tlv._SoftsStarMixer,
        "_PreLNTransformerLayer": tlv._PreLNTransformerLayer,
        "_NoOpLayer": tlv._NoOpLayer,
        "resolve_activation": resolve_activation, "_ACTIVATION_MAP": _ACTIVATION_MAP,
    }


def _slot_attrs(parent_module_path: str = "blocks.0", layer_idx: int = 0) -> dict:
    """合成 transformer_layer slot 字段（flat 的 TinyBlock = norm1+attn+norm2+ffn+2residual）。"""
    return dict(
        layer_idx=layer_idx, kind="transformer_layer",
        in_dim=32, out_dim=32, num_heads=4, head_dim=8,
        original_intermediate=64, activation="gelu", max_seq_len=16,
        parent_module_path=parent_module_path,
        source_class="TinyBlock", forward_arity="single", return_arity="single",
        mask_load_bearing=False, ffn_struct="standard", norm_type="layernorm",
    )


def _run(script: str, args: list[str]) -> tuple[int, str, str]:
    cmd = [sys.executable, str(_SCRIPTS_DIR / script), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(
            f"{script} 失败 rc={proc.returncode}\nargs={args}\nSTDERR:\n{proc.stderr}\nSTDOUT:\n{proc.stdout}"
        )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_result_json(stdout: str) -> dict:
    for line in reversed([ln for ln in stdout.splitlines() if ln.strip()]):
        if line.startswith("RESULT_JSON:"):
            return json.loads(line.split(":", 1)[1].strip())
    raise AssertionError(f"stdout 无法解析 RESULT_JSON：\n{stdout}")


def _assert_self_contained(optimized_flat: Path) -> None:
    """optimized_flat 必须无 puzzle_blocks / nas_agent / transformer_layer_variants import。"""
    src = optimized_flat.read_text(encoding="utf-8")
    import_lines = [ln for ln in src.splitlines()
                    if ln.strip().startswith(("import ", "from "))]
    bad = [ln for ln in import_lines
           if "puzzle_blocks" in ln or "nas_agent" in ln or "transformer_layer_variants" in ln]
    assert not bad, f"optimized_flat 必须自包含（无 puzzle/nas_agent/tlv import）：{bad}"


def _run_subproc(snippet: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True)


# ── dispatcher 静态测试（mirror vs 变体源 factory；不依赖 e2e）───────────────────

def test_dispatcher_all_layer_variants_match_source_keys() -> None:
    """每个 layer variant：_variant_dispatcher_src 生成的 _build_variant 产出 state_dict keys
    必与 transformer_layer_variants.make_*_layer（catalog factory，不 wrap）产出一致。

    防 _VARIANT_CONSTRUCTION/dispatcher 与 transformer_layer_variants.make_*_layer 数据漂移——
    6 分支不靠 e2e 覆盖，直接静态 exec 生成的 dispatcher 代码对比变体源 factory。
    """
    from types import SimpleNamespace

    import materialize_optimized as mz
    import transformer_layer_variants as tlv

    ns = _dispatcher_ns()
    all_variants = set(mz._VARIANT_CONSTRUCTION.keys())
    exec(compile(mz._variant_dispatcher_src(all_variants), "<dispatcher>", "exec"), ns)

    slot_attrs = _slot_attrs()
    slot_obj = SimpleNamespace(**slot_attrs)
    slot_dict = dict(slot_attrs)  # dispatcher 用 dict（optimized_flat 的 _BLOCK_MAP_SLOTS）

    for variant in sorted(all_variants):
        ref = getattr(tlv, f"make_{variant}")(slot_obj)
        ref_keys = set(ref.state_dict().keys())
        mirror = ns["_build_variant"](variant, slot_dict)
        mirror_keys = set(mirror.state_dict().keys())
        assert mirror_keys == ref_keys, (
            f"variant={variant}: dispatcher 镜像 keys ≠ 变体源 factory keys\n"
            f"  mirror_only={mirror_keys - ref_keys}\n  ref_only={ref_keys - mirror_keys}"
        )


def test_dispatcher_random_synthesizer_rejects_missing_max_seq_len() -> None:
    """random_synthesizer_layer 缺 max_seq_len 必 fail loud（禁 fallback——spec-reviewer LV-7）。"""
    import materialize_optimized as mz

    ns = _dispatcher_ns()
    exec(compile(mz._variant_dispatcher_src({"random_synthesizer_layer"}), "<d>", "exec"), ns)
    slot = _slot_attrs()
    slot["max_seq_len"] = None
    with pytest.raises(ValueError, match="max_seq_len"):
        ns["_build_variant"]("random_synthesizer_layer", slot)


def test_dispatcher_edge_cases_empty_and_noop_only() -> None:
    """dispatcher 边界：全 identity 桩 + no_op-only（早返回，无 _PreLNTransformerLayer 不可达落地）。"""
    import materialize_optimized as mz

    # 全 identity → 桩 dispatcher，调用即 raise（永不履行）
    ns_empty = _dispatcher_ns()
    exec(compile(mz._variant_dispatcher_src(set()), "<d>", "exec"), ns_empty)
    with pytest.raises(ValueError, match="全 identity"):
        ns_empty["_build_variant"]("vanilla_layer", _slot_attrs())

    # no_op-only → 早返回 _NoOpLayer；未知 variant 进 else raise
    ns_noop = _dispatcher_ns()
    exec(compile(mz._variant_dispatcher_src({"no_op_layer"}), "<d>", "exec"), ns_noop)
    mod = ns_noop["_build_variant"]("no_op_layer", _slot_attrs())
    assert mod.state_dict() == {}  # _NoOpLayer 零参
    with pytest.raises(ValueError, match="未知 variant"):
        ns_noop["_build_variant"]("vanilla_layer", _slot_attrs())


def test_dispatcher_rejects_unknown_variant() -> None:
    """生成的 dispatcher 对未登记 variant 进 else raise（fail loud）。"""
    import materialize_optimized as mz

    ns = _dispatcher_ns()
    exec(compile(mz._variant_dispatcher_src({"vanilla_layer"}), "<d>", "exec"), ns)
    with pytest.raises(ValueError, match="未知 variant"):
        ns["_build_variant"]("definitely_not_a_real_layer", _slot_attrs())


# ── materialize 端到端（layer 粒度 mock fixture）─────────────────────────────────

def _bootstrap_materialize(
    tmp_path: Path, variant: str, layer_idx: int = 1, build_cfg: str = "",
    selected_model: str | None = None,
) -> dict:
    """build_selected + materialize 端到端，返回 {res, optimized_flat, selected_model, flat, father}。

    selected_model=None → 跳过 build_selected（用空字串占位；key_alignment 仍跑，因 build_student_from_arch
    内部用空 block_library 随机 init variant）。selected_model="build" → 先 build_selected 再 materialize。
    """
    paths = write_flat_and_adapters(tmp_path, num_blocks=2)
    flat, adapters, father = paths["flat"], paths["adapters"], paths["father"]
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)
    block_library_dir = output_dir / "block_library"
    block_library_dir.mkdir(parents=True, exist_ok=True)

    parent_path = f"blocks.{layer_idx}"
    block_map_path = output_dir / "block_map.json"
    block_map_path.write_text(json.dumps({"slots": [_slot_attrs(parent_path, layer_idx)]}),
                              encoding="utf-8")
    selected_arch_path = output_dir / "selected_arch.json"
    selected_arch_path.write_text(json.dumps({"selected_arch": {
        str(layer_idx): {"transformer_layer": variant}
    }}), encoding="utf-8")

    sel_model_arg = ""
    if selected_model == "build":
        _run("build_selected.py", [
            "--selected_arch", str(selected_arch_path), "--block_map", str(block_map_path),
            "--build_fn", "build_model", "--build_cfg", build_cfg, "--flat_model", str(flat),
            "--block_library", str(block_library_dir), "--adapters", str(adapters),
            "--output_dir", str(output_dir),
        ])
        sel_model_arg = str(output_dir / "selected_model.pt")

    _, out, _ = _run("materialize_optimized.py", [
        "--flat_model", str(flat), "--build_fn", "build_model", "--build_cfg", build_cfg,
        "--selected_arch", str(selected_arch_path), "--block_map", str(block_map_path),
        "--selected_model", sel_model_arg, "--adapters", str(adapters),
        "--block_library", str(block_library_dir),
        "--output_dir", str(output_dir), "--base_name", "layer",
    ])
    return {
        "res": _parse_result_json(out),
        "optimized_flat": output_dir / "layer_optimized_flat.py",
        "selected_model": output_dir / "selected_model.pt",
        "flat": flat, "father": father, "output_dir": output_dir,
    }


@pytest.mark.parametrize("variant", [
    "vanilla_layer", "random_synthesizer_layer", "relu_attention_layer",
    "fnet_layer", "softs_star_layer",
])
def test_materialize_layer_each_variant(tmp_path: Path, variant: str) -> None:
    """全 5 真 attention layer variant 端到端：materialize 自检 + 自包含 + build_model shape + load_model strict。

    parametrize 覆盖每个**可选** variant 的 _apply_selected_arch 真装配路径（不只静态 key 对齐）——
    softs_star core_dim 烘入、random_synthesizer max_seq_len 边界全验。F1：no_op_layer 已退出 MIP
    候选集（build_selected → catalog → raise），故不经 build_selected 端到端；其 dispatcher 装配分支
    由 test_dispatcher_edge_cases_empty_and_noop_only 直接 exec 覆盖（不经 catalog）。
    """
    r = _bootstrap_materialize(tmp_path, variant, layer_idx=1, selected_model="build")
    res = r["res"]
    assert res["status"] == "executed", f"{variant}: materialize 自检失败：{res}"
    assert res["key_alignment_passed"] is True, f"{variant}: {res['key_alignment_detail']}"
    assert res["forward_selfcheck_passed"] is True, f"{variant}: {res['forward_selfcheck_detail']}"

    optimized_flat = r["optimized_flat"]
    assert optimized_flat.is_file()
    _assert_self_contained(optimized_flat)

    # build_model forward shape 保持（替换层 I/O 维度不变；head 输出 [B, num_classes=10]）
    probe = _run_subproc(
        "import importlib.util as u, torch\n"
        f"s=u.spec_from_file_location('o',r'{optimized_flat}'); m=u.module_from_spec(s); s.loader.exec_module(m)\n"
        "model=m.build_model().eval(); x=torch.randn(2,16,32)\n"
        "with torch.no_grad(): y=model(x)\n"
        f"assert y.shape==(2,{_DEFAULT_HEAD_DIM}), 'shape mismatch'\n"
        "assert torch.isfinite(y).all(), 'NaN/inf'\n"
        "print('SHAPE_OK')"
    )
    assert probe.returncode == 0 and "SHAPE_OK" in probe.stdout, (
        f"{variant}: build_model forward 失败：{probe.stderr or probe.stdout}"
    )

    # load_model strict（交付权重路径：selected_model 同 variant 结构）
    load_check = _run_subproc(
        f"import importlib.util as u; s=u.spec_from_file_location('o',r'{optimized_flat}');"
        f"m=u.module_from_spec(s); s.loader.exec_module(m);"
        f"m.load_model(r'{r['selected_model']}'); print('LOAD_OK')"
    )
    assert load_check.returncode == 0 and "LOAD_OK" in load_check.stdout, (
        f"{variant}: load_model strict 失败：{load_check.stderr}"
    )


def test_materialize_unknown_variant_rejected(tmp_path: Path) -> None:
    """main() 对 selected_arch 里未登记的 variant fail loud（非零 exit + stderr 含「未支持的 variant」）。"""
    paths = write_flat_and_adapters(tmp_path, num_blocks=2)
    flat, adapters = paths["flat"], paths["adapters"]
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "block_library").mkdir(parents=True, exist_ok=True)
    block_map_path = output_dir / "block_map.json"
    block_map_path.write_text(json.dumps({"slots": [_slot_attrs("blocks.1", 1)]}), encoding="utf-8")
    selected_arch_path = output_dir / "selected_arch.json"
    selected_arch_path.write_text(json.dumps({"selected_arch": {
        "1": {"transformer_layer": "definitely_not_a_real_layer"}
    }}), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "materialize_optimized.py"),
         "--flat_model", str(flat), "--build_fn", "build_model", "--build_cfg", "",
         "--selected_arch", str(selected_arch_path), "--block_map", str(block_map_path),
         "--adapters", str(adapters), "--block_library", str(output_dir / "block_library"),
         "--output_dir", str(output_dir), "--base_name", "bad"],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0, "未知 variant 应非零 exit"
    assert "未支持的 variant" in proc.stderr, f"stderr 应点名未支持的 variant：\n{proc.stderr}"


def test_materialize_build_cfg_baked(tmp_path: Path) -> None:
    """build_cfg 烘入：_BUILD_CFG 烧进 optimized_flat + build_model 真用之（head num_classes=5）。

    father 也用 num_classes=5 构建（同 cfg），免 build_student_from_arch 内 load_pretrained shape mismatch。
    """
    import importlib.util

    import torch

    from _puzzle_test_fixtures import TINY_FLAT_PY

    build_cfg = '{"num_classes": 5}'
    # 先写 flat + 用 num_classes=5 构建 father（与 build_cfg 一致）
    flat_path = tmp_path / "tiny_flat.py"
    flat_path.write_text(TINY_FLAT_PY, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    spec = importlib.util.spec_from_file_location("_flat_cfg", flat_path)
    fmod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fmod)
    father5 = tmp_path / "father5.pth"
    torch.save(fmod.build_model(num_blocks=2, num_classes=5).state_dict(), father5)
    paths = write_flat_and_adapters(tmp_path, father_ckpt_path=father5)
    flat, adapters = paths["flat"], paths["adapters"]

    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)
    block_library_dir = output_dir / "block_library"
    block_library_dir.mkdir(parents=True, exist_ok=True)
    block_map_path = output_dir / "block_map.json"
    block_map_path.write_text(json.dumps({"slots": [_slot_attrs("blocks.1", 1)]}), encoding="utf-8")
    selected_arch_path = output_dir / "selected_arch.json"
    selected_arch_path.write_text(json.dumps({"selected_arch": {
        "1": {"transformer_layer": "vanilla_layer"}
    }}), encoding="utf-8")

    _, out, _ = _run("materialize_optimized.py", [
        "--flat_model", str(flat), "--build_fn", "build_model", "--build_cfg", build_cfg,
        "--selected_arch", str(selected_arch_path), "--block_map", str(block_map_path),
        "--selected_model", "", "--adapters", str(adapters),
        "--block_library", str(block_library_dir), "--output_dir", str(output_dir), "--base_name", "cfg",
    ])
    res = _parse_result_json(out)
    assert res["status"] == "executed", f"build_cfg materialize 失败：{res}"

    optimized_flat = output_dir / "cfg_optimized_flat.py"
    src = optimized_flat.read_text(encoding="utf-8")
    assert "_BUILD_CFG = {'num_classes': 5}" in src, "build_cfg 未烘入 optimized_flat"

    # build_model 用了 cfg → head 输出 5（非默认 10）
    probe = _run_subproc(
        "import importlib.util as u, torch\n"
        f"s=u.spec_from_file_location('o',r'{optimized_flat}'); m=u.module_from_spec(s); s.loader.exec_module(m)\n"
        "with torch.no_grad(): y=m.build_model().eval()(torch.randn(2,16,32))\n"
        "assert y.shape==(2,5), 'shape mismatch'\n"
        "print('CFG_OK')"
    )
    assert probe.returncode == 0 and "CFG_OK" in probe.stdout, (
        f"build_cfg 未生效：{probe.stderr or probe.stdout}"
    )


def test_materialize_all_identity_zero_invasion(tmp_path: Path) -> None:
    """全 identity selected_arch：optimized_flat.load_model(father) forward 必与父模型 allclose（零侵入）。

    layer 粒度的 identity 对偶 block 粒度 test_materialize_identity_allclose——slot 不替换 →
    optimized 架构与父同构，同权重 forward 必一致。覆盖 _apply_selected_arch 的 identity continue 分支。
    """
    paths = write_flat_and_adapters(tmp_path, num_blocks=2)
    flat, adapters, father = paths["flat"], paths["adapters"], paths["father"]
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "block_library").mkdir(parents=True, exist_ok=True)
    block_map_path = output_dir / "block_map.json"
    block_map_path.write_text(json.dumps({"slots": [_slot_attrs("blocks.1", 1)]}), encoding="utf-8")
    selected_arch_path = output_dir / "selected_arch.json"
    selected_arch_path.write_text(json.dumps({"selected_arch": {
        "1": {"transformer_layer": "identity"}
    }}), encoding="utf-8")
    _run("materialize_optimized.py", [
        "--flat_model", str(flat), "--build_fn", "build_model", "--build_cfg", "",
        "--selected_arch", str(selected_arch_path), "--block_map", str(block_map_path),
        "--adapters", str(adapters), "--block_library", str(output_dir / "block_library"),
        "--output_dir", str(output_dir), "--base_name", "ident",
    ])
    optimized_flat = output_dir / "ident_optimized_flat.py"
    probe = _run_subproc(
        "import importlib.util as u, torch\n"
        f"so=u.spec_from_file_location('o',r'{optimized_flat}'); mo=u.module_from_spec(so); so.loader.exec_module(mo)\n"
        f"sf=u.spec_from_file_location('f',r'{flat}'); mf=u.module_from_spec(sf); sf.loader.exec_module(mf)\n"
        f"a=mo.load_model(r'{father}').eval()\n"
        f"b=mf.build_model().eval(); b.load_state_dict(torch.load(r'{father}',map_location='cpu'),strict=True)\n"
        "x=torch.randn(2,16,32)\n"
        "with torch.no_grad(): oa=a(x); ob=b(x)\n"
        "print('CLOSE' if torch.allclose(oa,ob,atol=1e-5) else 'DIFF')"
    )
    assert probe.returncode == 0 and "CLOSE" in probe.stdout, (
        f"全 identity optimized 应与父 allclose（零侵入）：{probe.stderr or probe.stdout}"
    )


def test_check_key_alignment_detects_shape_mismatch() -> None:
    """_check_key_alignment 检 shape_mismatch——catalog core_dim 漂移的装配期 fail loud 兜底。"""
    import torch.nn as nn

    import materialize_optimized as mz

    class _FakeMod:
        def build_model(self):
            return nn.Sequential(nn.Linear(4, 8))  # weight shape [8,4]

    # reference 同名 key 但 shape [16,4] → shape_mismatch
    reference = nn.Sequential(nn.Linear(4, 16)).state_dict()
    ok, detail = mz._check_key_alignment(_FakeMod(), reference, "shape-drift-ref")
    assert ok is False
    assert "shape_mismatch" in detail, f"detail 应含 shape_mismatch：{detail}"


def test_materialize_layer_idempotent(tmp_path: Path) -> None:
    """两次 materialize 产出的 optimized_flat.py 字节级一致（确定性装配，layer 粒度）。"""
    import hashlib

    paths = write_flat_and_adapters(tmp_path, num_blocks=2)
    flat, adapters = paths["flat"], paths["adapters"]
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)
    block_library_dir = output_dir / "block_library"
    block_library_dir.mkdir(parents=True, exist_ok=True)
    block_map_path = output_dir / "block_map.json"
    block_map_path.write_text(json.dumps({"slots": [_slot_attrs("blocks.0", 0)]}), encoding="utf-8")
    selected_arch_path = output_dir / "selected_arch.json"
    selected_arch_path.write_text(json.dumps({"selected_arch": {
        "0": {"transformer_layer": "fnet_layer"}
    }}), encoding="utf-8")

    common = ["--flat_model", str(flat), "--build_fn", "build_model", "--build_cfg", "",
              "--selected_arch", str(selected_arch_path), "--block_map", str(block_map_path),
              "--adapters", str(adapters), "--block_library", str(block_library_dir)]
    _run("materialize_optimized.py", [*common, "--output_dir", str(tmp_path / "a"), "--base_name", "t"])
    _run("materialize_optimized.py", [*common, "--output_dir", str(tmp_path / "b"), "--base_name", "t"])
    h1 = hashlib.md5((tmp_path / "a" / "t_optimized_flat.py").read_bytes()).hexdigest()
    h2 = hashlib.md5((tmp_path / "b" / "t_optimized_flat.py").read_bytes()).hexdigest()
    assert h1 == h2, "materialize 必须幂等（确定性装配，两次产出 md5 应一致）"
