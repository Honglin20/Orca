"""test_student.py —— 测 student 精度（NMSE），打印 STUDENT_ACCURACY 等。

读环境变量（measure_student.py 经 env 注入后 shell 执行本脚本）：
  - ``STUDENT_CKPT``：student ckpt 绝对路径（train_adapter 产物）。
  - ``STUDENT_MODEL_PATH``：student 变体 .py 绝对路径。

在**随机数据**上算真实 NMSE（``||out - target||² / ||target||²``）。

输出（measure_student 解析；见 measure_student._parse_accuracy）::

    STUDENT_ACCURACY: <nmse>        # 人类可读（measure_student 不直接解析此行）
    STUDENT_ACCURACY_KIND: nmse
    MET_ACCURACY: true|false        # 自评（measure_student 会用 --accuracy_baseline 重判）
    {"nmse": <nmse>}                # JSON 行（末行优先解析 → 稳定检 nmse kind，免 WARN）

measure_student 的 JSON-line 扫描（reverse，末行优先）会先命中本文件末行的 ``{"nmse": ...}``
→ 返回 (value, "nmse", "high")；配合 inputs 的 ``accuracy_baseline_kind=nmse`` 锁方向，无 WARN。
"""

from __future__ import annotations

import importlib.util
import math
import os
import sys
from pathlib import Path

import torch

DUMMY_SHAPE = [1, 4, 48, 64, 1]
# demo 自评基线（measure_student 会用 --accuracy_baseline 重判；此处仅装饰性自评，不伪造）。
_DEMO_NMSE_BASELINE = float(os.environ.get("STUDENT_ACCURACY_BASELINE", "1.5"))


def _load_student(model_path: str, ckpt_path: str) -> torch.nn.Module:
    """按路径 import 变体 .py + 读 ckpt + build + load_state_dict。"""
    model_path = os.path.abspath(model_path)
    if not os.path.isfile(model_path):
        raise FileNotFoundError(f"STUDENT_MODEL_PATH 不存在: {model_path}")

    model_dir = os.path.dirname(model_path)
    if model_dir not in sys.path:
        sys.path.insert(0, model_dir)  # 让 `from _demo_blocks import ...` 生效
    module_name = Path(model_path).stem
    spec = importlib.util.spec_from_file_location(module_name, model_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {model_path} 构造 import spec")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    factory = getattr(mod, "build_model", None)
    if not callable(factory):
        raise AttributeError(f"{model_path} 无 callable build_model")

    ck = torch.load(ckpt_path, map_location="cpu")
    cfg: dict = {}
    if isinstance(ck, dict):
        cfg = ck.get("student_cfg") or ck.get("build_cfg") or {}
        if isinstance(ck.get("student_state_dict"), dict):
            state = ck["student_state_dict"]            # train_adapter_template 产物键
        elif isinstance(ck.get("state_dict"), dict):
            state = ck["state_dict"]                    # 通用 / teacher ckpt 键
        else:
            state = ck                                  # 裸 state_dict
    else:
        state = ck

    model = factory(**cfg) if isinstance(cfg, dict) and cfg else factory()
    if not isinstance(model, torch.nn.Module):
        raise TypeError(f"build_model() 返回 {type(model).__name__}，期望 nn.Module")
    missing, unexpected = model.load_state_dict(state, strict=False)
    if missing:
        print(f"[test_student] WARN missing keys (top5): {list(missing)[:5]}", file=sys.stderr)
    if unexpected:
        print(f"[test_student] WARN unexpected keys (top5): {list(unexpected)[:5]}", file=sys.stderr)
    model.eval()
    return model


def _compute_nmse(model: torch.nn.Module, n_samples: int) -> float:
    """在随机 (x, y) 上算 NMSE = sum((out-y)²) / sum(y²)。真实计算，绝不伪造。"""
    torch.manual_seed(20260725)  # 固定 eval 数据（可复现；不影响训练）
    x = torch.randn(n_samples, *DUMMY_SHAPE[1:])
    y = torch.randn(n_samples, *DUMMY_SHAPE[1:])
    with torch.no_grad():
        out = model(x)
    target = y.view_as(out)
    num = float(torch.sum((out - target) ** 2).item())
    den = float(torch.sum(target ** 2).item()) + 1e-12
    nmse = num / den
    if not math.isfinite(nmse):
        nmse = 1e9  # 非有限（发散）→ 巨值 → MET_ACCURACY=false（不污染 JSON 解析）
    return nmse


def main() -> int:
    ckpt = os.environ.get("STUDENT_CKPT", "").strip()
    model_path = os.environ.get("STUDENT_MODEL_PATH", "").strip()
    if not ckpt or not model_path:
        print(
            "FAIL: 缺环境变量 STUDENT_CKPT / STUDENT_MODEL_PATH（measure_student 应注入）",
            file=sys.stderr,
        )
        return 2
    if not os.path.isfile(ckpt):
        print(f"FAIL: STUDENT_CKPT 文件不存在: {ckpt}", file=sys.stderr)
        return 2

    n_samples = int(os.environ.get("STUDENT_EVAL_SAMPLES", "8"))
    model = _load_student(model_path, ckpt)
    nmse = _compute_nmse(model, n_samples)
    met = bool(nmse <= _DEMO_NMSE_BASELINE)

    print(f"STUDENT_ACCURACY: {nmse:.6f}")
    print(f"STUDENT_ACCURACY_KIND: nmse")
    print(f"MET_ACCURACY: {str(met).lower()}")
    # JSON 行（末行优先）：measure_student 稳定检 nmse kind，免 WARN。
    print('{"nmse": %.6f}' % nmse)
    return 0


if __name__ == "__main__":
    sys.exit(main())
