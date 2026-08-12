"""_puzzle_test_fixtures.py —— U6 puzzle 测试合成 fixture（共享）。

U6 把项目接口从「flat 文件 (build/eval)」翻转到「flat + adapters」：
  - flat 文件只暴露架构源（build_model + DUMMY_INPUT）。
  - adapters 文件暴露 SPEC U6 §2.1 全部能力 API（build_model / forward_model /
    calib_iter / train_iter / extract_labels / kd_loss / task_loss / evaluate /
    METRIC_DIRECTION / EVAL_NOISE_ATOL / load_pretrained / DUMMY_INPUT /
    FORWARD_CALLING_CONVENTION）。

本模块给所有 puzzle 测试共享合成 transformer + adapters 模板。**禁用 target 项目
真码**——只用合成 nn.Module（通用性铁律）。
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import torch


# ── 合成 transformer flat 文件（架构源：build_model + DUMMY_INPUT；无 eval/data）──

TINY_FLAT_PY = textwrap.dedent(
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
    """
)


def _adapters_template(
    flat_path_str: str,
    father_ckpt_str: str,
    device_expr: str = "torch.device('cpu')",
    metric_direction: str = "higher-better",
    eval_noise_atol: float = 1e-6,
    convention: str = "single",
    eval_kind: str = "classification",
) -> str:
    """生成 puzzle_adapters.py 模板字符串。

    本模板是 **classification 通用 adapter**（合成 fixture 用）。production 下由
    pz_expand agent 按 SPEC §2.2 faithful 移植用户源码生成；本测试 fixture 只是
    示范最小可用形态，避免在测试中硬编码 target 项目真码。
    """
    return textwrap.dedent(
        f"""
        import importlib.util
        import torch
        import torch.nn as nn
        import torch.nn.functional as F
        from torch.utils.data import DataLoader, TensorDataset

        # 从 flat 文件 import build_model（架构真相源）
        _FLAT_PATH = r"{flat_path_str}"
        _spec = importlib.util.spec_from_file_location("_puzzle_flat_for_adapter", _FLAT_PATH)
        _flat = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_flat)
        build_model = _flat.build_model
        DUMMY_INPUT = _flat.DUMMY_INPUT

        FORWARD_CALLING_CONVENTION = "{convention}"  # single: forward(x: tensor)
        METRIC_DIRECTION = "{metric_direction}"
        EVAL_NOISE_ATOL = {eval_noise_atol}

        _FATHER_CKPT = r"{father_ckpt_str}"

        from collections import namedtuple
        _LoadResult = namedtuple("_LoadResult", ["missing", "unexpected", "from_scratch"])


        def forward_model(model, batch):
            # single convention: batch 是 tensor 或 (tensor,) 或 (tensor, labels)
            if isinstance(batch, (tuple, list)):
                x = batch[0]
            else:
                x = batch
            return model(x)


        class _CalibDataset(torch.utils.data.Dataset):
            def __init__(self, n=4):
                shape = DUMMY_INPUT["shape"]
                self.x = torch.randn(n, *shape[1:], dtype=torch.float32)
            def __len__(self): return len(self.x)
            def __getitem__(self, i): return self.x[i]


        def calib_iter(device=None):
            ds = _CalibDataset(n=4)
            return iter(DataLoader(ds, batch_size=2, shuffle=False))


        def train_iter(device=None):
            shape = DUMMY_INPUT["shape"]
            n = 8
            x = torch.randn(n, *shape[1:], dtype=torch.float32)
            y = torch.randint(0, 10, (n,))
            ds = TensorDataset(x, y)
            return iter(DataLoader(ds, batch_size=4, shuffle=False))


        def extract_labels(batch):
            if isinstance(batch, (tuple, list)) and len(batch) >= 2:
                return batch[1]
            return None


        def kd_loss(s_out, t_out, labels=None):
            # classification KD: KL on softmax temperature=1（合成 fixture 通用）
            if isinstance(s_out, (tuple, list)): s_out = s_out[0]
            if isinstance(t_out, (tuple, list)): t_out = t_out[0]
            sl = F.log_softmax(s_out, dim=-1)
            tp = F.softmax(t_out, dim=-1)
            return F.kl_div(sl, tp, reduction="batchmean")


        def task_loss(s_out, labels):
            if labels is None: return None
            if isinstance(s_out, (tuple, list)): s_out = s_out[0]
            return F.cross_entropy(s_out, labels)


        def evaluate(model):
            model.eval()
            with torch.no_grad():
                torch.manual_seed(0)
                x = torch.randn(*DUMMY_INPUT["shape"])
                logits = model(x)
                return float(logits.softmax(-1).max(dim=-1).values.mean().item())


        def load_pretrained(model):
            # 宽松加载：剥离常见 wrapper（state_dict 字段 / module. 前缀）
            import os
            missing_full = []
            unexpected_full = []
            from_scratch = False
            if not _FATHER_CKPT or not os.path.isfile(_FATHER_CKPT):
                return _LoadResult([], [], True)
            ckpt = torch.load(_FATHER_CKPT, map_location="cpu", weights_only=False)
            if isinstance(ckpt, dict) and "state_dict" in ckpt and not any(
                k.startswith(("blocks.", "embed.", "head.")) for k in ckpt.keys()
            ):
                state = ckpt["state_dict"]
            else:
                state = ckpt
            # 剥 module./_orig_mod./ema. 前缀（若存在）
            def _strip(sd):
                out = {{}}
                for k, v in sd.items():
                    nk = k
                    for pfx in ("module.", "_orig_mod.", "ema."):
                         if nk.startswith(pfx):
                             nk = nk[len(pfx):]
                    out[nk] = v
                return out
            state = _strip(state)
            missing, unexpected = model.load_state_dict(state, strict=False)
            # from_scratch：> 50% keys missing 视为实质未加载
            total = max(1, len(model.state_dict()))
            from_scratch = len(missing) > 0.5 * total
            return _LoadResult(list(missing), list(unexpected), from_scratch)
        """
    )


def write_flat_and_adapters(
    tmp_path: Path,
    father_ckpt_path: Path | None = None,
    metric_direction: str = "higher-better",
    eval_noise_atol: float = 1e-6,
    num_blocks: int = 2,
) -> dict[str, Path]:
    """写 flat 文件 + adapters 文件 + father ckpt，返回路径 dict。

    father_ckpt_path 为 None 时自动构造（build 模型 → save state_dict，零 missing）。
    """
    flat_path = tmp_path / "tiny_flat.py"
    flat_path.write_text(TINY_FLAT_PY, encoding="utf-8")

    if father_ckpt_path is None:
        sys.path.insert(0, str(tmp_path))
        spec = importlib.util.spec_from_file_location("_tiny_flat_boot", flat_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        father_ckpt_path = tmp_path / "father.pth"
        torch.save(mod.build_model(num_blocks=num_blocks).state_dict(), father_ckpt_path)

    adapters_path = tmp_path / "puzzle_adapters.py"
    adapters_path.write_text(
        _adapters_template(
            flat_path_str=str(flat_path),
            father_ckpt_str=str(father_ckpt_path),
            metric_direction=metric_direction,
            eval_noise_atol=eval_noise_atol,
        ),
        encoding="utf-8",
    )
    return {
        "flat": flat_path,
        "adapters": adapters_path,
        "father": father_ckpt_path,
    }


def search_space_payload(num_blocks: int = 2) -> dict:
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
