"""test_puzzle_materialize.py —— pz_materialize 确定性装配 + 不变量测试。

覆盖 docs/plans/2026-08-13-puzzle-materialize-optimized-flat.md §6 死不变量：
  1. status=executed + key_alignment_passed（optimized_flat vs build_student_from_arch 逐 key+shape 对齐）。
  2. forward_selfcheck_passed（standalone 子进程 forward 用项目真实签名）。
  3. 自包含：optimized_flat.py 无 puzzle_blocks / nas_agent import（仅 torch + stdlib）。
  4. load_model(selected_model.pt) strict 载入（交付权重路径）。
  5. 幂等：两次 materialize 产出的文件字节级一致（md5）。
  6. identity-only 架构：optimized_flat forward 与父模型 allclose（零侵入）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("torch")
pytest.importorskip("pulp")
pytest.importorskip("nas_agent")

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _REPO / "workflows" / "agents" / "_puzzle_scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_REPO / "tests"))

from _puzzle_test_fixtures import (  # noqa: E402
    search_space_payload,
    write_flat_and_adapters,
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


def _bootstrap(tmp_path: Path, num_blocks: int = 2) -> dict[str, Path]:
    """measure_baseline bootstrap → flat + adapters + block_map + baseline_metrics。"""
    import yaml
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = write_flat_and_adapters(tmp_path, num_blocks=num_blocks)
    ss_path = output_dir / "search_space.yaml"
    with open(ss_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(search_space_payload(num_blocks), f, allow_unicode=True, sort_keys=False)
    _, out, _ = _run("measure_baseline.py", [
        "--flat_path", str(paths["flat"]), "--build_fn", "build_model", "--build_cfg", "",
        "--adapters", str(paths["adapters"]), "--search_space_path", str(ss_path),
        "--latency_unit", "ms", "--output_dir", str(output_dir), "--seed", "0",
        "--latency_reduction_target", "0",  # bypass feasibility（本测试关注 materialize，非 LAT）
    ])
    res = _parse_result_json(out)
    assert res["model_type_supported"] is True
    return {
        "flat": paths["flat"], "adapters": paths["adapters"],
        "father": paths["father"],
        "block_map": output_dir / "block_map.json",
        "baseline_metrics": output_dir / "baseline_metrics.json",
        "output_dir": output_dir,
    }


def _build_library_and_select(paths: dict[str, Path], candidates: dict) -> Path:
    """bld → score → latency_table → mip_select，返 selected_arch.json 路径。"""
    output_dir = paths["output_dir"]
    block_map = paths["block_map"]
    build_args = ["--build_fn", "build_model", "--build_cfg", "",
                  "--flat_model", str(paths["flat"]), "--adapters", str(paths["adapters"])]
    block_library_dir = output_dir / "block_library"
    _run("bld.py", ["--block_map", str(block_map), *build_args,
                    "--block_candidates", json.dumps(candidates),
                    "--epochs", "1", "--output_dir", str(output_dir)])
    _run("score.py", ["--block_map", str(block_map), *build_args,
                      "--block_library", str(block_library_dir), "--output_dir", str(output_dir)])
    _run("latency_table.py", ["--block_map", str(block_map), *build_args,
                              "--block_library", str(block_library_dir),
                              "--output_dir", str(output_dir)])
    baseline = json.loads(paths["baseline_metrics"].read_text())
    _run("mip_select.py", ["--scores", str(output_dir / "scores.jsonl"),
                           "--latency-table", str(output_dir / "latency_table.jsonl"),
                           "--target-latency", str(baseline["baseline_latency"] * 2.0),
                           "--latency-unit", "ms", "--output_dir", str(output_dir)])
    return output_dir / "selected_arch.json"


@pytest.mark.skip(reason="block granularity superseded by layer-variant refactor; layer materialize covered by tests/test_materialize_layer.py + L5 target E2E")
@pytest.mark.slow
def test_materialize_key_alignment_and_self_contained(tmp_path: Path) -> None:
    """materialize 产 optimized_flat：key 对齐 + standalone forward + 自包含 + load_model。"""
    paths = _bootstrap(tmp_path)
    selected_arch_path = _build_library_and_select(
        paths, {"attention": ["identity", "fnet"], "ffn": ["identity", "ffn_50"]}
    )
    output_dir = paths["output_dir"]
    block_library_dir = output_dir / "block_library"

    # build_selected → selected_model.pt（父⊕BLD）
    _run("build_selected.py", [
        "--selected_arch", str(selected_arch_path), "--block_map", str(paths["block_map"]),
        "--build_fn", "build_model", "--build_cfg", "", "--flat_model", str(paths["flat"]),
        "--block_library", str(block_library_dir), "--adapters", str(paths["adapters"]),
        "--output_dir", str(output_dir),
    ])
    selected_model_path = output_dir / "selected_model.pt"
    assert selected_model_path.is_file()

    # materialize
    _, out, _ = _run("materialize_optimized.py", [
        "--flat_model", str(paths["flat"]), "--build_fn", "build_model", "--build_cfg", "",
        "--selected_arch", str(selected_arch_path), "--block_map", str(paths["block_map"]),
        "--selected_model", str(selected_model_path), "--adapters", str(paths["adapters"]),
        "--block_library", str(block_library_dir), "--output_dir", str(output_dir),
        "--base_name", "tiny",
    ])
    res = _parse_result_json(out)
    assert res["status"] == "executed", f"materialize 自检失败：{res}"
    assert res["key_alignment_passed"] is True, res["key_alignment_detail"]
    assert res["forward_selfcheck_passed"] is True, res["forward_selfcheck_detail"]

    optimized_flat = output_dir / "tiny_optimized_flat.py"
    assert optimized_flat.is_file()

    # 自包含：无 puzzle_blocks / nas_agent 的 import 依赖（仅 torch + stdlib）
    # 注：内联 helper 的 docstring 可能提到 "puzzle_blocks" 字样（非依赖），故只查 import 语句。
    src = optimized_flat.read_text(encoding="utf-8")
    import_lines = [ln for ln in src.splitlines()
                    if ln.strip().startswith(("import ", "from "))]
    bad = [ln for ln in import_lines
           if "puzzle_blocks" in ln or "nas_agent" in ln]
    assert not bad, f"optimized_flat 必须自包含（无 puzzle_blocks/nas_agent import）：{bad}"

    # load_model strict 载入（交付权重路径）
    load_check = subprocess.run(
        [sys.executable, "-c",
         f"import importlib.util as u; s=u.spec_from_file_location('o',r'{optimized_flat}');"
         f"m=u.module_from_spec(s); s.loader.exec_module(m);"
         f"m.load_model(r'{selected_model_path}'); print('LOAD_OK')"],
        capture_output=True, text=True,
    )
    assert load_check.returncode == 0 and "LOAD_OK" in load_check.stdout, (
        f"load_model strict 失败：{load_check.stderr}"
    )


@pytest.mark.skip(reason="block granularity superseded by layer-variant refactor; layer materialize covered by tests/test_materialize_layer.py + L5 target E2E")
@pytest.mark.slow
def test_materialize_idempotent(tmp_path: Path) -> None:
    """两次 materialize 产出的 optimized_flat.py 字节级一致（md5）。"""
    paths = _bootstrap(tmp_path)
    selected_arch_path = _build_library_and_select(
        paths, {"attention": ["identity", "fnet"], "ffn": ["identity", "ffn_50"]}
    )
    output_dir = paths["output_dir"]
    block_library_dir = output_dir / "block_library"
    _run("build_selected.py", [
        "--selected_arch", str(selected_arch_path), "--block_map", str(paths["block_map"]),
        "--build_fn", "build_model", "--build_cfg", "", "--flat_model", str(paths["flat"]),
        "--block_library", str(block_library_dir), "--adapters", str(paths["adapters"]),
        "--output_dir", str(output_dir),
    ])
    common = ["--flat_model", str(paths["flat"]), "--build_fn", "build_model", "--build_cfg", "",
              "--selected_arch", str(selected_arch_path), "--block_map", str(paths["block_map"]),
              "--selected_model", str(output_dir / "selected_model.pt"),
              "--adapters", str(paths["adapters"]), "--block_library", str(block_library_dir)]
    _run("materialize_optimized.py", [*common, "--output_dir", str(tmp_path / "a"), "--base_name", "t"])
    _run("materialize_optimized.py", [*common, "--output_dir", str(tmp_path / "b"), "--base_name", "t"])
    h1 = hashlib.md5((tmp_path / "a" / "t_optimized_flat.py").read_bytes()).hexdigest()
    h2 = hashlib.md5((tmp_path / "b" / "t_optimized_flat.py").read_bytes()).hexdigest()
    assert h1 == h2, "materialize 必须幂等（确定性装配，两次产出 md5 应一致）"


@pytest.mark.skip(reason="block granularity superseded by layer-variant refactor; layer materialize covered by tests/test_materialize_layer.py + L5 target E2E")
@pytest.mark.slow
def test_materialize_identity_allclose(tmp_path: Path) -> None:
    """全 identity 架构：optimized_flat forward 必与父模型 allclose（零侵入承诺）。"""
    # num_blocks=2 匹配 build_model 默认（build_selected 经 build_model() 零参实例化 father）。
    paths = _bootstrap(tmp_path, num_blocks=2)
    # 手写全 identity selected_arch（绕过 MIP，确保每 slot 都 identity）
    output_dir = paths["output_dir"]
    selected_arch_path = output_dir / "selected_arch.json"
    selected_arch_path.write_text(json.dumps({
        "selected_arch": {
            "0": {"attention": "identity", "ffn": "identity"},
            "1": {"attention": "identity", "ffn": "identity"},
        },
        "total_score": 0.0, "selected_latency": 0.0, "feasible": True,
        "select_reason": "mip-optimal", "latency_unit": "ms",
    }))
    block_library_dir = output_dir / "block_library"
    block_library_dir.mkdir(parents=True, exist_ok=True)
    _run("build_selected.py", [
        "--selected_arch", str(selected_arch_path), "--block_map", str(paths["block_map"]),
        "--build_fn", "build_model", "--build_cfg", "", "--flat_model", str(paths["flat"]),
        "--block_library", str(block_library_dir), "--adapters", str(paths["adapters"]),
        "--output_dir", str(output_dir),
    ])
    _, out, _ = _run("materialize_optimized.py", [
        "--flat_model", str(paths["flat"]), "--build_fn", "build_model", "--build_cfg", "",
        "--selected_arch", str(selected_arch_path), "--block_map", str(paths["block_map"]),
        "--selected_model", str(output_dir / "selected_model.pt"),
        "--adapters", str(paths["adapters"]), "--block_library", str(block_library_dir),
        "--output_dir", str(output_dir), "--base_name", "tiny",
    ])
    res = _parse_result_json(out)
    assert res["status"] == "executed", f"全 identity materialize 失败：{res}"

    # allclose：optimized_flat.load_model(father.pth) vs 父 build_model()+载 father.pth ——
    # 同权重 + 全 identity（零侵入）→ forward 必一致（比随机 init 才有意义）。
    optimized_flat = output_dir / "tiny_optimized_flat.py"
    father_flat = paths["flat"]
    father_ckpt = paths["father"]
    probe = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util as u, torch\n"
         f"so=u.spec_from_file_location('o',r'{optimized_flat}'); mo=u.module_from_spec(so); so.loader.exec_module(mo)\n"
         f"sf=u.spec_from_file_location('f',r'{father_flat}'); mf=u.module_from_spec(sf); sf.loader.exec_module(mf)\n"
         f"a=mo.load_model(r'{father_ckpt}').eval()\n"
         f"b=mf.build_model().eval(); b.load_state_dict(torch.load(r'{father_ckpt}',map_location='cpu'),strict=True)\n"
         "x=torch.randn(2,16,32)\n"
         "with torch.no_grad(): oa=a(x); ob=b(x)\n"
         "print('CLOSE' if torch.allclose(oa,ob,atol=1e-5) else 'DIFF')"],
        capture_output=True, text=True,
    )
    assert probe.returncode == 0 and "CLOSE" in probe.stdout, (
        f"全 identity 架构 optimized 应与父 allclose（零侵入）：{probe.stderr or probe.stdout}"
    )
