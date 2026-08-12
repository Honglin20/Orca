"""test_puzzle_measure_baseline.py —— Phase U2a measure_baseline + search_space_io 测试。

锁定 SPEC v2 §9.2 的 4 道 smoke intent + §6.3/§16.8 的 empty-slots terminate 路径 +
search_space.yaml ↔ block_map.json 契约。

重点（Rule 9：验证 intent 非 behavior）：
  - 4 道 smoke 各自是一道 fidelity 关卡（不只测「跑通」，测「fail loud 能拦错」）。
  - E22 empty slots → exit 2 + model_type_supported=false（terminate_unsupported）。
  - strict-load 是命门（E13/E5）：missing/unexpected 任一非零必 raise。
  - search_space path ↔ block_map parent_module_path 映射（E3）。
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

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "workflows" / "agents" / "_puzzle_scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))


# ── 合成 transformer flat（与 test_puzzle_scripts_smoke 同款；含 build/eval/DUMMY）────

_TINY_FLAT_PY = textwrap.dedent(
    """
    import torch
    import torch.nn as nn

    DUMMY_INPUT = {"shape": [2, 16, 32], "dtype": "float32"}


    class SimpleAttention(nn.Module):
        def __init__(self, dim, num_heads):
            super().__init__()
            self.num_heads = num_heads
            self.head_dim = dim // num_heads
            self.qkv = nn.Linear(dim, dim * 3)
            self.proj = nn.Linear(dim, dim)

        def forward(self, x):
            B, L, C = x.shape
            qkv = self.qkv(x).reshape(B, L, 3, self.num_heads, self.head_dim)
            q, k, v = qkv.unbind(dim=2)
            q, k, v = q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2)
            attn = (q @ k.transpose(-2, -1)) * (self.head_dim ** -0.5)
            attn = attn.softmax(dim=-1)
            out = (attn @ v).transpose(1, 2).reshape(B, L, C)
            return self.proj(out)


    class FeedForward(nn.Module):
        def __init__(self, dim, hidden):
            super().__init__()
            self.fc1 = nn.Linear(dim, hidden)
            self.act = nn.GELU()
            self.fc2 = nn.Linear(hidden, dim)

        def forward(self, x):
            return self.fc2(self.act(self.fc1(x)))


    class TinyBlock(nn.Module):
        def __init__(self, dim, num_heads, hidden):
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
        def __init__(self, dim=32, num_heads=4, num_blocks=2, hidden=64):
            super().__init__()
            self.embed = nn.Linear(32, dim)
            self.blocks = nn.ModuleList([TinyBlock(dim, num_heads, hidden) for _ in range(num_blocks)])
            self.norm = nn.LayerNorm(dim)
            self.head = nn.Linear(dim, 10)

        def forward(self, x):
            x = self.embed(x)
            for blk in self.blocks:
                x = blk(x)
            x = self.norm(x)
            return self.head(x.mean(dim=1))


    def build_model(dim=32, num_heads=4, num_blocks=2, hidden=64):
        return TinyTransformer(dim, num_heads, num_blocks, hidden)


    def evaluate(model):
        model.eval()
        with torch.no_grad():
            torch.manual_seed(0)
            x = torch.randn(*DUMMY_INPUT["shape"])
            return float(model(x).softmax(-1).max(dim=-1).values.mean().item())
    """
)


def _search_space_payload(num_blocks: int = 2) -> dict:
    """合成 search_space dict（模拟 LLM 产物；in_dim/out_dim 留 -1 待 trace）。"""
    slots = []
    for i in range(num_blocks):
        slots.append({
            "id": f"L{i}_attention", "path": f"blocks.{i}.attn", "kind": "attention",
            "layer_idx": i, "source_class": "SimpleAttention",
            "forward_arity": "single", "return_arity": "single",
            "mask_load_bearing": False, "num_heads": 4, "head_dim": 8,
            "in_dim": -1, "out_dim": -1,
            "kind_evidence": "forward 含 matmul(Q,K^T) 缩放 + softmax（q @ k.transpose * scale）",
        })
        slots.append({
            "id": f"L{i}_ffn", "path": f"blocks.{i}.ffn", "kind": "ffn",
            "layer_idx": i, "source_class": "FeedForward",
            "forward_arity": "single", "return_arity": "single",
            "mask_load_bearing": False,
            "original_intermediate": 64, "activation": "gelu", "ffn_struct": "standard",
            "in_dim": -1, "out_dim": -1,
            "kind_evidence": "Linear(fc1)->GELU->Linear(fc2) 主导，standard 结构",
        })
    return {
        "slots": slots,
        "candidates": {"attention": ["identity", "fnet", "no_op"],
                       "ffn": ["identity", "ffn_50", "no_op"]},
    }


def _dump_yaml(payload: dict, path: Path) -> None:
    import yaml  # type: ignore
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
    """写 flat model + 预训练 father ckpt + search_space.yaml，返回路径。"""
    import torch  # type: ignore

    flat_path = tmp_path / "tiny_flat.py"
    flat_path.write_text(_TINY_FLAT_PY, encoding="utf-8")

    # 预训练 father ckpt：build 模型 → save state_dict（strict-load 必双零）
    sys.path.insert(0, str(tmp_path))
    import importlib.util
    spec = importlib.util.spec_from_file_location("_tiny_flat", flat_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    father_ckpt = tmp_path / "father.pth"
    torch.save(mod.build_model().state_dict(), father_ckpt)

    ss_path = tmp_path / "search_space.yaml"
    _dump_yaml(_search_space_payload(num_blocks=2), ss_path)
    return {
        "flat": flat_path,
        "father": father_ckpt,
        "search_space": ss_path,
        "output_dir": tmp_path / "out",
    }


# ── search_space_io 单元 ──────────────────────────────────────────────────────

def test_search_space_io_roundtrip(tmp_path: Path) -> None:
    """load → save round-trip 保留 path/元数据；to_block_map 映射 path→parent_module_path。"""
    from search_space_io import load_search_space_yaml, save_search_space_yaml, to_block_map

    ss = tmp_path / "ss.yaml"
    _dump_yaml(_search_space_payload(num_blocks=1), ss)
    slot_dicts, candidates = load_search_space_yaml(ss)
    assert len(slot_dicts) == 2
    assert slot_dicts[0]["parent_module_path"] == "blocks.0.attn"  # path 映射
    assert slot_dicts[0]["kind_evidence"]  # 元数据保留
    assert "identity" in candidates["attention"]

    bm = to_block_map(slot_dicts)
    assert bm.slots[0].parent_module_path == "blocks.0.attn"
    assert bm.slots[0].forward_arity == "single"

    out_ss = tmp_path / "out_ss.yaml"
    save_search_space_yaml(out_ss, slot_dicts, candidates)
    slot_dicts2, _ = load_search_space_yaml(out_ss)
    assert slot_dicts2[0]["kind_evidence"]  # round-trip 保留


def test_search_space_io_load_fail_loud(tmp_path: Path) -> None:
    from search_space_io import load_search_space_yaml

    # 缺文件
    with pytest.raises(FileNotFoundError):
        load_search_space_yaml(tmp_path / "nope.yaml")
    # 非法 kind
    _dump_yaml({"slots": [{"id": "A", "path": "x", "kind": "weird", "layer_idx": 0}],
                "candidates": {"attention": ["identity"]}}, tmp_path / "bad.yaml")
    with pytest.raises(ValueError, match="kind"):
        load_search_space_yaml(tmp_path / "bad.yaml")
    # 重复 id
    _dump_yaml({"slots": [
        {"id": "A", "path": "x", "kind": "attention", "layer_idx": 0},
        {"id": "A", "path": "y", "kind": "attention", "layer_idx": 1}],
        "candidates": {"attention": ["identity"]}}, tmp_path / "dup.yaml")
    with pytest.raises(ValueError, match="重复"):
        load_search_space_yaml(tmp_path / "dup.yaml")
    # candidates 缺 identity（E1）
    _dump_yaml({"slots": [{"id": "A", "path": "x", "kind": "attention", "layer_idx": 0}],
                "candidates": {"attention": ["fnet"]}}, tmp_path / "noe1.yaml")
    with pytest.raises(ValueError, match="identity"):
        load_search_space_yaml(tmp_path / "noe1.yaml")


# ── measure_baseline：4 道 smoke happy path ────────────────────────────────────

@pytest.mark.slow
def test_measure_baseline_happy_4_smokes(tmp_path: Path) -> None:
    paths = _setup_fixture(tmp_path)
    paths["output_dir"].mkdir(parents=True, exist_ok=True)
    rc, out, err = _run("measure_baseline.py", [
        "--flat_path", str(paths["flat"]),
        "--build_fn", "build_model",
        "--build_cfg", "",
        "--father_ckpt", str(paths["father"]),
        "--eval_fn", "evaluate",
        "--eval_kind", "classification",
        "--search_space_path", str(paths["search_space"]),
        "--latency_unit", "ms",
        "--output_dir", str(paths["output_dir"]),
        "--seed", "0",
    ])
    assert rc == 0, f"measure_baseline rc={rc}\nSTDERR:\n{err}\nSTDOUT:\n{out}"
    result = _parse_result_json(out)
    assert result["model_type_supported"] is True
    assert result["smokes_passed"] == [
        "strict-load", "forward-determinism",
        "per-slot-identity-allclose", "eval-stability",
    ], f"4 道 smoke 应全绿：{result['smokes_passed']}"
    assert result["baseline_acc"] > 0
    assert result["baseline_latency"] > 0
    # 产物
    assert (paths["output_dir"] / "block_map.json").is_file()
    assert (paths["output_dir"] / "search_space.yaml").is_file()
    assert (paths["output_dir"] / "baseline_metrics.json").is_file()
    assert (paths["output_dir"] / "father_state_dict.pt").is_file()
    # block_map 的 in_dim/out_dim 已 trace 回填（非 -1）
    bm = json.loads((paths["output_dir"] / "block_map.json").read_text())
    assert all(s["in_dim"] == 32 for s in bm["slots"]), "in_dim 应 trace 回填为 32"
    assert all(s["out_dim"] == 32 for s in bm["slots"])
    # search_space.yaml 也回填了
    ss_text = (paths["output_dir"] / "search_space.yaml").read_text()
    assert "in_dim: 32" in ss_text


# ── E22：empty slots → exit 2 + terminate_unsupported ──────────────────────────

def test_measure_baseline_empty_slots_exit_2(tmp_path: Path) -> None:
    paths = _setup_fixture(tmp_path)
    empty_ss = tmp_path / "empty.yaml"
    empty_ss.write_text("slots: []\ncandidates: {}\n", encoding="utf-8")
    paths["output_dir"].mkdir(parents=True, exist_ok=True)
    rc, out, _ = _run("measure_baseline.py", [
        "--flat_path", str(paths["flat"]),
        "--build_fn", "build_model",
        "--father_ckpt", str(paths["father"]),
        "--eval_fn", "evaluate",
        "--eval_kind", "classification",
        "--search_space_path", str(empty_ss),
        "--output_dir", str(paths["output_dir"]),
    ])
    assert rc == 2, f"empty slots 应 exit 2，得 rc={rc}"
    result = _parse_result_json(out)
    assert result["model_type_supported"] is False
    assert "terminate_unsupported" in result["error"] or "empty" in result["error"]


# ── smoke 1 fail loud：strict-load missing keys ────────────────────────────────

def test_measure_baseline_strict_load_fail(tmp_path: Path) -> None:
    """father ckpt 与 flat schema 不齐（少权重）→ strict-load smoke raise → exit 2。"""
    import torch  # type: ignore

    paths = _setup_fixture(tmp_path)
    # 构造一个缺 key 的 father ckpt：删 head.weight
    state = torch.load(paths["father"], map_location="cpu", weights_only=False)
    del state["head.weight"]
    del state["head.bias"]
    bad_ckpt = tmp_path / "bad_father.pth"
    torch.save(state, bad_ckpt)
    paths["output_dir"].mkdir(parents=True, exist_ok=True)
    rc, out, err = _run("measure_baseline.py", [
        "--flat_path", str(paths["flat"]),
        "--build_fn", "build_model",
        "--father_ckpt", str(bad_ckpt),
        "--eval_fn", "evaluate",
        "--eval_kind", "classification",
        "--search_space_path", str(paths["search_space"]),
        "--output_dir", str(paths["output_dir"]),
    ])
    assert rc == 2, f"strict-load 失败应 exit 2，得 rc={rc}"
    assert "strict-load" in err, f"stderr 应点名 strict-load smoke 失败：\n{err}"
    assert "missing" in err.lower()


# ── smoke 2 fail loud：forward-determinism（非确定 forward）────────────────────

def test_measure_baseline_forward_determinism_fail(tmp_path: Path) -> None:
    """forward 含未固定 RNG（Dropout in eval 泄漏——构造一个 eval 不关 dropout 的模型）。"""
    import torch  # type: ignore

    nondet_flat = tmp_path / "nondet_flat.py"
    nondet_flat.write_text(textwrap.dedent(
        """
        import torch
        import torch.nn as nn
        import torch.nn.functional as F

        DUMMY_INPUT = {"shape": [2, 8], "dtype": "float32"}

        class NonDet(nn.Module):
            # 故意用 F.dropout(training=True)——即使 model.eval() forward 也带随机性，
            # 模拟「forward 内含未固定 RNG」（forward-determinism smoke 的拦截目标）。
            def __init__(self):
                super().__init__()
                self.fc = nn.Linear(8, 4)

            def forward(self, x):
                return self.fc(F.dropout(x, p=0.5, training=True))

        def build_model():
            return NonDet()

        def evaluate(model):
            with torch.no_grad():
                return float(model(torch.randn(*DUMMY_INPUT["shape"])).abs().mean())
        """
    ), encoding="utf-8")
    # father ckpt：build + save
    sys.path.insert(0, str(tmp_path))
    import importlib.util
    spec = importlib.util.spec_from_file_location("_nondet", nondet_flat)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    father = tmp_path / "father.pth"
    torch.save(mod.build_model().state_dict(), father)
    # 把模型设 train 让 dropout 生效
    # search_space: 一个 attention slot（path fc 不对，但 NonDet 没 attention——用 single slot fc 测 forward）
    ss = tmp_path / "ss.yaml"
    _dump_yaml({"slots": [{"id": "L0_attn", "path": "fc", "kind": "attention",
                           "layer_idx": 0, "num_heads": 1, "head_dim": 4}],
                "candidates": {"attention": ["identity"]}}, ss)
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    rc, out, err = _run("measure_baseline.py", [
        "--flat_path", str(nondet_flat),
        "--build_fn", "build_model",
        "--father_ckpt", str(father),
        "--eval_fn", "evaluate",
        "--eval_kind", "classification",
        "--search_space_path", str(ss),
        "--output_dir", str(out_dir),
    ])
    assert rc == 2, f"forward-determinism 失败应 exit 2，得 rc={rc}"
    # 命中 forward-determinism 或 identity-allclose 之一（eval-stability 也可能先拦）
    assert (
        "forward-determinism" in err
        or "identity allclose" in err
        or "eval-stability" in err
    ), f"stderr 应点名某道 determinism smoke 失败：\n{err}"


# ── smoke 3 fail loud：eval-stability（eval_fn 两次返不同 acc）───────────────────

def test_measure_baseline_eval_stability_fail(tmp_path: Path) -> None:
    """eval_fn 内含未 seed 随机 → 两次返不同 acc → eval-stability smoke raise → exit 2。"""
    import torch  # type: ignore

    nondet_eval_flat = tmp_path / "nondet_eval_flat.py"
    nondet_eval_flat.write_text(textwrap.dedent(
        """
        import torch
        import torch.nn as nn

        DUMMY_INPUT = {"shape": [2, 8], "dtype": "float32"}

        class Tiny(nn.Module):
            def __init__(self):
                super().__init__()
                self.fc = nn.Linear(8, 4)

            def forward(self, x):
                return self.fc(x)

        def build_model():
            return Tiny()

        def evaluate(model):
            # 故意每次返未 seed 的随机数——eval-stability smoke 的拦截目标
            model.eval()
            with torch.no_grad():
                model(torch.randn(*DUMMY_INPUT["shape"]))
                return float(torch.randn(1).item())
        """
    ), encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    import importlib.util
    spec = importlib.util.spec_from_file_location("_nondet_eval", nondet_eval_flat)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    father = tmp_path / "father.pth"
    torch.save(mod.build_model().state_dict(), father)
    ss = tmp_path / "ss.yaml"
    _dump_yaml({"slots": [{"id": "L0_attn", "path": "fc", "kind": "attention",
                           "layer_idx": 0, "num_heads": 1, "head_dim": 4}],
                "candidates": {"attention": ["identity"]}}, ss)
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    rc, out, err = _run("measure_baseline.py", [
        "--flat_path", str(nondet_eval_flat),
        "--build_fn", "build_model",
        "--father_ckpt", str(father),
        "--eval_fn", "evaluate",
        "--eval_kind", "classification",
        "--search_space_path", str(ss),
        "--output_dir", str(out_dir),
    ])
    assert rc == 2, f"eval-stability 失败应 exit 2，得 rc={rc}"
    assert "eval-stability" in err, f"stderr 应点名 eval-stability smoke 失败：\n{err}"


# ── smoke 4 isolated：per-slot allclose 分支（函数级，隔离 smoke 2 干扰）─────────

def test_per_slot_identity_allclose_branch_raises(monkeypatch) -> None:
    """smoke 4 的 allclose 检查能独立拦「两次 forward 的 slot output 不一致」。

    whole-output 确定的模型，其 per-slot 输出必然确定（smoke 2 蕴含 smoke 4）。真实模型
    无法触发 smoke 4 而不先触发 smoke 2——故函数级 monkeypatch _hook_slot_outputs 返两次
    不同 tensor，证 allclose 分支真能 raise（隔离验证 smoke 4 的 check 逻辑独立有效）。
    """
    import torch  # type: ignore
    import torch.nn as nn  # type: ignore
    import measure_baseline as mb  # type: ignore

    # 一个最简确定性模型（smoke 2 会过）
    class Tiny(nn.Module):
        def __init__(self):
            super().__init__()
            self.fc = torch.nn.Linear(4, 4)

        def forward(self, x):
            return self.fc(x)

    model = Tiny()
    dummy = torch.randn(2, 4)
    # monkeypatch：两次 _hook_slot_outputs 返不同的 slot output
    call_count = {"n": 0}

    def fake_hook(_model, paths, _dummy, _device):
        call_count["n"] += 1
        # 第一次返全 1，第二次返全 1 + 0.1（超过 atol=1e-5）→ allclose False
        val = torch.ones(2, 4) if call_count["n"] == 1 else torch.ones(2, 4) + 0.1
        return {p: val for p in paths}

    monkeypatch.setattr(mb, "_hook_slot_outputs", fake_hook)
    with pytest.raises(RuntimeError, match="per-slot identity allclose"):
        mb.forward_determinism_and_identity_allclose(
            model, ["fc"], dummy, torch.device("cpu")
        )


# ── smoke 1 fail loud：strict-load unexpected keys ─────────────────────────────

def test_measure_baseline_strict_load_unexpected_fail(tmp_path: Path) -> None:
    """father ckpt 多塞一个 fake key → unexpected 非零 → strict-load smoke raise。"""
    import torch  # type: ignore

    paths = _setup_fixture(tmp_path)
    state = torch.load(paths["father"], map_location="cpu", weights_only=False)
    state["__fake_extra__.weight"] = torch.zeros(2, 2)
    bad_ckpt = tmp_path / "extra_father.pth"
    torch.save(state, bad_ckpt)
    paths["output_dir"].mkdir(parents=True, exist_ok=True)
    rc, out, err = _run("measure_baseline.py", [
        "--flat_path", str(paths["flat"]),
        "--build_fn", "build_model",
        "--father_ckpt", str(bad_ckpt),
        "--eval_fn", "evaluate",
        "--eval_kind", "classification",
        "--search_space_path", str(paths["search_space"]),
        "--output_dir", str(paths["output_dir"]),
    ])
    assert rc == 2
    assert "strict-load" in err and "unexpected" in err.lower()


# ── search_space_io 补充失败路径 ───────────────────────────────────────────────

def test_search_space_io_load_fail_lound_paths_and_fields(tmp_path: Path) -> None:
    from search_space_io import load_search_space_yaml

    # slots 非 list
    _dump_yaml({"slots": "notalist", "candidates": {"attention": ["identity"]}},
               tmp_path / "badslots.yaml")
    with pytest.raises(ValueError, match="slots.*list"):
        load_search_space_yaml(tmp_path / "badslots.yaml")
    # slot 缺必填字段（layer_idx）
    _dump_yaml({"slots": [{"id": "A", "path": "x", "kind": "attention"}],
                "candidates": {"attention": ["identity"]}}, tmp_path / "nofield.yaml")
    with pytest.raises(ValueError, match="必填字段"):
        load_search_space_yaml(tmp_path / "nofield.yaml")
    # 重复 path（同 module 双声明）
    _dump_yaml({"slots": [
        {"id": "A", "path": "x", "kind": "attention", "layer_idx": 0},
        {"id": "B", "path": "x", "kind": "attention", "layer_idx": 1}],
        "candidates": {"attention": ["identity"]}}, tmp_path / "duppath.yaml")
    with pytest.raises(ValueError, match="path.*重复"):
        load_search_space_yaml(tmp_path / "duppath.yaml")
    # slots 非空但 candidates 空 dict（fail loud，禁静默填默认）
    _dump_yaml({"slots": [{"id": "A", "path": "x", "kind": "attention", "layer_idx": 0}],
                "candidates": {}}, tmp_path / "emptycand.yaml")
    with pytest.raises(ValueError, match="candidates.*显式声明"):
        load_search_space_yaml(tmp_path / "emptycand.yaml")


# ── happy path：baseline_metrics.json 审计字段 ─────────────────────────────────

@pytest.mark.slow
def test_measure_baseline_happy_baseline_metrics_content(tmp_path: Path) -> None:
    """baseline_metrics.json 的 smokes_passed == 4 道全名 + eval_kind/seed 透传。"""
    paths = _setup_fixture(tmp_path)
    paths["output_dir"].mkdir(parents=True, exist_ok=True)
    rc, out, err = _run("measure_baseline.py", [
        "--flat_path", str(paths["flat"]),
        "--build_fn", "build_model",
        "--father_ckpt", str(paths["father"]),
        "--eval_fn", "evaluate",
        "--eval_kind", "classification",
        "--search_space_path", str(paths["search_space"]),
        "--latency_unit", "ms",
        "--output_dir", str(paths["output_dir"]),
        "--seed", "7",
    ])
    assert rc == 0, f"rc={rc}\nSTDERR:\n{err}"
    bm = json.loads((paths["output_dir"] / "baseline_metrics.json").read_text())
    assert bm["smokes_passed"] == [
        "strict-load", "forward-determinism",
        "per-slot-identity-allclose", "eval-stability",
    ]
    assert bm["eval_kind"] == "classification"
    assert bm["seed"] == 7
    assert bm["latency_unit"] == "ms"
