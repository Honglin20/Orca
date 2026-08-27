"""test_puzzle_father_state.py —— Puzzle U6 father 权重贯穿契约的单元测试。

U6 改造（root cause C/A/K）：脚本不再做 strict-load 双零硬门，也不再 ``load_father_model``。
father 权重贯穿路径：
  - ``adapters.build_model()`` + ``adapters.load_pretrained(model)`` → ``_LoadResult``
    （前缀剥离 / 多字段 dict / module./_orig_mod./ema 由适配器消化）。
  - ``build_pretrained_model(adapters)``：上面两步的 helper。
  - ``build_student_from_arch(adapters=...)``：identity（passthrough）slot 保留 father 权重。

不接真 fixture——合成最小 nn.Module + 临时 state_dict 文件，torch CPU 可用。
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("torch")
pytest.importorskip("nas_agent")

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _REPO / "workflows" / "puzzle" / "agents" / "_puzzle_scripts"


def _import_puzzle_common():
    here = str(_SCRIPTS_DIR)
    if here not in sys.path:
        sys.path.insert(0, here)
    import importlib
    return importlib.import_module("puzzle_common")


_TINY_MODEL_PY = textwrap.dedent(
    """
    import torch
    import torch.nn as nn

    DUMMY_INPUT = {"shape": [2, 4, 8], "dtype": "float32"}

    class SimpleAttention(nn.Module):
        def __init__(self, dim: int):
            super().__init__()
            self.proj = nn.Linear(dim, dim)
        def forward(self, x):
            return self.proj(x)

    class FeedForward(nn.Module):
        def __init__(self, dim: int):
            super().__init__()
            self.fc1 = nn.Linear(dim, dim * 2)
            self.act = nn.GELU()
            self.fc2 = nn.Linear(dim * 2, dim)
        def forward(self, x):
            return self.fc2(self.act(self.fc1(x)))

    class TinyBlock(nn.Module):
        def __init__(self, dim: int):
            super().__init__()
            self.attn = SimpleAttention(dim)
            self.ffn = FeedForward(dim)
        def forward(self, x):
            return x + self.attn(x) + self.ffn(x)

    class TinyTransformer(nn.Module):
        def __init__(self, dim=8):
            super().__init__()
            self.embed = nn.Linear(8, dim)
            self.block = TinyBlock(dim)
        def forward(self, x):
            return self.block(self.embed(x))

    def build_model(dim=8):
        return TinyTransformer(dim)
    """
)


def _write_tiny_flat_model(tmp_path: Path) -> Path:
    p = tmp_path / "flat_model.py"
    p.write_text(_TINY_MODEL_PY, encoding="utf-8")
    return p


def _write_adapters(
    tmp_path: Path, flat_path: Path, father_ckpt: Path,
    wrap_in_state_dict: bool = False, strip_prefix: str | None = None,
) -> Path:
    """写最小 adapter；支持 wrapper / 前缀剥离去验证 load_pretrained 的通用性。"""
    wrap_flag = "True" if wrap_in_state_dict else "False"
    strip_expr = repr(strip_prefix) if strip_prefix else "None"
    adapters = tmp_path / "puzzle_adapters.py"
    adapters.write_text(textwrap.dedent(f"""
        import importlib.util, torch
        import torch.nn.functional as F
        from torch.utils.data import DataLoader, TensorDataset
        from collections import namedtuple
        _LoadResult = namedtuple("_LoadResult", ["missing", "unexpected", "from_scratch"])

        _spec = importlib.util.spec_from_file_location("_flat_for_adapter", r"{flat_path}")
        _flat = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_flat)
        build_model = _flat.build_model
        DUMMY_INPUT = _flat.DUMMY_INPUT

        FORWARD_CALLING_CONVENTION = "single"
        METRIC_DIRECTION = "higher-better"
        EVAL_NOISE_ATOL = 1e-6

        _WRAP = {wrap_flag}
        _STRIP = {strip_expr}

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
            if _WRAP and isinstance(ck, dict) and "state_dict" in ck:
                state = ck["state_dict"]
            else:
                state = ck
            if _STRIP:
                state = {{k[len(_STRIP):] if k.startswith(_STRIP) else k: v for k, v in state.items()}}
            mi, un = m.load_state_dict(state, strict=False)
            return _LoadResult(list(mi), list(un), len(mi) > 0.5 * len(m.state_dict()))
    """), encoding="utf-8")
    return adapters


# ── _LoadResult 结构 ─────────────────────────────────────────────────────────

def test_load_result_namedtuple_fields() -> None:
    pc = _import_puzzle_common()
    lr = pc._LoadResult(missing=["a"], unexpected=["b"], from_scratch=False)
    assert lr.missing == ["a"]
    assert lr.unexpected == ["b"]
    assert lr.from_scratch is False


# ── build_pretrained_model：father 权重真载入 ─────────────────────────────────

def test_build_pretrained_model_loads_weights(tmp_path: Path) -> None:
    """adapters.build_model + load_pretrained → father 权重真注入（sentinel 校验）。"""
    import torch
    pc = _import_puzzle_common()
    flat = _write_tiny_flat_model(tmp_path)
    sys.path.insert(0, str(tmp_path))
    spec = importlib.util.spec_from_file_location("_tiny_flat", flat)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    father = mod.build_model()
    sentinel_val = 3.1415
    with torch.no_grad():
        father.embed.weight.fill_(sentinel_val)
    father_ckpt = tmp_path / "father.pt"
    torch.save(father.state_dict(), father_ckpt)

    adapters_path = _write_adapters(tmp_path, flat, father_ckpt)
    adapters = pc.load_puzzle_adapters(adapters_path)
    loaded = pc.build_pretrained_model(adapters)
    assert torch.allclose(
        loaded.embed.weight,
        torch.full_like(loaded.embed.weight, sentinel_val),
    ), "father 权重未真载入（embed.weight 应为 sentinel 值）"
    assert loaded.training is False


def test_build_pretrained_model_handles_wrapper_format(tmp_path: Path) -> None:
    """ckpt 形如 {state_dict: {...}, ...} → 适配器解包后正确载入。"""
    import torch
    pc = _import_puzzle_common()
    flat = _write_tiny_flat_model(tmp_path)
    sys.path.insert(0, str(tmp_path))
    spec = importlib.util.spec_from_file_location("_tiny_flat_wrap", flat)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    father = mod.build_model()
    sentinel_val = 2.718
    with torch.no_grad():
        father.embed.weight.fill_(sentinel_val)
    wrapper_ckpt = tmp_path / "father_wrapper.pt"
    torch.save({"state_dict": father.state_dict(), "epoch": 10, "foo": "bar"}, wrapper_ckpt)

    adapters_path = _write_adapters(
        tmp_path, flat, wrapper_ckpt, wrap_in_state_dict=True
    )
    adapters = pc.load_puzzle_adapters(adapters_path)
    loaded = pc.build_pretrained_model(adapters)
    assert torch.allclose(
        loaded.embed.weight,
        torch.full_like(loaded.embed.weight, sentinel_val),
    )


def test_build_pretrained_model_strips_module_prefix(tmp_path: Path) -> None:
    """ckpt 顶层 key 含 ``module.`` 前缀 → 适配器剥离后正确载入（DDP wrap 兼容）。"""
    import torch
    pc = _import_puzzle_common()
    flat = _write_tiny_flat_model(tmp_path)
    sys.path.insert(0, str(tmp_path))
    spec = importlib.util.spec_from_file_location("_tiny_flat_pfx", flat)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    father = mod.build_model()
    sentinel_val = 1.618
    with torch.no_grad():
        father.embed.weight.fill_(sentinel_val)
    prefixed_state = {f"module.{k}": v for k, v in father.state_dict().items()}
    ckpt = tmp_path / "father_prefixed.pt"
    torch.save(prefixed_state, ckpt)

    adapters_path = _write_adapters(tmp_path, flat, ckpt, strip_prefix="module.")
    adapters = pc.load_puzzle_adapters(adapters_path)
    loaded = pc.build_pretrained_model(adapters)
    assert torch.allclose(
        loaded.embed.weight,
        torch.full_like(loaded.embed.weight, sentinel_val),
    )


# ── build_student_from_arch：identity slot 保留 father 权重（核心 intent）─────

def test_build_student_from_arch_identity_retains_father_weights(tmp_path: Path):
    """delta 核心契约：identity（passthrough）slot 在 adapters 注入 father 权重后保留。"""
    import torch
    pc = _import_puzzle_common()
    flat = _write_tiny_flat_model(tmp_path)
    sys.path.insert(0, str(tmp_path))
    spec = importlib.util.spec_from_file_location("_tiny_flat_student", flat)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    father = mod.build_model()
    sentinel_val = 1.234
    with torch.no_grad():
        father.block.attn.proj.weight.fill_(sentinel_val)
        father.block.ffn.fc1.weight.fill_(sentinel_val)
    father_ckpt = tmp_path / "father_state.pt"
    torch.save(father.state_dict(), father_ckpt)

    adapters_path = _write_adapters(tmp_path, flat, father_ckpt)
    adapters = pc.load_puzzle_adapters(adapters_path)

    slot_att = pc.Slot(
        layer_idx=0, kind="attention", in_dim=8, out_dim=8,
        num_heads=4, head_dim=2, source_class="SimpleAttention",
        parent_module_path="block.attn",
    )
    slot_ffn = pc.Slot(
        layer_idx=0, kind="ffn", in_dim=8, out_dim=8,
        num_heads=0, head_dim=0, source_class="FeedForward",
        parent_module_path="block.ffn",
        original_intermediate=16, activation="gelu",
    )
    block_map = pc.BlockMap(slots=[slot_att, slot_ffn])
    selected_arch = {"selected_arch": {"0": {"attention": "identity", "ffn": "identity"}}}
    block_library_dir = tmp_path / "block_library"; block_library_dir.mkdir()

    # U6：build_student_from_arch(adapters=...) → adapters.load_pretrained 注入 father 权重
    student = pc.build_student_from_arch(
        adapters=adapters,
        block_map=block_map,
        selected_arch=selected_arch,
        block_library_dir=block_library_dir,
        device=torch.device("cpu"),
    )
    assert torch.allclose(
        student.block.attn.proj.weight,
        torch.full_like(student.block.attn.proj.weight, sentinel_val),
    ), "adapters.load_pretrained 后 identity（attention）slot 必须保留 father 权重"
    assert torch.allclose(
        student.block.ffn.fc1.weight,
        torch.full_like(student.block.ffn.fc1.weight, sentinel_val),
    ), "adapters.load_pretrained 后 identity（ffn）slot 必须保留 father 权重"
