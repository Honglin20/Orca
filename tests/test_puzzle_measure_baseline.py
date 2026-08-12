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
