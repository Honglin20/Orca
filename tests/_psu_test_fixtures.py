"""_psu_test_fixtures.py —— PSU（puzzle-supernet）P1 测试合成 fixture（共享）。

模拟 flatten / expand 两节点的确定性产物，全部只用合成 nn.Module（MNIST 级小模型，
CPU）：
  - ``TOY_PREPARED_PY``：合成 transformer flat 文件（embed + 2 层 encoder + head）
  - ``TOY_LOAD_PRETRAINED_PY``：flatten 期 ``load_pretrained.py`` 生成契约的玩具实现
  - ``TOY_LOAD_PRETRAINED_BAD_PY``：ckpt 冒烟失败版（strict 载入缺键 → fail loud）
  - ``TOY_SUPERNET_PY``：choice-only 超网玩具实现（遵循 transformer_layer spec：
    SearchSpace 三硬约束 / 权重继承 + freeze 分组 / 物化键契约 / all_original 默认）
  - ``TOY_SUPERNET_BAD_PY``：等价 gate 失败版（层 1 权重被扰动 + embed 参数漏冻结）
  - ``write_toy_flatten_artifacts`` / ``write_toy_expand_artifacts``：落盘 + 建 toy ckpt
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import torch

TOY_PREPARED_PY = textwrap.dedent(
    """
    import torch
    import torch.nn as nn

    DUMMY_INPUT = {"shape": [2, 8], "dtype": "int64"}

    class ToyEncoderLayer(nn.Module):
        def __init__(self, dim, num_heads, ffn_dim):
            super().__init__()
            self.norm1 = nn.LayerNorm(dim)
            self.attn = nn.MultiheadAttention(dim, num_heads, batch_first=True)
            self.norm2 = nn.LayerNorm(dim)
            self.fc1 = nn.Linear(dim, ffn_dim)
            self.act = nn.GELU()
            self.fc2 = nn.Linear(ffn_dim, dim)

        def forward(self, x, attn_mask=None):
            h = self.norm1(x)
            a, _ = self.attn(h, h, h, attn_mask=attn_mask, need_weights=False)
            x = x + a
            x = x + self.fc2(self.act(self.fc1(self.norm2(x))))
            return x

    class ToyTransformer(nn.Module):
        def __init__(self, vocab_size=50, dim=32, num_heads=4, ffn_dim=64, depth=2, num_classes=10):
            super().__init__()
            self.embed = nn.Embedding(vocab_size, dim)
            self.layers = nn.ModuleList(
                ToyEncoderLayer(dim, num_heads, ffn_dim) for _ in range(depth)
            )
            self.head = nn.Linear(dim, num_classes)

        def forward(self, tokens, attn_mask=None):
            x = self.embed(tokens)
            for layer in self.layers:
                x = layer(x, attn_mask=attn_mask)
            return self.head(x.mean(dim=1))

    if __name__ == "__main__":
        model = ToyTransformer()
        tokens = torch.randint(0, 50, (2, 8))
        out = model(tokens)
        print("output shape:", tuple(out.shape))
    """
)

TOY_LOAD_PRETRAINED_PY = textwrap.dedent(
    """
    \"\"\"load_pretrained.py —— 确定性预训练原模型构建器（玩具实现，flatten 生成契约的形态）。\"\"\"
    from __future__ import annotations

    from pathlib import Path

    import torch

    from toy_flat import ToyTransformer

    PRETRAINED_CKPT = Path(__file__).resolve().parent / "toy_ckpt.pt"


    def _unwrap_state_dict(raw):
        \"\"\"解包 {state_dict: ...} / {model: ...} 包装 + 剥 module. DataParallel 前缀。\"\"\"
        if isinstance(raw, dict) and "state_dict" in raw:
            raw = raw["state_dict"]
        if isinstance(raw, dict) and "model" in raw and isinstance(raw["model"], dict):
            raw = raw["model"]
        if any(k.startswith("module.") for k in raw):
            raw = {k[len("module."):]: v for k, v in raw.items()}
        return raw


    def build_pretrained_model() -> torch.nn.Module:
        model = ToyTransformer()
        raw = torch.load(PRETRAINED_CKPT, map_location="cpu")
        sd = _unwrap_state_dict(raw)
        model.load_state_dict(sd, strict=True)  # key mismatch → RuntimeError 列未匹配键
        return model.eval()


    def build_probe_inputs():
        g = torch.Generator().manual_seed(0)
        cases = [{"tokens": torch.randint(0, 50, (2, 8), generator=g)}]
        # causal mask：bool attn_mask 语义 True=blocked → 对角以上 True（对角以下可看）。
        causal = torch.triu(torch.ones(8, 8, dtype=torch.bool), diagonal=1)
        cases.append({"tokens": torch.randint(0, 50, (2, 8), generator=g),
                      "attn_mask": causal})
        return cases


    if __name__ == "__main__":
        model = build_pretrained_model()
        for i, case in enumerate(build_probe_inputs()):
            out = model(**case)
            print(f"case{i}: output shape {tuple(out.shape)}")
    """
)

# ckpt 冒烟失败版：载入前丢一个键 → strict load_state_dict RuntimeError（Missing key）。
TOY_LOAD_PRETRAINED_BAD_PY = TOY_LOAD_PRETRAINED_PY.replace(
    'model.load_state_dict(sd, strict=True)  # key mismatch → RuntimeError 列未匹配键',
    'sd = {k: v for k, v in sd.items() if k != "head.weight"}\n'
    '    model.load_state_dict(sd, strict=True)  # 缺键 → fail loud',
)
assert "缺键 → fail loud" in TOY_LOAD_PRETRAINED_BAD_PY  # replace 命中卫语句

_TOY_SUPERNET_COMMON_HEAD = textwrap.dedent(
    '''
    """supernet.py —— choice-only 玩具超网（遵循 transformer_layer spec 的最小实现）。"""
    from __future__ import annotations

    import copy
    import random
    from dataclasses import dataclass

    import torch
    import torch.nn as nn

    VOCAB_SIZE = 50
    NUM_CLASSES = 10

    BRANCH_CHOICES = ("original", "vanilla", "synthesizer")


    class ToyEncoderLayer(nn.Module):
        \"\"\"与 toy_flat 同构的 original 层（supernet 自包含：无跨目录 import）。\"\"\"

        def __init__(self, dim, num_heads, ffn_dim):
            super().__init__()
            self.norm1 = nn.LayerNorm(dim)
            self.attn = nn.MultiheadAttention(dim, num_heads, batch_first=True)
            self.norm2 = nn.LayerNorm(dim)
            self.fc1 = nn.Linear(dim, ffn_dim)
            self.act = nn.GELU()
            self.fc2 = nn.Linear(ffn_dim, dim)

        def forward(self, x, attn_mask=None):
            h = self.norm1(x)
            a, _ = self.attn(h, h, h, attn_mask=attn_mask, need_weights=False)
            x = x + a
            x = x + self.fc2(self.act(self.fc1(self.norm2(x))))
            return x


    class ChoiceLayer(nn.Module):
        """nas_agent ChoiceLayer 的同 API 玩具替身（保持 fixture 自包含）。"""

        def __init__(self, *, branches):
            super().__init__()
            self.branches = nn.ModuleDict(branches)
            self.choice_name = next(iter(self.branches))

        def forward(self, *args, **kwargs):
            return self.branches[self.choice_name](*args, **kwargs)


    @dataclass
    class ArchConfig:
        choices: tuple

        def validate(self) -> bool:
            return bool(self.choices) and all(c in BRANCH_CHOICES for c in self.choices)


    @dataclass
    class SearchSpace:
        # 唯一公有 list/tuple 属性 = choice 容器（硬约束 ①）。
        branch_choices: tuple = BRANCH_CHOICES
        # 钉死维度一律标量（硬约束 ②），零参构造（硬约束 ③）。
        depth: int = 2
        global_dim: int = 32
        head_dim: int = 8
        num_heads: int = 4
        ffn_dim: int = 64
        max_seq_len: int = 8
        activation: str = "gelu"

        def sample(self) -> ArchConfig:
            return ArchConfig(
                choices=tuple(random.choice(self.branch_choices) for _ in range(self.depth))
            )

        def all_original(self) -> ArchConfig:
            return ArchConfig(choices=("original",) * self.depth)

        def validate(self) -> bool:
            if "original" not in self.branch_choices:
                return False
            if len(set(self.branch_choices)) != len(self.branch_choices):
                return False
            if len(self.branch_choices) < 2:
                return False
            return self.depth >= 1 and self.global_dim % self.num_heads == 0


    class _ToyVariantLayer(nn.Module):
        """玩具变体层（Pre-LN + 线性 token mixer，mask-blind）。"""

        def __init__(self, dim, ffn_dim):
            super().__init__()
            self.norm1 = nn.LayerNorm(dim)
            self.mix = nn.Linear(dim, dim)
            self.norm2 = nn.LayerNorm(dim)
            self.fc1 = nn.Linear(dim, ffn_dim)
            self.act = nn.GELU()
            self.fc2 = nn.Linear(ffn_dim, dim)

        def forward(self, x, src_mask=None, **kwargs):
            x = x + self.mix(self.norm1(x))
            x = x + self.fc2(self.act(self.fc1(self.norm2(x))))
            return x


    class Branch(nn.Module):
        """分支适配器：forward 委托 + get_active_subnet 深拷贝 + elastic_num_params。"""

        def __init__(self, inner):
            super().__init__()
            self.inner = inner

        def forward(self, *args, **kwargs):
            return self.inner(*args, **kwargs)

        def get_active_subnet(self) -> nn.Module:
            return copy.deepcopy(self.inner)

        @property
        def elastic_num_params(self):
            return sum(p.numel() for p in self.parameters())


    class ToySubnet(nn.Module):
        """物化子网：模块树镜像原模型拓扑（slot 位置放 active 分支导出模块）。"""

        def __init__(self, embed, layers, head):
            super().__init__()
            self.embed = embed
            self.layers = nn.ModuleList(layers)
            self.head = head

        def forward(self, tokens, attn_mask=None):
            x = self.embed(tokens)
            for layer in self.layers:
                x = layer(x, attn_mask=attn_mask)
            return self.head(x.mean(dim=1))
    '''
)

_TOY_SUPERNET_GOOD_BODY = textwrap.dedent(
    '''
    class SuperNet(nn.Module):
        def __init__(self, search_space, pretrained_state=None):
            super().__init__()
            ss = search_space
            self.embed = nn.Embedding(VOCAB_SIZE, ss.global_dim)
            self.head = nn.Linear(ss.global_dim, NUM_CLASSES)
            self.layers = nn.ModuleList()
            for _ in range(ss.depth):
                branches = {
                    "original": Branch(ToyEncoderLayer(ss.global_dim, ss.num_heads, ss.ffn_dim)),
                    "vanilla": Branch(_ToyVariantLayer(ss.global_dim, ss.ffn_dim)),
                    "synthesizer": Branch(_ToyVariantLayer(ss.global_dim, ss.ffn_dim)),
                }
                self.layers.append(ChoiceLayer(branches=branches))
            if pretrained_state is not None:
                self._inherit(pretrained_state)
            self._apply_freeze()
            self.set_sample_config(ss.all_original())  # 默认 config = 全 original

        def _inherit(self, state):
            consumed = set()
            for name, module in (("embed", self.embed), ("head", self.head)):
                prefix = f"{name}."
                sub = {k[len(prefix):]: v for k, v in state.items() if k.startswith(prefix)}
                module.load_state_dict(sub, strict=True)
                consumed |= {k for k in state if k.startswith(prefix)}
            for i, layer in enumerate(self.layers):
                prefix = f"layers.{i}."
                sub = {k[len(prefix):]: v for k, v in state.items() if k.startswith(prefix)}
                layer.branches["original"].inner.load_state_dict(sub, strict=True)
                consumed |= {k for k in state if k.startswith(prefix)}
            leftover = sorted(set(state) - consumed)
            if leftover:
                raise RuntimeError(f"pretrained_state 未消费键（fail loud）: {leftover}")

        def _apply_freeze(self):
            for p in self.embed.parameters():
                p.requires_grad_(False)
            for p in self.head.parameters():
                p.requires_grad_(False)
            for layer in self.layers:
                for name, branch in layer.branches.items():
                    for p in branch.parameters():
                        p.requires_grad_(name != "original")

        def set_sample_config(self, arch_config):
            if not isinstance(arch_config, ArchConfig) or not arch_config.validate():
                raise ValueError(f"非法 ArchConfig: {arch_config!r}")
            if len(arch_config.choices) != len(self.layers):
                raise ValueError("choices 长度 != slot 数")
            for i, choice in enumerate(arch_config.choices):
                if choice not in self.layers[i].branches:
                    raise ValueError(f"layers[{i}] 无分支 {choice!r}")
                self.layers[i].choice_name = choice

        def forward(self, tokens, attn_mask=None):
            x = self.embed(tokens)
            for layer in self.layers:
                x = layer(x, attn_mask=attn_mask)
            return self.head(x.mean(dim=1))

        def get_active_subnet(self) -> nn.Module:
            layers = [layer.branches[layer.choice_name].get_active_subnet()
                      for layer in self.layers]
            return ToySubnet(copy.deepcopy(self.embed), layers, copy.deepcopy(self.head))

        @property
        def elastic_num_params(self):
            total = sum(p.numel() for p in self.embed.parameters())
            total += sum(p.numel() for p in self.head.parameters())
            for layer in self.layers:
                total += layer.branches[layer.choice_name].elastic_num_params
            return total


    def build_supernet(pretrained_state=None) -> SuperNet:
        return SuperNet(SearchSpace(), pretrained_state=pretrained_state)


    if __name__ == "__main__":
        ss = SearchSpace()
        assert ss.validate()
        supernet = build_supernet()
        cfg = ss.sample()
        supernet.set_sample_config(cfg)
        tokens = torch.randint(0, VOCAB_SIZE, (2, 8))
        with torch.no_grad():
            out_a = supernet(tokens)
            out_b = supernet.get_active_subnet()(tokens)
        assert torch.allclose(out_a, out_b, atol=1e-5, rtol=1e-4)
        print("toy supernet self-check OK")
    '''
)

# 失败版：层 1 original 分支权重被扰动（物化键值不等 + forward 漂移）+ embed 漏冻结。
# 注意 replace 目标匹配 **dedent 之后**的 _TOY_SUPERNET_GOOD_BODY 文本。
_TOY_SUPERNET_BAD_BODY = _TOY_SUPERNET_GOOD_BODY.replace(
    """        for i, layer in enumerate(self.layers):
            prefix = f"layers.{i}."
            sub = {k[len(prefix):]: v for k, v in state.items() if k.startswith(prefix)}
            layer.branches["original"].inner.load_state_dict(sub, strict=True)
            consumed |= {k for k in state if k.startswith(prefix)}""",
    """        for i, layer in enumerate(self.layers):
            prefix = f"layers.{i}."
            sub = {k[len(prefix):]: v for k, v in state.items() if k.startswith(prefix)}
            layer.branches["original"].inner.load_state_dict(sub, strict=True)
            if i == 1:  # 缺陷注入：继承后被扰动（模拟静默错配。注意必须用**非均匀**扰动：
                        # 对整行/整权重加常数会被前置 LayerNorm 的零均值性精确抵消，
                        # forward 完全不变——这正是物化键契约独立于 forward 检查的价值）
                with torch.no_grad():
                    layer.branches["original"].inner.fc1.weight[0].mul_(2.0)
            consumed |= {k for k in state if k.startswith(prefix)}""",
).replace(
    """    def _apply_freeze(self):
        for p in self.embed.parameters():
            p.requires_grad_(False)""",
    """    def _apply_freeze(self):
        for p in self.embed.parameters():
            p.requires_grad_(True)  # 缺陷注入：固定模块漏冻结""",
)

