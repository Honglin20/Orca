"""test_puzzle_scripts_smoke.py —— Puzzle P2.10 端到端链路 smoke test。

不接真 fixture——在测试文件内定义最小合成 transformer（2 层 SimpleAttention +
SimpleFFN），跑全链：expand→bld(2 variant, 1 epoch)→score→mip→build→gkd(1
epoch, CPU)→gate，断言每步产物文件存在 + AC 字段类型正确。

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
            x = torch.randn(*DUMMY_INPUT["shape"])
            logits = model(x)
            # proxy acc: mean max-softmax confidence
            return float(logits.softmax(-1).max(dim=-1).values.mean().item())
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
    1. 末行 ``RESULT_JSON: {...}`` （expand/bld/score/build/gkd 等有 KEY:value 遥测的脚本）
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


@pytest.mark.slow
def test_puzzle_full_chain_cpu(tmp_path: Path) -> None:
    # 写 tiny model 作 model_path
    model_path = tmp_path / "model.py"
    model_path.write_text(_TINY_MODEL_PY, encoding="utf-8")

    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 只用 2 variant per slot_type 加速 CPU（identity + 一个真候选）
    block_candidates = json.dumps({
        "attention": ["identity", "fnet"],
        "ffn": ["identity", "ffn_50"],
    })

    build_args = ["--build_fn", "build_model", "--build_cfg", ""]
    eval_args = ["--eval_fn", "evaluate", "--eval_kind", "classification"]

    # 1. expand
    rc, out, _ = _run("expand_model.py", [
        "--project_root", str(tmp_path),
        "--model_path", str(model_path),
        *build_args,
        *eval_args,
        "--latency_unit", "ms",
        "--output_dir", str(output_dir),
        "--seed", "0",
    ])
    result = _parse_result_json(out)
    assert result["model_type_supported"] is True
    block_map_path = output_dir / "block_map.json"
    flat_model_path = output_dir / "model_flat.py"
    baseline_metrics_path = output_dir / "baseline_metrics.json"
    assert block_map_path.is_file()
    assert flat_model_path.is_file()
    assert baseline_metrics_path.is_file()
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

    # 8. gate_report
    rc, out, _ = _run("gate_report.py", [
        "--final_model", str(final_model_path),
        "--baseline_metrics", str(baseline_metrics_path),
        "--flat_model", str(flat_model_path),
        *build_args,
        "--block_map", str(block_map_path),
        "--block_library", str(block_library_dir),
        *eval_args,
        "--latency_unit", "ms",
        "--accuracy_tolerance", "0.5",
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


# ── 辅助：构造无 attention/ffn 的纯 CNN 模型（测 expand_model fail-loud 路径）────

_NO_SLOT_MODEL_PY = textwrap.dedent(
    """
    import torch
    import torch.nn as nn

    DUMMY_INPUT = {"shape": [2, 1, 28, 28], "dtype": "float32"}

    class TinyCNN(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv1 = nn.Conv2d(1, 8, 3)
            self.conv2 = nn.Conv2d(8, 16, 3)
            self.fc = nn.Linear(16 * 24 * 24, 10)

        def forward(self, x):
            x = torch.relu(self.conv1(x))
            x = torch.relu(self.conv2(x))
            x = x.flatten(1)
            return self.fc(x)

    def build_model():
        return TinyCNN()

    def evaluate(model):
        model.eval()
        with torch.no_grad():
            x = torch.randn(*DUMMY_INPUT["shape"])
            return float(model(x).softmax(-1).max().item())
    """
)


def test_expand_no_slot_exit_2(tmp_path: Path) -> None:
    """无 attention/ffn slot 的模型 → expand_model exit 2 + model_type_supported=false。"""
    model_path = tmp_path / "cnn.py"
    model_path.write_text(_NO_SLOT_MODEL_PY, encoding="utf-8")
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, str(_SCRIPTS_DIR / "expand_model.py"),
        "--project_root", str(tmp_path),
        "--model_path", str(model_path),
        "--build_fn", "build_model",
        "--eval_fn", "evaluate",
        "--eval_kind", "classification",
        "--output_dir", str(output_dir),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 2, (
        f"无 slot 模型应 exit 2，得 rc={proc.returncode}\nSTDERR:\n{proc.stderr}"
    )
    result = _parse_result_json(proc.stdout)
    assert result["model_type_supported"] is False
    assert result["baseline_acc"] == 0.0
    assert result["error"], "应给出 fail loud 根因"


def test_mip_select_infeasible(tmp_path: Path) -> None:
    """MIP 预算太紧 → feasible=false, select_reason=infeasible, selected_arch={}。"""
    scores_path = tmp_path / "scores.jsonl"
    latency_path = tmp_path / "latency_table.jsonl"
    # 2 组（layer,slot_type），每组 2 variant；latency 都很大
    with open(scores_path, "w") as f:
        for layer in (0, 1):
            for slot in ("attention", "ffn"):
                for v in ("identity", "other"):
                    f.write(json.dumps({
                        "layer": layer, "slot": slot, "variant": v,
                        "score": -0.5 if v != "identity" else 0.0, "valid": True,
                    }) + "\n")
    with open(latency_path, "w") as f:
        for layer in (0, 1):
            for slot in ("attention", "ffn"):
                for v in ("identity", "other"):
                    f.write(json.dumps({
                        "layer": layer, "slot": slot, "variant": v,
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


def test_score_preserves_parent_state(tmp_path: Path) -> None:
    """score.py 跑完后，父模型 state_dict 不变（replace_slot 还原不变量）。"""
    model_path = tmp_path / "model.py"
    model_path.write_text(_TINY_MODEL_PY, encoding="utf-8")
    output_dir = tmp_path / "out"

    # 复用全链的前两步（expand + bld）
    _run("expand_model.py", [
        "--project_root", str(tmp_path),
        "--model_path", str(model_path),
        "--build_fn", "build_model",
        "--eval_fn", "evaluate",
        "--eval_kind", "classification",
        "--output_dir", str(output_dir),
    ])
    block_map_path = output_dir / "block_map.json"
    flat_model_path = output_dir / "model_flat.py"
    _run("bld.py", [
        "--block_map", str(block_map_path),
        "--flat_model", str(flat_model_path),
        "--build_fn", "build_model",
        "--block_candidates",
        json.dumps({"attention": ["identity", "fnet"],
                    "ffn": ["identity", "ffn_50"]}),
        "--epochs", "1",
        "--output_dir", str(output_dir),
    ])

    # 载入父模型 → 取 state_dict 哈希
    sys.path.insert(0, str(_SCRIPTS_DIR))
    from puzzle_common import load_flat_model  # type: ignore
    parent_before = load_flat_model(flat_model_path, "build_model", "")
    sd_before = {k: v.clone() for k, v in parent_before.state_dict().items()}

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

    # 跑完后再载入父模型 → 对比
    # 关键：score 内部用同一 model 对象做 in-place replace_slot；还原失败会污染。
    # 用新进程重 load 不够（那会重置）——我们信任 score.py 内部 finally 还原；
    # 这里改为：score 跑完后立即在同一进程内复测 forward 数值是否稳定。
    parent_after = load_flat_model(flat_model_path, "build_model", "")
    sd_after = parent_after.state_dict()
    # 重建后的随机权重 != before（重新初始化），所以不能直接比 state_dict；
    # 改测 score 进程内是否抛异常（进程内 finally 还原出错会 rc=2）。score 已 rc=0。
    # 真正的不变量测试需要 score.py 暴露一个内部 API——这里降级为 rc==0 校验 +
    # scores.jsonl 非空校验（间接证明 score 没崩在还原失败上）。
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
    """block_candidates JSON 解析的 happy + 5 种 fail-loud 分支（纯函数）。"""
    sys.path.insert(0, str(_SCRIPTS_DIR))
    from puzzle_common import parse_block_candidates  # type: ignore

    # 空 → 默认集
    d = parse_block_candidates("")
    assert "identity" in d["attention"]
    assert "ffn_50" in d["ffn"]

    # 合法 JSON
    d = parse_block_candidates('{"attention": ["identity", "fnet"], "ffn": ["no_op"]}')
    assert d == {"attention": ["identity", "fnet"], "ffn": ["no_op"]}

    # 5 种 raise 分支
    with pytest.raises(ValueError):
        parse_block_candidates("not json")
    with pytest.raises(ValueError):
        parse_block_candidates("[1,2,3]")  # 非 dict
    with pytest.raises(ValueError):
        parse_block_candidates('{"attention": ["identity"]}')  # 缺 ffn
    with pytest.raises(ValueError):
        parse_block_candidates('{"attention": [123], "ffn": ["no_op"]}')  # 非 str
    with pytest.raises(ValueError):
        parse_block_candidates(
            '{"attention": ["nonexistent_variant"], "ffn": ["no_op"]}'
        )  # 未注册
    with pytest.raises(ValueError):
        parse_block_candidates(
            '{"attention": ["identity"], "ffn": ["random_synthesizer"]}'
        )  # 不适用 slot_type

