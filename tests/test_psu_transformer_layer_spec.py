"""test_psu_transformer_layer_spec.py —— transformer_layer spec 契约测试。

锁定 canonical ``search_space.py`` + choice 契约 gate 的 intent：
  - SearchSpace 三硬约束：①公有 list/tuple 属性仅 ``branch_choices``；②钉死维度标量
    （schema 反射扫描不发现假维度）；③零参构造 + 模块级零副作用。
  - choice-only：``sample()`` 只采 per-layer choice；``all_original()`` 全 original；
    ``validate()`` 校验 original ∈ 分支集。
  - 反向维度 gate（check_choice_contract.py）：任何非 choice 公有容器（含单值元组）
    FAIL；分支集缺 original FAIL；.baseline.json pin 不一致 FAIL；合规空间 PASS。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC_DIR = REPO / "workflows" / "agents" / "psu_expand_supernet" / "references" / "supernet_specs" / "transformer_layer"
CHECK_CHOICE = REPO / "workflows" / "agents" / "psu_expand_supernet" / "scripts" / "check_choice_contract.py"
sys.path.insert(0, str(REPO / "tests"))

from _psu_test_fixtures import write_toy_expand_artifacts  # noqa: E402


def _load_canonical():
    spec = importlib.util.spec_from_file_location(
        "psu_canonical_search_space", SPEC_DIR / "search_space.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["psu_canonical_search_space"] = mod  # dataclass 注解解析需要
    spec.loader.exec_module(mod)
    return mod


# ── 硬约束 ①：公有 list/tuple 属性仅 choice 容器 ────────────────────────────


def test_only_public_container_is_branch_choices():
    """反射扫描（generate_schema 同款口径）发现的搜索维度必须唯一为 branch_choices。"""
    mod = _load_canonical()
    ss = mod.SearchSpace()
    public_containers = {
        attr for attr in dir(ss)
        if not attr.startswith("_") and isinstance(getattr(ss, attr), (list, tuple))
        and len(getattr(ss, attr)) > 0
    }
    assert public_containers == {"branch_choices"}


# ── 硬约束 ②：钉死维度标量 ──────────────────────────────────────────────────


def test_pinned_dims_are_scalars():
    """钉死维度全部标量（单值元组会被反射误报为 type=list 假维度）。"""
    mod = _load_canonical()
    ss = mod.SearchSpace()
    for attr in ("depth", "global_dim", "head_dim", "num_heads", "ffn_dim", "max_seq_len"):
        assert isinstance(getattr(ss, attr), int), f"{attr} 须为 int 标量"
    assert isinstance(ss.activation, str)


# ── 硬约束 ③：零参构造 + 模块级零副作用 ────────────────────────────────────


def test_zero_arg_construction_and_no_side_effects():
    """SearchSpace() 零参可构造；exec 模块（__name__ 非 __main__）不触发 demo 块。"""
    import contextlib
    import io

    import types

    src_path = SPEC_DIR / "search_space.py"
    mod = types.ModuleType("not_main")  # __name__ 非 __main__：demo 块不触发
    mod.__dict__["__file__"] = str(src_path)
    sys.modules["not_main"] = mod
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(compile(src_path.read_text(encoding="utf-8"), str(src_path), "exec"),
             mod.__dict__)
    assert buf.getvalue() == ""
    ss = mod.SearchSpace()  # 零参构造，不需要 ckpt
    assert ss.depth >= 1


# ── choice-only 语义 ───────────────────────────────────────────────────────


def test_sample_only_samples_choices():
    mod = _load_canonical()
    ss = mod.SearchSpace()
    for _ in range(10):
        cfg = ss.sample()
        assert len(cfg.choices) == ss.depth
        assert set(cfg.choices) <= set(ss.branch_choices)
        assert cfg.validate()


def test_all_original_and_validate():
    mod = _load_canonical()
    ss = mod.SearchSpace()
    assert ss.all_original().choices == ("original",) * ss.depth
    assert ss.validate()
    # 缺 original → False；重复分支 → False；单分支 → False
    from dataclasses import replace
    assert not replace(ss, branch_choices=("vanilla", "fnet")).validate()
    assert not replace(ss, branch_choices=("original", "original")).validate()
    assert not replace(ss, branch_choices=("original",)).validate()


# ── 反向维度 gate（check_choice_contract.py 脚本级）────────────────────────


def _run_choice_check(artifacts_dir: Path) -> tuple[int, str]:
    import subprocess
    proc = subprocess.run(
        [sys.executable, str(CHECK_CHOICE), "--artifacts-dir", str(artifacts_dir)],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


GOOD_SPACE = """
from dataclasses import dataclass

BRANCH_CHOICES = ("original", "vanilla", "fnet")

@dataclass
class SearchSpace:
    branch_choices: tuple = BRANCH_CHOICES
    depth: int = 2
    global_dim: int = 32
    num_heads: int = 4

def build_supernet(pretrained_state=None):
    return None
"""

BAD_MULTI_CANDIDATES = GOOD_SPACE.replace(
    "    depth: int = 2", "    depth: int = 2\n    depth_candidates: tuple = (1, 2, 4)"
)

BAD_SINGLE_VALUE_TUPLE = GOOD_SPACE.replace(
    "    num_heads: int = 4", "    num_heads: tuple = (4,)"
)

BAD_NO_ORIGINAL = GOOD_SPACE.replace(
    'BRANCH_CHOICES = ("original", "vanilla", "fnet")',
    'BRANCH_CHOICES = ("vanilla", "fnet")',
)


def test_choice_contract_passes_on_good_space(tmp_path):
    (tmp_path / "supernet.py").write_text(GOOD_SPACE, encoding="utf-8")
    rc, out = _run_choice_check(tmp_path)
    assert rc == 0, out


def test_choice_contract_fails_on_multi_candidates(tmp_path):
    (tmp_path / "supernet.py").write_text(BAD_MULTI_CANDIDATES, encoding="utf-8")
    rc, out = _run_choice_check(tmp_path)
    assert rc == 1
    assert "depth_candidates" in out and "反向维度 gate" in out


def test_choice_contract_fails_on_single_value_tuple(tmp_path):
    """单值元组同样违规（平铺单值元组被反射误报为 type=list 假维度）。"""
    (tmp_path / "supernet.py").write_text(BAD_SINGLE_VALUE_TUPLE, encoding="utf-8")
    rc, out = _run_choice_check(tmp_path)
    assert rc == 1
    assert "num_heads" in out


def test_choice_contract_fails_without_original(tmp_path):
    (tmp_path / "supernet.py").write_text(BAD_NO_ORIGINAL, encoding="utf-8")
    rc, out = _run_choice_check(tmp_path)
    assert rc == 1
    assert "original" in out


def test_choice_contract_baseline_pin_mismatch(tmp_path):
    """.baseline.json 实测值与 SearchSpace 标量不一致 → pin 校验 fail loud。"""
    (tmp_path / "supernet.py").write_text(GOOD_SPACE, encoding="utf-8")
    (tmp_path / ".baseline.json").write_text(
        json.dumps({"depth": 2, "internal_dims": {"num_heads": 8}}), encoding="utf-8"
    )
    rc, out = _run_choice_check(tmp_path)
    assert rc == 1
    assert "pin 校验" in out and "num_heads" in out


def test_choice_contract_on_toy_supernet(tmp_path):
    """expand 全套 toy 产物（真实 supernet.py 形态）上 choice 契约 PASS。"""
    write_toy_expand_artifacts(tmp_path, with_inspect=False)
    rc, out = _run_choice_check(tmp_path)
    assert rc == 0, out
