"""test_model8_students.py —— kd-nas-demo KB 新增 model8 student 变体的深度 smoke。

与 ``test_smoke.py`` 的分工：
  - ``test_smoke.py``：demo KB 全变体（含本批 model8 变体）的基本 I/O 契约网（shape / KNOBS /
    feature_hook 长度）—— 守护「契约不回归」。
  - 本文件：model8 student 变体**特有**的正确性维度（超出基本契约）：
    1. **pick_variant 排序**：主变体 ``00_model8_bn3relu`` 是 glob 字典序第一（任务硬约束）；
    2. **validate_contract 硬校验**：每变体过 ``model-flatten/scripts/validate_contract.py``（PASS）；
    3. **build_model(**defaults) + build_model(**mins)** 都能实例化 + forward（min 是结构地板）；
    4. **backward 梯度（defaults + mins 双 cfg）**：forward + backward 能跑（BN 训练路径无 NaN）；
    5. **norm/act 身份**：BN 变体真含 BatchNorm1d、ReLU 变体真含 ReLU，且**不含**对方类型（防 cfg 漂移）；
    6. **feature_hook_names**：default + min 配置都 2 hook、distinct，default 配置 mid-block 索引正确；
    7. **fail-loud 负测**：非法 norm_type / act_type / 空 block_mtypes 应 raise（Rule 12）；
    8. **原始 model8 组合（ln+gelu）直测**：闭合 (norm × act) 4 组合矩阵（无变体覆盖此组合）。

跑法::

    pytest examples/kd-nas-demo/test_model8_students.py -v
    python3 examples/kd-nas-demo/test_model8_students.py   # 无 pytest 也能跑
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_DEMO_DIR = Path(__file__).resolve().parent
_RECEIVER_DIR = _DEMO_DIR / "knowledge_base" / "families" / "receiver"
_REPO_ROOT = _DEMO_DIR.parent.parent
_VALIDATE_SCRIPT = _REPO_ROOT / "workflows" / "agents" / "model-flatten" / "scripts" / "validate_contract.py"

_EXPECTED_DUMMY_SHAPE = [1, 4, 48, 64, 1]

# 4 个新 model8 student 变体（文件名 stem）；顺序 = 字典序（与 pick_variant._list_variants 一致）。
NEW_VARIANTS = [
    "00_model8_bn3relu",
    "01_model8_bn3gelu",
    "02_model8_ln3relu",
    "03_model8_bn4relu",
]
MAIN_VARIANT = "00_model8_bn3relu"

# (norm_type, act_type, default_num_blocks) 每变体的结构身份（防 cfg 漂移）。
VARIANT_IDENTITY = {
    "00_model8_bn3relu": ("bn", "relu", 3),
    "01_model8_bn3gelu": ("bn", "gelu", 3),
    "02_model8_ln3relu": ("ln", "relu", 3),
    "03_model8_bn4relu": ("bn", "relu", 4),
}

# 让直测 ``_model8_student_blocks`` 的 import 生效（``_load`` 已为变体 import 做同款注入）。
if str(_RECEIVER_DIR) not in sys.path:
    sys.path.insert(0, str(_RECEIVER_DIR))

# cfg_label → KNOBS 字段名（defaults 对应 "default"，mins 对应 "min"）。
_CFG_KEY = {"defaults": "default", "mins": "min"}


def _load(stem: str):
    """按 stem import 变体 .py（镜像 pick_variant._load_variant）；确保同目录入 sys.path。"""
    path = (_RECEIVER_DIR / f"{stem}.py").resolve()
    model_dir = str(path.parent)
    if model_dir not in sys.path:
        sys.path.insert(0, model_dir)
    spec = importlib.util.spec_from_file_location(path.stem, str(path))
    assert spec is not None and spec.loader is not None, f"无法构造 spec: {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_validate_contract():
    """import model-flatten 的 validate_contract.validate_contract 函数（standalone，仅依赖 torch）。"""
    if not _VALIDATE_SCRIPT.is_file():
        pytest.skip(f"validate_contract.py 不存在：{_VALIDATE_SCRIPT}")
    scripts_dir = str(_VALIDATE_SCRIPT.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import validate_contract  # type: ignore[import-not-found]
    return validate_contract.validate_contract


# ---------------------------------------------------------------------------
# 1. pick_variant 排序：主变体字典序第一
# ---------------------------------------------------------------------------
def test_main_variant_is_first_in_glob():
    """主变体 ``00_model8_bn3relu`` 必须是 receiver glob（排除 _*.py）字典序第一。

    pick_variant._list_variants 用 ``sorted(os.listdir(...))`` 排 ``*.py`` 排除 ``_*.py``。
    ``00_*`` (digit 0x30) < ``demo_*`` ('d' 0x64) < ``spt_*`` ('s' 0x73) → 主变体全局第一。
    任务硬约束：「放 KB 第一个」「命名让它字典序最小」。
    """
    names = sorted(
        n for n in os.listdir(_RECEIVER_DIR)
        if n.endswith(".py") and not n.startswith("_")
    )
    assert names, "receiver 目录无 .py 变体"
    assert names[0] == f"{MAIN_VARIANT}.py", (
        f"主变体 {MAIN_VARIANT}.py 不是 glob 第一（实际第一={names[0]!r}，全序前 5={names[:5]})"
    )


# ---------------------------------------------------------------------------
# 2. validate_contract 硬校验（任务要求：每变体过 validate_contract.py PASS）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stem", NEW_VARIANTS, ids=lambda s: s)
def test_validate_contract_pass(stem):
    """每变体过 ``model-flatten/scripts/validate_contract.py``（PASS）——契约硬校验。"""
    validate_contract = _load_validate_contract()
    path = str((_RECEIVER_DIR / f"{stem}.py").resolve())
    result = validate_contract(path, device_arg="cpu", seed=0)
    assert result["ok"], f"{stem} validate_contract FAIL：{result['reason']}"
    assert result["forward_shape"] == _EXPECTED_DUMMY_SHAPE


# ---------------------------------------------------------------------------
# 3. build_model(**defaults) + build_model(**mins) 都能 forward
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stem", NEW_VARIANTS, ids=lambda s: s)
def test_build_defaults_and_mins_forward(stem):
    """build_model(**defaults) 与 build_model(**mins) 都能 forward + 输出同形（min 是结构地板）。"""
    import torch
    mod = _load(stem)
    defaults = {k: kn["default"] for k, kn in mod.KNOBS.items()}
    mins = {k: kn["min"] for k, kn in mod.KNOBS.items()}

    for label, cfg in (("defaults", defaults), ("mins", mins)):
        net = mod.build_model(**cfg)
        net.eval()
        x = torch.randn(*_EXPECTED_DUMMY_SHAPE)
        with torch.no_grad():
            y = net(x)
        assert y.shape == x.shape, (
            f"{stem}[{label}] cfg={cfg} 输出 {tuple(y.shape)} != 输入 {tuple(x.shape)}"
        )


# ---------------------------------------------------------------------------
# 4. backward 梯度（defaults + mins 双 cfg；BN 训练路径关键校验）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cfg_label", ["defaults", "mins"], ids=lambda s: s)
@pytest.mark.parametrize("stem", NEW_VARIANTS, ids=lambda s: s)
def test_backward_gradient_no_nan(stem, cfg_label):
    """forward + backward 能跑（train mode），所有 requires_grad 参数梯度 finite。

    双 cfg 覆盖：``defaults``（变体默认 num_blocks/embed_dim）+ ``mins``（结构地板，
    embed_dim=8 通道数减半的 Conv1d 反向路径）。BN 走 train-mode batch 统计（N=B*num_syms=64，
    稳健）。任务约束：「梯度能跑（不许只改不验）」。
    """
    import torch
    mod = _load(stem)
    cfg = {k: kn[_CFG_KEY[cfg_label]] for k, kn in mod.KNOBS.items()}
    net = mod.build_model(**cfg)
    net.train()  # BN 走 batch 统计（最严路径）
    x = torch.randn(*_EXPECTED_DUMMY_SHAPE)
    y = net(x)
    loss = y.float().mean()
    loss.backward()
    for name, p in net.named_parameters():
        if p.requires_grad:
            assert p.grad is not None, f"{stem}[{cfg_label}] 参数 {name!r} 无梯度"
            assert torch.isfinite(p.grad).all(), f"{stem}[{cfg_label}] 参数 {name!r} 梯度含 NaN/Inf"


# ---------------------------------------------------------------------------
# 5. norm/act 身份验证（防 cfg 漂移：BN 变体真有 BN、ReLU 变体真有 ReLU）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stem", NEW_VARIANTS, ids=lambda s: s)
def test_norm_act_identity(stem):
    """变体真实例化目标 norm/act 类型（防 _NORM_TYPE/_ACT_TYPE 常量写错或 cfg 漂移）。"""
    import torch.nn as nn
    mod = _load(stem)
    exp_norm, exp_act, _ = VARIANT_IDENTITY[stem]
    net = mod.build_model()

    norm_modules = [m for m in net.modules() if isinstance(m, (nn.LayerNorm, nn.BatchNorm1d))]
    act_modules = [m for m in net.modules() if isinstance(m, (nn.GELU, nn.ReLU))]

    assert norm_modules, f"{stem} 无任何 LayerNorm/BatchNorm1d 模块"
    assert act_modules, f"{stem} 无任何 GELU/ReLU 模块"

    if exp_norm == "bn":
        assert any(isinstance(m, nn.BatchNorm1d) for m in norm_modules), (
            f"{stem} 期望含 BatchNorm1d（norm_type=bn），实际 norm 模块={norm_modules[:3]}"
        )
        assert not any(isinstance(m, nn.LayerNorm) for m in norm_modules), (
            f"{stem} norm_type=bn 但出现 LayerNorm（cfg 漂移）"
        )
    else:  # "ln"
        assert any(isinstance(m, nn.LayerNorm) for m in norm_modules), (
            f"{stem} 期望含 LayerNorm（norm_type=ln），实际 norm 模块={norm_modules[:3]}"
        )
        assert not any(isinstance(m, nn.BatchNorm1d) for m in norm_modules), (
            f"{stem} norm_type=ln 但出现 BatchNorm1d（cfg 漂移）"
        )

    if exp_act == "relu":
        assert any(isinstance(m, nn.ReLU) for m in act_modules), (
            f"{stem} 期望含 ReLU（act_type=relu）"
        )
        assert not any(isinstance(m, nn.GELU) for m in act_modules), (
            f"{stem} act_type=relu 但出现 GELU（cfg 漂移）"
        )
    else:  # "gelu"
        assert any(isinstance(m, nn.GELU) for m in act_modules), (
            f"{stem} 期望含 GELU（act_type=gelu）"
        )
        assert not any(isinstance(m, nn.ReLU) for m in act_modules), (
            f"{stem} act_type=gelu 但出现 ReLU（cfg 漂移）"
        )


@pytest.mark.parametrize("stem", NEW_VARIANTS, ids=lambda s: s)
def test_default_num_blocks(stem):
    """default num_blocks 与 VARIANT_IDENTITY 一致（03 变体默认 4 层，其余 3 层）。"""
    mod = _load(stem)
    _, _, exp_blocks = VARIANT_IDENTITY[stem]
    assert mod.KNOBS["num_blocks"]["default"] == exp_blocks, (
        f"{stem} num_blocks.default={mod.KNOBS['num_blocks']['default']} != 期望 {exp_blocks}"
    )


# ---------------------------------------------------------------------------
# 6. feature_hook_names：default + min 配置都 2 hook 且 distinct
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stem", NEW_VARIANTS, ids=lambda s: s)
def test_feature_hook_names_default(stem):
    """default 配置 feature_hook_names 恒 2 hook 且落在 distinct block，hook 名真实存在。

    mid-block 索引校验：03 变体默认 4 层 → 第二 hook = ``main.2``（mid=4//2=2）；
    其余默认 3 层 → ``main.1``（mid=3//2=1）。防 ``feature_hook_names`` 永远返回
    ``["main.0","main.1"]`` 的退化实现（中间层语义未校验）。
    """
    mod = _load(stem)
    cfg = {k: kn["default"] for k, kn in mod.KNOBS.items()}
    net = mod.build_model(**cfg)
    hooks = net.feature_hook_names()
    assert isinstance(hooks, list) and len(hooks) == 2, f"{stem} hook 长度={len(hooks)}，期望 2"
    assert hooks[0] != hooks[1], f"{stem} default 配置两 hook 重复 {hooks}（应 distinct block）"
    _, _, default_n = VARIANT_IDENTITY[stem]
    exp_second = f"main.{max(1, default_n // 2)}"
    assert hooks[1] == exp_second, (
        f"{stem} default n={default_n} 第二 hook 应 {exp_second}（中间层），实际 {hooks[1]!r}"
    )
    named = dict(net.named_modules())
    for h in hooks:
        assert h in named, f"{stem} hook {h!r} 不在 named_modules()"


@pytest.mark.parametrize("stem", NEW_VARIANTS, ids=lambda s: s)
def test_feature_hook_names_min(stem):
    """min 配置（num_blocks=2）feature_hook_names 仍 2 hook 且 distinct。"""
    mod = _load(stem)
    cfg = {k: kn["min"] for k, kn in mod.KNOBS.items()}
    net = mod.build_model(**cfg)
    hooks = net.feature_hook_names()
    assert len(hooks) == 2, f"{stem} min 配置 hook 长度={len(hooks)}，期望 2"
    assert hooks[0] != hooks[1], f"{stem} min 配置两 hook 重复 {hooks}"
    named = dict(net.named_modules())
    for h in hooks:
        assert h in named, f"{stem} hook {h!r} 不在 named_modules()"


# ---------------------------------------------------------------------------
# 7. fail-loud 负测：非法 norm_type / act_type / 空 block_mtypes 应 raise（Rule 12）
#    被测代码 ``_model8_student_blocks.py`` 的 raise 分支零变体覆盖（4 变体永远传合法常量），
#    此处直测共享积木，闭合 fail-loud 路径。
# ---------------------------------------------------------------------------
def test_invalid_norm_type_raises():
    """SignalAttention1D / SignalFeedForward1D 对非法 norm_type raise ValueError（fail loud）。"""
    from _model8_student_blocks import SignalAttention1D, SignalFeedForward1D
    for bad in ("invalid", "", "LN", "batchnorm"):
        with pytest.raises(ValueError, match="norm_type"):
            SignalAttention1D(16, 64, 48, norm_type=bad)
        with pytest.raises(ValueError, match="norm_type"):
            SignalFeedForward1D(16, 64, 48, norm_type=bad)


def test_invalid_act_type_raises():
    """SignalFeedForward1D 对非法 act_type raise ValueError（fail loud）。"""
    from _model8_student_blocks import SignalFeedForward1D
    for bad in ("invalid", "", "GELU", "tanh"):
        with pytest.raises(ValueError, match="act_type"):
            SignalFeedForward1D(16, 64, 48, act_type=bad)


def test_empty_block_mtypes_raises():
    """SignalProcessingTransformer 对空 block_mtypes raise ValueError（fail loud 兜底）。

    KNOBS.min=2 已在变体层挡住空 list，但直构时应 fail loud（Rule 12），不静默产空 main。
    """
    from _model8_student_blocks import SignalProcessingTransformer
    with pytest.raises(ValueError, match="block_mtypes"):
        SignalProcessingTransformer(block_mtypes=[])


# ---------------------------------------------------------------------------
# 8. 直测原始 model8 组合（ln + gelu）—— 闭合 (norm × act) 4 组合矩阵
#    4 变体覆盖了 bn+relu / bn+gelu / ln+relu，但 **ln+gelu（原始 model8）无变体**。
#    ``_model8_student_blocks`` 的默认路径（norm=ln, act=gelu）应与 teacher_model 前向等价。
# ---------------------------------------------------------------------------
def test_ln_gelu_combo_matches_original_model8():
    """ln + gelu（原始 model8）前向 + backward + 2 hook（与 teacher 同构）。"""
    import torch
    from _model8_student_blocks import SignalProcessingTransformer
    net = SignalProcessingTransformer(
        block_mtypes=["t1", "t2", "t1"],  # teacher 同款 t1/t2 交替（3 层，更快）
        in_channels=4, embed_dim=16, num_symbols=64, num_subcarriers=48,
        norm_type="ln", act_type="gelu",
    )
    net.train()
    x = torch.randn(*_EXPECTED_DUMMY_SHAPE)
    y = net(x)
    assert y.shape == x.shape, f"ln+gelu 前向 shape {tuple(y.shape)} != {tuple(x.shape)}"
    # ln+gelu 反向路径无 NaN
    y.float().mean().backward()
    for name, p in net.named_parameters():
        if p.requires_grad:
            assert torch.isfinite(p.grad).all(), f"ln+gelu 参数 {name!r} 梯度含 NaN/Inf"
    hooks = net.feature_hook_names()
    assert len(hooks) == 2 and hooks[0] != hooks[1], f"ln+gelu hooks={hooks}"


# ---------------------------------------------------------------------------
# 直跑入口（无 pytest 也能跑）
# ---------------------------------------------------------------------------
def _run_all() -> int:
    import torch  # noqa: F401  (触发 ImportError 早暴露，而非跑到一半才崩)
    import unittest

    # pytest.skip 在非 pytest 直跑时抛 Skipped（BaseException 子类，不被 except Exception 捕获）；
    # 此处显式识别为 SKIP，不误报 FAIL。
    skip_excs = (unittest.SkipTest, getattr(pytest.skip, "Exception", unittest.SkipTest))

    failures = 0

    def run(fn, *args):
        nonlocal failures
        label = ",".join(str(a) for a in args) if args else ""
        try:
            fn(*args)
            print(f"  PASS  {fn.__name__}[{label}]")
        except skip_excs as e:  # SKIP 不算 FAIL
            print(f"  SKIP  {fn.__name__}[{label}]: {e}")
        except Exception as e:
            failures += 1
            print(f"  FAIL  {fn.__name__}[{label}]: {type(e).__name__}: {e}")

    # 非参数化测试
    for fn in (
        test_main_variant_is_first_in_glob,
        test_invalid_norm_type_raises,
        test_invalid_act_type_raises,
        test_empty_block_mtypes_raises,
        test_ln_gelu_combo_matches_original_model8,
    ):
        run(fn)

    # 参数化测试
    for stem in NEW_VARIANTS:
        run(test_validate_contract_pass, stem)
        run(test_build_defaults_and_mins_forward, stem)
        run(test_norm_act_identity, stem)
        run(test_default_num_blocks, stem)
        run(test_feature_hook_names_default, stem)
        run(test_feature_hook_names_min, stem)
        for cfg_label in ("defaults", "mins"):
            run(test_backward_gradient_no_nan, stem, cfg_label)
    return 1 if failures else 0


if __name__ == "__main__":
    # 直接 ``python3 test_model8_students.py`` 时跑 _run_all；pytest 收集时 __name__ != "__main__"
    # （pytest 以文件名 stem import 模块），此块不执行——故无需 ``"pytest" not in sys.modules`` 守卫
    # （那会让已装 pytest 的直跑变 no-op）。
    sys.exit(_run_all())
