"""test_smoke.py —— kd-nas-demo 契约 + 集成 smoke（pytest 兼容 + 可直跑）。

机器化 README「组件自验」第 1 步（原本只是手动 ``python -c``），守护 demo 契约不被回归：
  - 4 变体 + baseline 的 I/O 契约（DUMMY_INPUT / BUILD_FN / KNOBS schema）；
  - build_model() 默认 cfg 前向 + 输出同形；
  - feature_hook_names 恒 2 个（KD OFD/FitNets 要求与 teacher 等长）。

跑法::

    pytest examples/kd-nas-demo/test_smoke.py -v
    # 或直跑（无 pytest 也能跑）：
    python3 examples/kd-nas-demo/test_smoke.py

不在本 smoke 内：workflow 集成脚本（teacher_setup / tune_latency / train_adapter / gpu_probe）
的端到端验证——那些见 README「组件自验」第 2~6 步（产 ckpt / teacher_cache，需 onnxruntime +
KD 库，慢），由人工或 CI 的 E2E 阶段执行。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_DEMO_DIR = Path(__file__).resolve().parent
_RECEIVER_DIR = _DEMO_DIR / "knowledge_base" / "families" / "receiver"

# 4 变体 + baseline（None = baseline_model.py 在 demo 根，不在 KB receiver 目录）。
VARIANT_PATHS = [
    _RECEIVER_DIR / "demo_tiny_tf.py",
    _RECEIVER_DIR / "demo_tiny_alt.py",
    _RECEIVER_DIR / "demo_tiny_cnn_pw.py",
    _RECEIVER_DIR / "demo_tiny_cnn_dil.py",
    _DEMO_DIR / "baseline_model.py",
]

_VALID_LEVERAGES = {"high", "medium", "low"}
_EXPECTED_DUMMY_SHAPE = [1, 4, 48, 64, 1]


def _load(path: Path):
    """按路径 import .py（镜像 pick_variant._load_variant）；确保同目录入 sys.path。"""
    path = path.resolve()
    model_dir = str(path.parent)
    if model_dir not in sys.path:
        sys.path.insert(0, model_dir)
    spec = importlib.util.spec_from_file_location(path.stem, str(path))
    assert spec is not None and spec.loader is not None, f"无法构造 spec: {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("path", VARIANT_PATHS, ids=lambda p: p.stem)
def test_io_contract(path):
    """§1 契约：DUMMY_INPUT.shape / BUILD_FN / KNOBS schema（pick_variant._validate_variant 镜像）。"""
    mod = _load(path)
    assert mod.DUMMY_INPUT == {"shape": _EXPECTED_DUMMY_SHAPE, "dtype": "float32"}, (
        f"{path.name} DUMMY_INPUT 非 {{'shape':[1,4,48,64,1],'dtype':'float32'}}：{mod.DUMMY_INPUT}"
    )
    assert mod.BUILD_FN == "build_model", f"{path.name} BUILD_FN 非 build_model"
    assert isinstance(mod.KNOBS, dict) and mod.KNOBS, f"{path.name} KNOBS 非非空 dict"
    for k_name, kn in mod.KNOBS.items():
        assert isinstance(kn, dict), f"{path.name} KNOBS[{k_name}] 非 dict"
        assert kn["step"] < 0, f"{path.name} KNOBS[{k_name}].step={kn['step']} 需 <0"
        assert kn["leverage"] in _VALID_LEVERAGES, (
            f"{path.name} KNOBS[{k_name}].leverage={kn['leverage']!r} 不在 {_VALID_LEVERAGES}"
        )
        assert isinstance(kn["default"], int) and isinstance(kn["min"], int), (
            f"{path.name} KNOBS[{k_name}] default/min 需 int"
        )
        assert kn["default"] >= kn["min"], f"{path.name} KNOBS[{k_name}] default<min"


@pytest.mark.parametrize("path", VARIANT_PATHS, ids=lambda p: p.stem)
def test_knobs_num_blocks_min_ge_2(path):
    """demo 约定：num_blocks.min ≥ 2（保 feature_hook_names 两 hook distinct，免 KD OFD 退化）。"""
    mod = _load(path)
    if "num_blocks" in mod.KNOBS:
        assert mod.KNOBS["num_blocks"]["min"] >= 2, (
            f"{path.name} num_blocks.min={mod.KNOBS['num_blocks']['min']} 需 ≥2（demo 约定）"
        )


@pytest.mark.parametrize("path", VARIANT_PATHS, ids=lambda p: p.stem)
def test_build_forward_shape(path):
    """build_model(**KNOBS.default) 前向 + 输出同形 [1,4,48,64,1]（I/O 契约）。"""
    import torch
    mod = _load(path)
    cfg = {k: kn["default"] for k, kn in mod.KNOBS.items()}
    net = mod.build_model(**cfg)
    net.eval()
    x = torch.randn(*_EXPECTED_DUMMY_SHAPE)
    with torch.no_grad():
        y = net(x)
    assert y.shape == x.shape, f"{path.name} 输出 shape {tuple(y.shape)} != 输入 {tuple(x.shape)}"


@pytest.mark.parametrize("path", VARIANT_PATHS, ids=lambda p: p.stem)
def test_feature_hook_names_len_2(path):
    """feature_hook_names 恒 2 个（与 teacher 等长，KD OFD/FitNets 要求，否则 compose.prepare raise）。"""
    mod = _load(path)
    cfg = {k: kn["default"] for k, kn in mod.KNOBS.items()}
    net = mod.build_model(**cfg)
    hooks = net.feature_hook_names()
    assert isinstance(hooks, list) and len(hooks) == 2, (
        f"{path.name} feature_hook_names 长度={len(hooks)}，期望 2"
    )
    # hook 名应真实存在于 model.named_modules()（KD wrapper 注册 forward hook 时会校验）。
    named = dict(net.named_modules())
    for h in hooks:
        assert h in named, f"{path.name} hook {h!r} 不在 named_modules()"


# ---------------------------------------------------------------------------
# 直跑入口（无 pytest 也能跑：python3 test_smoke.py）
# ---------------------------------------------------------------------------
def _run_all() -> int:
    failures = 0
    fn_and_args = [
        ("test_io_contract", VARIANT_PATHS),
        ("test_knobs_num_blocks_min_ge_2", VARIANT_PATHS),
        ("test_build_forward_shape", VARIANT_PATHS),
        ("test_feature_hook_names_len_2", VARIANT_PATHS),
    ]
    for fn_name, paths in fn_and_args:
        fn = globals()[fn_name]
        for p in paths:
            try:
                fn(p)
                print(f"  PASS  {fn_name}[{p.stem}]")
            except Exception as e:
                failures += 1
                print(f"  FAIL  {fn_name}[{p.stem}]: {type(e).__name__}: {e}")
    return 1 if failures else 0


if __name__ == "__main__":
    if "pytest" not in sys.modules:
        sys.exit(_run_all())
    # 经 pytest 跑时 __main__ 不重复（pytest 收集 test_* 函数）。