TOY_SUPERNET_PY = _TOY_SUPERNET_COMMON_HEAD + _TOY_SUPERNET_GOOD_BODY
TOY_SUPERNET_BAD_PY = _TOY_SUPERNET_COMMON_HEAD + _TOY_SUPERNET_BAD_BODY

TOY_INSPECT_PY = textwrap.dedent(
    """
    from supernet import SearchSpace, build_supernet

    def main():
        ss = SearchSpace()
        assert ss.validate()
        supernet = build_supernet()
        supernet.set_sample_config(ss.all_original())
        layer0 = supernet.layers[0]
        for name, branch in layer0.branches.items():
            print(f"{name}: params={branch.elastic_num_params}")

    if __name__ == "__main__":
        main()
    """
)

TOY_MANIFEST = textwrap.dedent(
    """\
    ---
    source_project_root: /tmp/toy
    ---

    ## Project Overview

    toy transformer classification

    ## Model

    toy_flat.py::ToyTransformer; ckpt toy_ckpt.pt（裸 state_dict）

    ## Training And Evaluation

    metric: toy_acc (higher-better). Evaluation entry: toy evaluate()

    ## Data And Environment

    synthetic int tokens

    ## Relevant Source Files

    toy_flat.py
    """
)


def _save(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _write_toy_ckpt(artifacts_dir: Path, wrap: bool = False) -> Path:
    """写 toy 预训练 ckpt（固定 seed 构造原模型后存 state_dict）。"""
    import importlib.util

    flat_path = artifacts_dir / "toy_flat.py"
    spec = importlib.util.spec_from_file_location("_toy_flat_for_ckpt", flat_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    torch.manual_seed(0)
    model = mod.ToyTransformer()
    sd = model.state_dict()
    if wrap:
        sd = {"state_dict": sd}
    out = artifacts_dir / "toy_ckpt.pt"
    torch.save(sd, out)
    return out


def write_toy_flatten_artifacts(artifacts_dir: Path, bad_loader: bool = False) -> dict[str, Path]:
    """落盘 flatten 侧 toy 产物（flat + manifest + load_pretrained + ckpt）。"""
    paths = {
        "flat": _save(artifacts_dir / "toy_flat.py", TOY_PREPARED_PY),
        "manifest": _save(artifacts_dir / "project_manifest.md", TOY_MANIFEST),
        "load_pretrained": _save(
            artifacts_dir / "load_pretrained.py",
            TOY_LOAD_PRETRAINED_BAD_PY if bad_loader else TOY_LOAD_PRETRAINED_PY,
        ),
    }
    _write_toy_ckpt(artifacts_dir, wrap=True)
    return paths


def write_toy_expand_artifacts(artifacts_dir: Path, bad_supernet: bool = False,
                               with_inspect: bool = True) -> dict[str, Path]:
    """落盘 expand 侧 toy 产物（supernet + inspect + baseline + loader + ckpt + flat）。"""
    paths = write_toy_flatten_artifacts(artifacts_dir)
    paths["supernet"] = _save(
        artifacts_dir / "supernet.py",
        TOY_SUPERNET_BAD_PY if bad_supernet else TOY_SUPERNET_PY,
    )
    if with_inspect:
        paths["inspect"] = _save(artifacts_dir / "inspect_supernet.py", TOY_INSPECT_PY)
    paths["baseline"] = _save(
        artifacts_dir / ".baseline.json",
        '{"depth": 2, "internal_dims": {"global_dim": 32, "head_dim": 8, '
        '"num_heads": 4, "ffn_dim": 64, "max_seq_len": 8}}',
    )
    return paths


def run_script(cmd: list[str], artifacts_dir: Path, cwd: Path | None = None) -> dict:
    """跑一个 gate 脚本，返回 {rc, stdout, stderr}（env 注入 ORCA_ARTIFACTS_DIR）。"""
    import subprocess

    env = {**os.environ, "ORCA_ARTIFACTS_DIR": str(artifacts_dir)}
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(cwd or artifacts_dir))
    return {"rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
