"""test_puzzle_scripts_smoke.py —— Puzzle 端到端链路 smoke test。

不接真 fixture——在测试文件内定义最小合成 transformer（2 层 SimpleAttention +
SimpleFFN），跑全链：measure_baseline（v1 expand_model 已退役）→bld(2 variant, 1
epoch)→score→mip→build→gkd(1 epoch, CPU)→gate，断言每步产物文件存在 + AC 字段类型正确。

`pz_expand` 生产链路 = LLM 自适应 flatten + 写 search_space.yaml + 跑 measure_baseline.py。
本测试以合成 flat 文件 + 合成 search_space.yaml 模拟 LLM 产物，再调 measure_baseline.py
完成确定性基线测量，下游 bld/score/mip/build/gkd/gate 跑真实预写脚本。

用 tempfile.TemporaryDirectory 作 output_dir。torch CPU 可用。
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# ``slow`` mark 在 pyproject.toml [tool.pytest.ini_options].markers 注册。
pytest.importorskip("torch")
pytest.importorskip("pulp")        # mip_select.py 硬依赖
pytest.importorskip("nas_agent")   # candidate factories 走 Elastic* 块

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "workflows" / "agents" / "_puzzle_scripts"


# ── 最小合成 transformer（写进临时 model.py 作 puzzle 输入）──────────────────

_TINY_MODEL_PY = textwrap.dedent(
    """
    import torch
    import torch.nn as nn

    DUMMY_INPUT = {"shape": [2, 16, 32], "dtype": "float32"}


    class SimpleAttention(nn.Module):
        def __init__(self, dim: int, num_heads: int):
            super().__init__()
            self.num_heads = num_heads
            self.head_dim = dim // num_heads
            self.qkv = nn.Linear(dim, dim * 3)
            self.proj = nn.Linear(dim, dim)

        def forward(self, x):
            B, L, C = x.shape
            qkv = self.qkv(x).reshape(B, L, 3, self.num_heads, self.head_dim)
            q, k, v = qkv.unbind(dim=2)
            q = q.transpose(1, 2)
            k = k.transpose(1, 2)
            v = v.transpose(1, 2)
            attn = (q @ k.transpose(-2, -1)) * (self.head_dim ** -0.5)
            attn = attn.softmax(dim=-1)
            out = (attn @ v).transpose(1, 2).reshape(B, L, C)
            return self.proj(out)


    class FeedForward(nn.Module):
        def __init__(self, dim: int, hidden: int):
            super().__init__()
            self.fc1 = nn.Linear(dim, hidden)
            self.act = nn.GELU()
            self.fc2 = nn.Linear(hidden, dim)

        def forward(self, x):
            return self.fc2(self.act(self.fc1(x)))


    class TinyBlock(nn.Module):
        def __init__(self, dim: int, num_heads: int, hidden: int):
            super().__init__()
            self.norm1 = nn.LayerNorm(dim)
            self.attn = SimpleAttention(dim, num_heads)
            self.norm2 = nn.LayerNorm(dim)
            self.ffn = FeedForward(dim, hidden)

        def forward(self, x):
            x = x + self.attn(self.norm1(x))
            x = x + self.ffn(self.norm2(x))
            return x


    class TinyTransformer(nn.Module):
        def __init__(self, dim=32, num_heads=4, num_blocks=2, hidden=64, num_classes=10):
            super().__init__()
            self.embed = nn.Linear(32, dim)
            self.blocks = nn.ModuleList([
                TinyBlock(dim, num_heads, hidden) for _ in range(num_blocks)
            ])
            self.norm = nn.LayerNorm(dim)
            self.head = nn.Linear(dim, num_classes)

        def forward(self, x):
            x = self.embed(x)
            for blk in self.blocks:
                x = blk(x)
            x = self.norm(x)
            return self.head(x.mean(dim=1))


    def build_model(dim=32, num_heads=4, num_blocks=2, hidden=64, num_classes=10):
        return TinyTransformer(dim, num_heads, num_blocks, hidden, num_classes)


    def evaluate(model):
        model.eval()
        with torch.no_grad():
            torch.manual_seed(0)
            x = torch.randn(*DUMMY_INPUT["shape"])
            logits = model(x)
            # proxy acc: mean max-softmax confidence
            return float(logits.softmax(-1).max(dim=-1).values.mean().item())


    def build_calib_loader():
        # 测试用 randn calib（合成模型无真实数据集）；生产路径走 manifest 桥接真实 loader（E14）
        from torch.utils.data import DataLoader, TensorDataset
        x = torch.randn(*DUMMY_INPUT["shape"])
        ds = TensorDataset(x)
        return DataLoader(ds, batch_size=x.shape[0])
    """
)


def _run(script: str, args: list[str]) -> tuple[int, str, str]:
    """跑一个 _puzzle_scripts 脚本；返回 (rc, stdout, stderr)。fail loud（rc!=0 raise）。"""
    cmd = [sys.executable, str(_SCRIPTS_DIR / script), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(
            f"{script} 失败 rc={proc.returncode}\n"
            f"args={args}\nSTDERR:\n{proc.stderr}\nSTDOUT:\n{proc.stdout}"
        )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_result_json(stdout: str) -> dict:
    """从 stdout 解析结果 JSON。

    支持两种格式：
    1. 末行 ``RESULT_JSON: {...}`` （measure_baseline/bld/score/build/gkd 等有 KEY:value 遥测的脚本）
    2. 单行 JSON（mip_select/gate_report 等 zero-LLM 确定性脚本的直接转发 stdout）
    """
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    # 先找 RESULT_JSON: 前缀行
    for line in reversed(lines):
        if line.startswith("RESULT_JSON:"):
            return json.loads(line.split(":", 1)[1].strip())
    # 回落：末行作 plain JSON 试解析（zero-LLM 脚本直接输出单行 JSON）
    if lines:
        try:
            return json.loads(lines[-1].strip())
        except json.JSONDecodeError:
            pass
    raise AssertionError(f"stdout 无法解析 JSON：\n{stdout}")


def _search_space_payload(num_blocks: int = 2) -> dict:
    """合成 search_space dict（模拟 pz_expand LLM 产物；in_dim/out_dim 留 -1 待 trace）。"""
    slots = []
    for i in range(num_blocks):
        slots.append({
            "id": f"L{i}_attention", "path": f"blocks.{i}.attn", "kind": "attention",
            "layer_idx": i, "source_class": "SimpleAttention",
            "forward_arity": "single", "return_arity": "single",
            "mask_load_bearing": False, "num_heads": 4, "head_dim": 8,
            "in_dim": -1, "out_dim": -1,
            "kind_evidence": "forward 含 matmul(Q,K^T) 缩放 + softmax",
        })
        slots.append({
            "id": f"L{i}_ffn", "path": f"blocks.{i}.ffn", "kind": "ffn",
            "layer_idx": i, "source_class": "FeedForward",
            "forward_arity": "single", "return_arity": "single",
            "mask_load_bearing": False,
            "original_intermediate": 64, "activation": "gelu", "ffn_struct": "standard",
            "in_dim": -1, "out_dim": -1,
            "kind_evidence": "Linear(fc1)->GELU->Linear(fc2) standard 结构",
        })
    return {
        "slots": slots,
        "candidates": {"attention": ["identity", "fnet", "no_op"],
                       "ffn": ["identity", "ffn_50", "no_op"]},
    }


def _bootstrap_measure_baseline(
    tmp_path: Path, output_dir: Path, num_blocks: int = 2
) -> dict[str, Path]:
    """模拟 pz_expand LLM 产物 + 跑 measure_baseline.py 完成基线测量。

    生产路径下 pz_expand agent（LLM）产 ``<base>_flat.py`` + ``search_space.yaml``，
    再调预写脚本 ``measure_baseline.py`` 做确定性基线测量。本 helper 用合成 flat 文件
    + 合成 search_space.yaml 替代 LLM 产物，调同一脚本完成 setup，让下游 bld/score/...
    能跑真实预写脚本。

    返回路径 dict：flat / block_map / baseline_metrics / search_space / father_state。
    """
    import importlib.util
    import torch  # type: ignore
    import yaml  # type: ignore

    flat_path = output_dir / "model_flat.py"
    flat_path.write_text(_TINY_MODEL_PY, encoding="utf-8")

    # 预训练 father ckpt：build 模型 → save state_dict（strict-load 必双零）
    sys.path.insert(0, str(output_dir))
    spec = importlib.util.spec_from_file_location("_tiny_flat_bootstrap", flat_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    father_ckpt = tmp_path / "father.pth"
    torch.save(mod.build_model().state_dict(), father_ckpt)

    ss_path = output_dir / "search_space.yaml"
    with open(ss_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(_search_space_payload(num_blocks), f, allow_unicode=True, sort_keys=False)

    rc, out, err = _run("measure_baseline.py", [
        "--flat_path", str(flat_path),
        "--build_fn", "build_model",
        "--build_cfg", "",
        "--father_ckpt", str(father_ckpt),
        "--eval_fn", "evaluate",
        "--eval_kind", "classification",
        "--search_space_path", str(ss_path),
        "--latency_unit", "ms",
        "--output_dir", str(output_dir),
        "--seed", "0",
    ])
    result = _parse_result_json(out)
    assert result["model_type_supported"] is True, (
        f"measure_baseline bootstrap 失败：{result}\nSTDERR:\n{err}"
    )
    return {
        "flat": flat_path,
        "block_map": output_dir / "block_map.json",
        "baseline_metrics": output_dir / "baseline_metrics.json",
        "search_space": output_dir / "search_space.yaml",
        "father_state": output_dir / "father_state_dict.pt",
    }


@pytest.mark.slow
def test_puzzle_full_chain_cpu(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 只用 2 variant per slot_type 加速 CPU（identity + 一个真候选）
    block_candidates = json.dumps({
        "attention": ["identity", "fnet"],
        "ffn": ["identity", "ffn_50"],
    })

    build_args = ["--build_fn", "build_model", "--build_cfg", ""]
    eval_args = ["--eval_fn", "evaluate", "--eval_kind", "classification"]

    # 1. measure_baseline（v1 expand_model.py 已退役；pz_expand LLM 产 flat + search_space，
    #    预写脚本 measure_baseline.py 跑确定性基线测量 + 4 道 smoke）
    paths = _bootstrap_measure_baseline(tmp_path, output_dir, num_blocks=2)
    block_map_path = paths["block_map"]
    flat_model_path = paths["flat"]
    baseline_metrics_path = paths["baseline_metrics"]
    with open(block_map_path) as f:
        bm = json.load(f)
    assert len(bm["slots"]) >= 4, f"期望至少 4 个 slot（2 layer × 2 type），得到 {len(bm['slots'])}"

    # 2. bld（1 epoch，加速）
    block_library_dir = output_dir / "block_library"
    rc, out, _ = _run("bld.py", [
        "--block_map", str(block_map_path),
        "--flat_model", str(flat_model_path),
        *build_args,
        "--block_candidates", block_candidates,
        "--calib_loader_fn", f"{flat_model_path}::build_calib_loader",
        "--epochs", "1",
        "--output_dir", str(output_dir),
    ])
    assert (output_dir / "bld_summary.json").is_file()
    ckpts = list(block_library_dir.glob("*.pt"))
    assert len(ckpts) >= 4, f"BLD 应产 ≥4 ckpt，得到 {len(ckpts)}"

    # 3. score
    rc, out, _ = _run("score.py", [
        "--block_map", str(block_map_path),
        "--flat_model", str(flat_model_path),
        *build_args,
        *eval_args,
        "--block_library", str(block_library_dir),
        "--output_dir", str(output_dir),
    ])
    scores_path = output_dir / "scores.jsonl"
    assert scores_path.is_file()
    score_lines = [l for l in scores_path.read_text().splitlines() if l.strip()]
    assert len(score_lines) >= 4

    # 4. latency_table
    rc, out, _ = _run("latency_table.py", [
        "--block_map", str(block_map_path),
        "--flat_model", str(flat_model_path),
        *build_args,
        "--block_library", str(block_library_dir),
        "--output_dir", str(output_dir),
    ])
    latency_path = output_dir / "latency_table.jsonl"
    assert latency_path.is_file()
    lat_lines = [l for l in latency_path.read_text().splitlines() if l.strip()]
    assert len(lat_lines) >= 4

    # 5. mip_select（target_latency 给一个较松的预算，保证 feasible）
    baseline_metrics = json.loads(baseline_metrics_path.read_text())
    target_lat = baseline_metrics["baseline_latency"] * 2.0  # 宽松
    rc, out, _ = _run("mip_select.py", [
        "--scores", str(scores_path),
        "--latency-table", str(latency_path),
        "--target-latency", str(target_lat),
        "--latency-unit", "ms",
        "--output_dir", str(output_dir),
    ])
    selected = _parse_result_json(out)
    assert selected["feasible"] is True, f"MIP 应 feasible（target 宽松）：{selected}"
    assert selected["selected_arch"], f"selected_arch 不应为空：{selected}"
    selected_arch_path = output_dir / "selected_arch.json"
    assert selected_arch_path.is_file()

    # 6. build_selected
    rc, out, _ = _run("build_selected.py", [
        "--selected_arch", str(selected_arch_path),
        "--block_map", str(block_map_path),
        "--flat_model", str(flat_model_path),
        *build_args,
        "--block_library", str(block_library_dir),
        "--output_dir", str(output_dir),
    ])
    selected_model_path = output_dir / "selected_model.pt"
    assert selected_model_path.is_file()

    # 7. gkd_retrain（1 epoch CPU）
    rc, out, _ = _run("gkd_retrain.py", [
        "--selected_model", str(selected_model_path),
        "--flat_model", str(flat_model_path),
        *build_args,
        *eval_args,
        "--block_map", str(block_map_path),
        "--block_library", str(block_library_dir),
        "--epochs", "1",
        "--output_dir", str(output_dir),
    ])
    final_model_path = output_dir / "runs" / "retrain" / "final_model.pt"
    assert final_model_path.is_file()
    progress = (output_dir / "runs" / "retrain" / "progress.jsonl").read_text().splitlines()
    assert len(progress) >= 1

    # 8. gate_report（D5 baseline-dependent ACC AC：脚本 _acc_pass 无条件按 baseline
    #    高/低自动选阈值；--accuracy_tolerance 是 dead 兼容入参（argparse 消费、main body
    #    不读），launcher 不传以保持契约清晰）
    rc, out, _ = _run("gate_report.py", [
        "--final_model", str(final_model_path),
        "--baseline_metrics", str(baseline_metrics_path),
        "--flat_model", str(flat_model_path),
        *build_args,
        "--block_map", str(block_map_path),
        "--block_library", str(block_library_dir),
        *eval_args,
        "--latency_unit", "ms",
        "--output_dir", str(output_dir),
    ])
    gate_result_path = output_dir / "gate_result.json"
    assert gate_result_path.is_file()
    gate = json.loads(gate_result_path.read_text())

    # AC 字段类型校验
    assert gate["gate_status"] in {"pass", "fail"}
    assert isinstance(gate["final_acc"], (int, float))
    assert isinstance(gate["final_latency"], (int, float))
    assert isinstance(gate["baseline_acc"], (int, float))
    assert isinstance(gate["baseline_latency"], (int, float))
    assert isinstance(gate["acc_delta"], (int, float))
    assert isinstance(gate["latency_ratio"], (int, float))
    assert gate["gate_reason"] in {"both-met", "acc-miss", "latency-miss", "both-miss"}
    assert gate["latency_unit"] == "ms"
    assert isinstance(gate["report_path"], str) and gate["report_path"]


# ── MIP infeasible 路径（v1 expand_model fail-loud 路径已退役；empty-slots
#    输入校验由 test_puzzle_measure_baseline.py::test_measure_baseline_empty_slots_exit_2
#    覆盖；旧的「确定性 slot 识别算法拒绝 CNN」intent 已架构性迁到 LLM pz_expand +
#    evaluator 审查层，其召回由 test_puzzle_evaluator_recall.py 测）。


def test_mip_select_infeasible(tmp_path: Path) -> None:
    """MIP 预算太紧 → feasible=false, select_reason=infeasible, selected_arch={}。"""
    scores_path = tmp_path / "scores.jsonl"
    latency_path = tmp_path / "latency_table.jsonl"
    # 2 组（layer,kind），每组 2 variant；latency 都很大
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
    # 4 组 × 100ms = 400ms 最低；target=10ms → 必 infeasible
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
    assert proc.returncode == 0, f"infeasible 是合法结果（rc=0）：STDERR:\n{proc.stderr}"
    result = _parse_result_json(proc.stdout)
    assert result["feasible"] is False
    assert result["select_reason"] == "infeasible"
    assert result["selected_arch"] == {}


def test_score_runs_and_identity_passthrough_score_is_zero(tmp_path: Path) -> None:
    """score.py 跑通 + identity（passthrough）variant 的 score == 0（自比距离为 0）。

    注：真正的「父模型 state_dict 还原不变量」需要 score.py 暴露内部 replace_slot/还原
    API 做前后哈希对比——本测试降级为 rc==0 + identity score==0 校验（间接证明 finally
    还原没崩）。完整的 state_dict 不变量测试留待 score.py 暴露内部 API 后补。
    """
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 复用全链的前两步（measure_baseline + bld）
    paths = _bootstrap_measure_baseline(tmp_path, output_dir, num_blocks=2)
    block_map_path = paths["block_map"]
    flat_model_path = paths["flat"]
    _run("bld.py", [
        "--block_map", str(block_map_path),
        "--flat_model", str(flat_model_path),
        "--build_fn", "build_model",
        "--block_candidates",
        json.dumps({"attention": ["identity", "fnet"],
                    "ffn": ["identity", "ffn_50"]}),
        "--calib_loader_fn", f"{flat_model_path}::build_calib_loader",
        "--epochs", "1",
        "--output_dir", str(output_dir),
    ])

    # 载入父模型（触发 flat model 可 import 校验，并 warm cache）
    sys.path.insert(0, str(_SCRIPTS_DIR))
    from puzzle_common import load_flat_model  # type: ignore
    load_flat_model(flat_model_path, "build_model", "")

    # 跑 score
    _run("score.py", [
        "--block_map", str(block_map_path),
        "--flat_model", str(flat_model_path),
        "--build_fn", "build_model",
        "--eval_fn", "evaluate",
        "--eval_kind", "classification",
        "--block_library", str(output_dir / "block_library"),
        "--output_dir", str(output_dir),
    ])

    # 跑完后：score 已 rc=0（进程内 finally 还原出错会 rc=2）。校验 scores.jsonl
    # 非空 + identity variant 的 score == 0（passthrough 自比距离为 0）。
    scores_path = output_dir / "scores.jsonl"
    assert scores_path.is_file()
    rows = [l for l in scores_path.read_text().splitlines() if l.strip()]
    assert len(rows) >= 4
    # 校验：identity variant 的 score 应为 0（passthrough 自比）
    identity_rows = [json.loads(r) for r in rows if json.loads(r)["variant"] == "identity"]
    assert identity_rows, "应至少有一个 identity 行"
    for r in identity_rows:
        assert r["score"] == 0.0, f"identity variant score 应为 0（passthrough），得 {r['score']}"


def test_parse_block_candidates_unit() -> None:
    """block_candidates JSON 解析的 happy + fail-loud 分支（kind-keyed + E1）。"""
    sys.path.insert(0, str(_SCRIPTS_DIR))
    from puzzle_common import parse_block_candidates  # type: ignore

    # 空 → 默认集（5 kind，每个都含 identity）
    d = parse_block_candidates("")
    assert "identity" in d["attention"]
    assert "ffn_50" in d["ffn"]
    assert d["conv"] == ["identity"]
    assert d["moe"] == ["identity"]
    assert d["custom"] == ["identity"]

    # 合法 kind-keyed JSON（每 kind 都含 identity，E1）
    d = parse_block_candidates(
        '{"attention": ["identity", "fnet"], "ffn": ["identity", "no_op"]}'
    )
    assert d == {"attention": ["identity", "fnet"], "ffn": ["identity", "no_op"]}

    # conv/moe/custom 只含 identity 也合法（D4）
    d = parse_block_candidates('{"conv": ["identity"]}')
    assert d == {"conv": ["identity"]}

    # raise 分支
    with pytest.raises(ValueError):
        parse_block_candidates("not json")
    with pytest.raises(ValueError):
        parse_block_candidates("[1,2,3]")  # 非 dict
    with pytest.raises(ValueError):
        parse_block_candidates("{}")  # 空 dict
    with pytest.raises(ValueError):
        parse_block_candidates('{"attention": [123], "ffn": ["identity"]}')  # 非 str
    with pytest.raises(ValueError):
        parse_block_candidates(
            '{"attention": ["nonexistent_variant"], "ffn": ["identity"]}'
        )  # 未注册
    with pytest.raises(ValueError):
        parse_block_candidates(
            '{"attention": ["identity"], "ffn": ["random_synthesizer", "identity"]}'
        )  # 不适用 kind
    # E1：某 kind 列表缺 identity → raise
    with pytest.raises(ValueError, match="identity"):
        parse_block_candidates('{"attention": ["fnet"], "ffn": ["identity"]}')
    with pytest.raises(ValueError, match="identity"):
        parse_block_candidates('{"attention": ["identity"], "ffn": ["no_op"]}')

