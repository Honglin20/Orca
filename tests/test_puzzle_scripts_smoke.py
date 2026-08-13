"""test_puzzle_scripts_smoke.py —— Puzzle U6 端到端链路 smoke test。

U6 改造：合成 flat + 合成 adapters（不接 target 项目真码）。跑全链：
measure_baseline → bld → score → latency_table → mip → build → gkd → gate，
断言每步产物文件存在 + AC 字段类型正确 + final_status.json 落盘。
"""

from __future__ import annotations

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
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    for line in reversed(lines):
        if line.startswith("RESULT_JSON:"):
            return json.loads(line.split(":", 1)[1].strip())
    if lines:
        try:
            return json.loads(lines[-1].strip())
        except json.JSONDecodeError:
            pass
    raise AssertionError(f"stdout 无法解析 JSON：\n{stdout}")


def _bootstrap_measure_baseline(tmp_path: Path, output_dir: Path, num_blocks: int = 2) -> dict[str, Path]:
    """模拟 pz_expand LLM 产物 + 跑 measure_baseline.py 完成基线测量。"""
    import yaml
    paths = write_flat_and_adapters(tmp_path, num_blocks=num_blocks)
    ss_path = output_dir / "search_space.yaml"
    with open(ss_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(search_space_payload(num_blocks), f, allow_unicode=True, sort_keys=False)

    rc, out, err = _run("measure_baseline.py", [
        "--flat_path", str(paths["flat"]),
        "--build_fn", "build_model",
        "--build_cfg", "",
        "--adapters", str(paths["adapters"]),
        "--search_space_path", str(ss_path),
        "--latency_unit", "ms",
        "--output_dir", str(output_dir),
        "--seed", "0",
        # 下游 stage 测试不关心 latency 可行性（合成 fixture 太小、block 占比波动大）；
        # target=0 让 feasibility 检查恒过（max_reduction >= 0 - tol 恒成立），专注于
        # 下游 stage 各脚本本身的行为。
        "--latency_reduction_target", "0",
    ])
    result = _parse_result_json(out)
    assert result["model_type_supported"] is True, (
        f"measure_baseline bootstrap 失败：{result}\nSTDERR:\n{err}"
    )
    # 弱烟雾断言（target=0 bypass → feasibility 应过 + floor 字段落盘）——防 floor 逻辑
    # 彻底坏掉时静默回归。下游 stage 测试本不关心 feasibility，但 bootstrap 应产出可信基线。
    assert result.get("latency_target_feasible") is True, (
        f"target=0 应 feasible（max_reduction >= 0 - tol 恒成立），得 {result.get('latency_target_feasible')}"
    )
    assert result.get("latency_floor", 0) > 0, "floor latency 应 > 0"
    return {
        "flat": paths["flat"],
        "adapters": paths["adapters"],
        "block_map": output_dir / "block_map.json",
        "baseline_metrics": output_dir / "baseline_metrics.json",
        "search_space": output_dir / "search_space.yaml",
        "father_state": output_dir / "father_state_dict.pt",
    }


@pytest.mark.slow
def test_puzzle_full_chain_cpu(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)

    block_candidates = json.dumps({
        "attention": ["identity", "fnet"],
        "ffn": ["identity", "ffn_50"],
    })

    paths = _bootstrap_measure_baseline(tmp_path, output_dir, num_blocks=2)
    block_map_path = paths["block_map"]
    flat_model_path = paths["flat"]
    adapters_path = paths["adapters"]
    baseline_metrics_path = paths["baseline_metrics"]
    build_args = ["--build_fn", "build_model", "--build_cfg", "",
                  "--flat_model", str(flat_model_path),
                  "--adapters", str(adapters_path)]

    # 2. bld
    block_library_dir = output_dir / "block_library"
    _run("bld.py", [
        "--block_map", str(block_map_path),
        *build_args,
        "--block_candidates", block_candidates,
        "--epochs", "1",
        "--output_dir", str(output_dir),
    ])
    assert (output_dir / "bld_summary.json").is_file()
    ckpts = list(block_library_dir.glob("*.pt"))
    assert len(ckpts) >= 4

    # 3. score
    _run("score.py", [
        "--block_map", str(block_map_path),
        *build_args,
        "--block_library", str(block_library_dir),
        "--output_dir", str(output_dir),
    ])
    scores_path = output_dir / "scores.jsonl"
    assert scores_path.is_file()
    assert len(scores_path.read_text().splitlines()) >= 4

    # 4. latency_table
    _run("latency_table.py", [
        "--block_map", str(block_map_path),
        *build_args,
        "--block_library", str(block_library_dir),
        "--output_dir", str(output_dir),
    ])
    latency_path = output_dir / "latency_table.jsonl"
    assert latency_path.is_file()
    assert len(latency_path.read_text().splitlines()) >= 4
    assert (output_dir / "latency_floor.json").is_file()

    # 5. mip_select（reduction=0.5 默认 → target = baseline × 0.5；但 2 层小模型且 identity
    #    主导，故 target 宽松；显式给 target=baseline×2.0 保证 feasible）
    baseline_metrics = json.loads(baseline_metrics_path.read_text())
    target_lat = baseline_metrics["baseline_latency"] * 2.0
    _run("mip_select.py", [
        "--scores", str(scores_path),
        "--latency-table", str(latency_path),
        "--target-latency", str(target_lat),
        "--latency-unit", "ms",
        "--output_dir", str(output_dir),
    ])
    selected_arch_path = output_dir / "selected_arch.json"
    assert selected_arch_path.is_file()
    selected = json.loads(selected_arch_path.read_text())
    assert selected["feasible"] is True
    assert selected["selected_arch"]
    # select_reason 不再含 target-too-aggressive（root cause G）
    assert selected["select_reason"] in {"mip-optimal", "infeasible", "none"}

    # 6. build_selected
    _run("build_selected.py", [
        "--selected_arch", str(selected_arch_path),
        "--block_map", str(block_map_path),
        *build_args,
        "--block_library", str(block_library_dir),
        "--adapters", str(adapters_path),
        "--output_dir", str(output_dir),
    ])
    selected_model_path = output_dir / "selected_model.pt"
    assert selected_model_path.is_file()

    # 6.5 materialize（产 optimized_flat 自包含最优架构；key 对齐 + standalone forward 自检）
    optimized_flat_path = output_dir / "tiny_optimized_flat.py"
    _, mat_out, _ = _run("materialize_optimized.py", [
        "--flat_model", str(flat_model_path),
        "--build_fn", "build_model",
        "--build_cfg", "",
        "--selected_arch", str(selected_arch_path),
        "--block_map", str(block_map_path),
        "--selected_model", str(selected_model_path),
        "--adapters", str(adapters_path),
        "--block_library", str(block_library_dir),
        "--output_dir", str(output_dir),
        "--base_name", "tiny",
    ])
    mat_result = _parse_result_json(mat_out)
    assert mat_result["status"] == "executed", f"materialize 自检失败：{mat_result}"
    assert mat_result["key_alignment_passed"] is True
    assert mat_result["forward_selfcheck_passed"] is True
    assert optimized_flat_path.is_file()

    # 7. gkd_retrain（student 严格走 optimized_flat）
    _run("gkd_retrain.py", [
        "--selected_model", str(selected_model_path),
        "--optimized_flat", str(optimized_flat_path),
        "--adapters", str(adapters_path),
        "--epochs", "1",
        "--output_dir", str(output_dir),
    ])
    final_model_path = output_dir / "runs" / "retrain" / "final_model.pt"
    assert final_model_path.is_file()

    # 8. gate_report（optimized_flat 基底 + adapters.evaluate；latency_reduction_target 默认 0.5）
    _run("gate_report.py", [
        "--final_model", str(final_model_path),
        "--baseline_metrics", str(baseline_metrics_path),
        "--optimized_flat", str(optimized_flat_path),
        "--adapters", str(adapters_path),
        "--latency_unit", "ms",
        "--output_dir", str(output_dir),
    ])
    gate_result_path = output_dir / "gate_result.json"
    assert gate_result_path.is_file()
    gate = json.loads(gate_result_path.read_text())
    assert gate["gate_status"] in {"pass", "fail"}
    assert gate["metric_direction"] == "higher-better"
    assert gate["gate_reason"] in {"both-met", "metric-miss", "latency-miss", "both-miss"}
    # U6 root cause J：final_status.json 落盘
    final_status_path = output_dir / "final_status.json"
    assert final_status_path.is_file()
    fs = json.loads(final_status_path.read_text())
    assert fs["stage"] == "pz_report"
    assert fs["status"] == gate["gate_status"]
    assert set(fs.keys()) >= {"stage", "status", "reason", "metrics"}


# ── MIP infeasible 路径 ───────────────────────────────────────────────────────

def test_mip_select_infeasible(tmp_path: Path) -> None:
    """MIP 预算太紧 → feasible=false, select_reason=infeasible, selected_arch={}。"""
    scores_path = tmp_path / "scores.jsonl"
    latency_path = tmp_path / "latency_table.jsonl"
    with open(scores_path, "w") as f:
        for layer in (0, 1):
            for kind in ("attention", "ffn"):
                for v in ("identity", "other"):
                    f.write(json.dumps({
                        "layer": layer, "kind": kind, "variant": v,
                        "score": -0.5 if v != "identity" else 0.0, "valid": True,
                    }) + "\n")
    with open(latency_path, "w") as f:
        for layer in (0, 1):
            for kind in ("attention", "ffn"):
                for v in ("identity", "other"):
                    f.write(json.dumps({
                        "layer": layer, "kind": kind, "variant": v,
                        "latency_ms": 100.0,
                    }) + "\n")
    (tmp_path / "baseline_metrics.json").write_text(
        json.dumps({"baseline_latency": 500.0, "latency_unit": "ms"})
    )
    cmd = [
        sys.executable, str(_SCRIPTS_DIR / "mip_select.py"),
        "--scores", str(scores_path),
        "--latency-table", str(latency_path),
        "--target-latency", "10.0",
        "--baseline-metrics", str(tmp_path / "baseline_metrics.json"),
        "--output_dir", str(tmp_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"infeasible 是合法 rc=0：STDERR:\n{proc.stderr}"
    result = _parse_result_json(proc.stdout)
    # 加性 infeasible（target 过紧）→ best-effort：返 min-latency arch 让 gate 实测裁决，不空死在 select
    assert result["feasible"] is False
    assert result["select_reason"] == "best-effort"
    assert result["selected_arch"], "best-effort 必须返非空 min-latency arch（gate 实测裁决）"
    assert len(result["selected_arch"]) == 2  # 2 layers each picked min-latency variant


# ── root cause G：mip 不再 target-too-aggressive 早警 ─────────────────────────

def test_mip_select_no_target_too_aggressive_early_terminate(tmp_path: Path) -> None:
    """target_latency > baseline/2 不再硬 terminate（root cause G）——正常跑 MIP。

    构造 target = baseline × 10（远 > baseline/2）：旧逻辑会 target-too-aggressive 早警，
    新逻辑正常跑 MIP（feasible=true，因 target 宽松）。
    """
    scores_path = tmp_path / "scores.jsonl"
    latency_path = tmp_path / "latency_table.jsonl"
    with open(scores_path, "w") as f:
        f.write(json.dumps({"layer": 0, "kind": "attention", "variant": "identity",
                            "score": 0.0, "valid": True}) + "\n")
    with open(latency_path, "w") as f:
        f.write(json.dumps({"layer": 0, "kind": "attention", "variant": "identity",
                            "latency_ms": 1.0}) + "\n")
    (tmp_path / "baseline_metrics.json").write_text(
        json.dumps({"baseline_latency": 10.0})
    )
    cmd = [
        sys.executable, str(_SCRIPTS_DIR / "mip_select.py"),
        "--scores", str(scores_path),
        "--latency-table", str(latency_path),
        "--target-latency", "100.0",  # > baseline/2 = 5.0
        "--baseline-metrics", str(tmp_path / "baseline_metrics.json"),
        "--output_dir", str(tmp_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0
    result = _parse_result_json(proc.stdout)
    assert result["select_reason"] != "target-too-aggressive"
    assert "infeasible_reason" not in result


# ── root cause G：reduction 推导软目标 ────────────────────────────────────────

def test_mip_select_reduction_soft_target(tmp_path: Path) -> None:
    """--target-latency 缺省 → baseline × (1 - reduction) 软目标。"""
    scores_path = tmp_path / "scores.jsonl"
    latency_path = tmp_path / "latency_table.jsonl"
    with open(scores_path, "w") as f:
        f.write(json.dumps({"layer": 0, "kind": "attention", "variant": "identity",
                            "score": 0.0, "valid": True}) + "\n")
    with open(latency_path, "w") as f:
        f.write(json.dumps({"layer": 0, "kind": "attention", "variant": "identity",
                            "latency_ms": 1.0}) + "\n")
    (tmp_path / "baseline_metrics.json").write_text(
        json.dumps({"baseline_latency": 10.0})
    )
    cmd = [
        sys.executable, str(_SCRIPTS_DIR / "mip_select.py"),
        "--scores", str(scores_path),
        "--latency-table", str(latency_path),
        "--latency_reduction_target", "0.7",  # 软目标 = 10 × 0.3 = 3.0
        "--baseline-metrics", str(tmp_path / "baseline_metrics.json"),
        "--output_dir", str(tmp_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"STDERR:\n{proc.stderr}"
    result = _parse_result_json(proc.stdout)
    # 软目标 = 10 × (1 - 0.7) = 3.0；floor 必为 0（baseline - identity=1=9? actually floor=9）→ infeasible
    # 关键校验：target_latency 字段记录了推导值
    assert abs(result["target_latency"] - 3.0) < 1e-6
    assert result["latency_reduction_target"] == 0.7


# ── score identity passthrough = 0 ────────────────────────────────────────────

def test_score_runs_and_identity_passthrough_score_is_zero(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = _bootstrap_measure_baseline(tmp_path, output_dir, num_blocks=2)
    block_map_path = paths["block_map"]
    flat_model_path = paths["flat"]
    adapters_path = paths["adapters"]
    build_args = ["--build_fn", "build_model", "--build_cfg", "",
                  "--flat_model", str(flat_model_path),
                  "--adapters", str(adapters_path)]
    _run("bld.py", [
        "--block_map", str(block_map_path),
        *build_args,
        "--block_candidates",
        json.dumps({"attention": ["identity", "fnet"],
                    "ffn": ["identity", "ffn_50"]}),
        "--epochs", "1",
        "--output_dir", str(output_dir),
    ])
    _run("score.py", [
        "--block_map", str(block_map_path),
        *build_args,
        "--block_library", str(output_dir / "block_library"),
        "--output_dir", str(output_dir),
    ])
    scores_path = output_dir / "scores.jsonl"
    rows = [l for l in scores_path.read_text().splitlines() if l.strip()]
    assert len(rows) >= 4
    identity_rows = [json.loads(r) for r in rows if json.loads(r)["variant"] == "identity"]
    assert identity_rows
    for r in identity_rows:
        assert r["score"] == 0.0


# ── parse_block_candidates 单元 ───────────────────────────────────────────────

def test_parse_block_candidates_unit() -> None:
    from puzzle_common import parse_block_candidates
    d = parse_block_candidates("")
    assert "identity" in d["attention"]
    assert "ffn_50" in d["ffn"]
    assert d["conv"] == ["identity"]
    # 默认集含 mask_aware 候选（masked_vanilla）
    assert "masked_vanilla" in d["attention"]
    d = parse_block_candidates(
        '{"attention": ["identity", "fnet"], "ffn": ["identity", "no_op"]}'
    )
    assert d == {"attention": ["identity", "fnet"], "ffn": ["identity", "no_op"]}
    with pytest.raises(ValueError):
        parse_block_candidates("not json")
    with pytest.raises(ValueError):
        parse_block_candidates('{"attention": ["fnet"], "ffn": ["identity"]}')  # 缺 identity
