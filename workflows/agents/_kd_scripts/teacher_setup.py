"""teacher_setup.py —— teacher 节点确定性后端（契约 §4）。

对齐 `workflows/agents/_kd_scripts/CONTRACTS.md` §4 `teacher_setup`。

**本脚本是确定性部分**（6 层编辑 + 从头训由 teacher_setup agent 节点先做完，本脚本拿训好的
ckpt）。负责：

    1. 动态 import teacher model（`build_fn`）+ load ckpt（冻结）；
    2. 探测 teacher 倒数第二非叶子模块 + 最后一层，记录 `hook_names`（供 KDStudentWrapper /
       TeacherCache 对齐中间 feature）；
    3. 一次性前向 sanity check（用 dummy_input / proxy_dataset_spec）——验证 hook 能取到 feature；
    4. 存 `teacher_cache.pt`（= `{teacher_state_dict, hook_names, teacher_model_path,
       build_fn, dummy_input, feature_dims, latency_ms, accuracy}`）；
       **格式决策（task override CONTRACTS §3）**：TeacherCache.load 重建 teacher 并直接
       teacher(x)（teacher 始终在线），不做预缓存查表。
    5. 导 teacher ONNX + 测 latency（复用 _struct_scripts/export_onnx.py +
       `--latency_provider module::func`）；
    6. 跑 `--eval_command` 测 teacher accuracy（解析 TEACHER_ACCURACY/NMSE/MSE/BER/SNR）；
    7. 写 `teacher_meta.json` + 结构化 stdout。

CLI（契约 §4）::

    python3 teacher_setup.py \\
      --teacher_model_path <6层 model.py> --teacher_ckpt <ckpt> \\
      --build_fn <fn> --dummy_input '<json>' \\
      --eval_command "<cmd>" --proxy_dataset_spec '<json>' \\
      --output_dir <dir> --opset 17 \\
      --latency_provider "path/to/latency_onnxrt.py::measure"

stdout::

    TEACHER_LATENCY_MS: <float>
    TEACHER_ACCURACY: <float>
    TEACHER_DB_BASELINE: 0.0
    TEACHER_ONNX: <abs path>
    TEACHER_CACHE: <abs path>
    TEACHER_META: <abs path>

fail loud：任何 import/ckpt/ONNX/latency 失败 → 非零退出 + stderr。
eval_command 非零退出 → fail loud；解析不出精度 → accuracy_confidence=low（不致命）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any


# ── 动态加载 teacher model.py（与 export_onnx.py 同款 importlib）────────────────
def _load_teacher_module(model_path: str, build_fn: str):
    """importlib.util.spec_from_file_location 加载 teacher model.py，返回 (mod, factory)。"""
    import torch.nn as nn

    model_path = os.path.abspath(model_path)
    if not os.path.isfile(model_path):
        raise FileNotFoundError(f"teacher_model_path 不存在: {model_path}")

    model_dir = os.path.dirname(model_path)
    module_name = Path(model_path).stem
    if model_dir not in sys.path:
        sys.path.insert(0, model_dir)
    spec = importlib.util.spec_from_file_location(module_name, model_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {model_path} 构造 import spec")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)

    if not hasattr(mod, build_fn):
        raise AttributeError(
            f"{module_name} 无属性 {build_fn!r}；可用: "
            f"{[n for n in dir(mod) if not n.startswith('_')]}"
        )
    factory = getattr(mod, build_fn)
    if not callable(factory):
        raise TypeError(f"{build_fn} 不可调用（{type(factory)}）")
    return mod, factory


# ── 复用 _struct_scripts/export_onnx.export_onnx ───────────────────────────────
def _export_onnx(model_path, build_fn, dummy_input, opset, out):
    here_struct = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "_struct_scripts"
    )
    here_struct = os.path.abspath(here_struct)
    if here_struct not in sys.path:
        sys.path.insert(0, here_struct)
    from export_onnx import export_onnx  # type: ignore

    return export_onnx(
        model_path=model_path,
        build_fn=build_fn,
        dummy_input=dummy_input,
        opset=opset,
        out=out,
        device="cpu",
    )


def _load_measure(latency_provider: str):
    """`/abs/path/file.py::measure` → callable。"""
    if "::" not in latency_provider:
        raise ValueError(f"latency_provider 需 'path::func'，得到 {latency_provider!r}")
    path, func = latency_provider.split("::", 1)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"latency_provider 文件不存在: {path}")
    spec = importlib.util.spec_from_file_location("cost_model", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    measure = getattr(mod, func, None)
    if not callable(measure):
        raise TypeError(f"{path}::{func} 不是 callable")
    return measure


# ── hook 探测 ──────────────────────────────────────────────────────────────────
def _discover_hook_names(teacher) -> list[str]:
    """取 teacher 的倒数第二 + 最后一个非叶子模块名。

    非叶子 = 有 child module（`list(mod.children())` 非空）。
    至少返回 1 个；不足时退回最后一个叶子模块名。
    """
    import torch.nn as nn

    non_leaf = [
        (name, mod) for name, mod in teacher.named_modules()
        if mod is not teacher and list(mod.children())
    ]
    if len(non_leaf) >= 2:
        return [non_leaf[-2][0], non_leaf[-1][0]]
    if len(non_leaf) == 1:
        return [non_leaf[-1][0]]
    # fallback：取所有叶子最后一个
    leaves = [(n, m) for n, m in teacher.named_modules() if not list(m.children())]
    if not leaves:
        raise RuntimeError("teacher 无可 hook 的子模块")
    return [leaves[-1][0]]


def _register_hooks(teacher, hook_names: list[str]):
    """注册 forward hook，返回 (handles, sink_dict)。sink 记录每次 forward 的 feature。"""
    sink: dict[str, Any] = {}
    handles = []
    name_to_mod = dict(teacher.named_modules())

    def _make_hook(name: str):
        def _hook(_mod, _inp, out):
            sink[name] = out
        return _hook

    for n in hook_names:
        if n not in name_to_mod:
            raise KeyError(
                f"hook 目标模块 {n!r} 不在 teacher；可用: "
                f"{list(name_to_mod.keys())[-10:]}"
            )
        handles.append(name_to_mod[n].register_forward_hook(_make_hook(n)))
    return handles, sink


# ── proxy_dataset_spec 解析（默认随机正态 fallback）────────────────────────────
def _build_proxy_batch(spec_raw: str, dummy_input_raw: str):
    """根据 proxy_dataset_spec 造一个 batch 做 sanity check forward。

    支持两种形态：
      - {"shape":[...],"dtype":"float32","n_batches":N}：随机正态（默认）
      - {"from_eval_loader":true,...}：当前不接入用户 eval loader（需 train_kd.py 侧处理），
        本脚本 fallback 到随机正态 + 记录 warning。

    返回 (batch_tensor, spec_used_dict)。
    """
    import torch

    if spec_raw and spec_raw.strip():
        if os.path.isfile(spec_raw):
            text = Path(spec_raw).read_text(encoding="utf-8")
        else:
            text = spec_raw
        try:
            spec = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"proxy_dataset_spec 非合法 JSON: {e}\n原文：{text!r}") from e
    else:
        spec = {}

    if spec.get("from_eval_loader"):
        # 不在本脚本职责内（需用户 train loader），fallback 到随机正态。
        spec = {
            "shape": _dummy_shape(dummy_input_raw),
            "dtype": "float32",
            "n_batches": spec.get("max_batches", 1) or 1,
            "_fallback_reason": "from_eval_loader 未接入，已回退随机正态",
        }

    shape = spec.get("shape") or _dummy_shape(dummy_input_raw)
    if not isinstance(shape, list) or not shape:
        raise ValueError(f"proxy_dataset_spec.shape 非法: {shape!r}")
    dtype_name = spec.get("dtype", "float32")

    dtype_table = {
        "float32": torch.float32, "float": torch.float32,
        "float64": torch.float64, "double": torch.float64,
        "float16": torch.float16, "half": torch.float16,
    }
    dtype = dtype_table.get(dtype_name, torch.float32)
    # batch 维度用 n_batches 覆盖 shape[0]
    n_batches = int(spec.get("n_batches", 1) or 1)
    shape = [n_batches] + list(shape[1:])
    batch = torch.randn(*shape, dtype=dtype)
    return batch, spec


def _dummy_shape(dummy_input_raw: str) -> list[int]:
    """从 dummy_input JSON 抽 shape（fallback [1,4,48,64,1]）。"""
    if not dummy_input_raw or not dummy_input_raw.strip():
        return [1, 4, 48, 64, 1]
    if os.path.isfile(dummy_input_raw):
        text = Path(dummy_input_raw).read_text(encoding="utf-8")
    else:
        text = dummy_input_raw
    try:
        d = json.loads(text)
        if isinstance(d, dict) and isinstance(d.get("shape"), list):
            return list(d["shape"])
    except json.JSONDecodeError:
        pass
    return [1, 4, 48, 64, 1]


# ── eval_command accuracy 解析 ─────────────────────────────────────────────────
_ACC_PATTERNS = [
    (re.compile(r"TEACHER_ACCURACY\s*[:=]\s*([0-9]*\.?[0-9]+)", re.I), "acc"),
    (re.compile(r"\baccuracy\s*[:=]\s*([0-9]*\.?[0-9]+)", re.I), "acc"),
    (re.compile(r"\bNMSE\s*[:=]\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)", re.I), "nmse"),
    (re.compile(r"\bMSE\s*[:=]\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)", re.I), "mse"),
    (re.compile(r"\bBER\s*[:=]\s*([0-9]*\.?[0-9]+)", re.I), "ber"),
    (re.compile(r"\bSNR[_-]?dB\s*[:=]\s*([-+]?[0-9]*\.?[0-9]+)", re.I), "snr"),
    (re.compile(r"\bSNR\s*[:=]\s*([-+]?[0-9]*\.?[0-9]+)", re.I), "snr"),
]


def _parse_accuracy(stdout: str) -> tuple[float, str, str]:
    """从 eval stdout 解析精度。返回 (value, kind, confidence)。

    confidence = 'high'（命中 TEACHER_ACCURACY/NMSE/MSE/BER/SNR/accuracy）或 'low'（解析失败→占位 0.0）。
    """
    for line in stdout.splitlines()[::-1]:
        s = line.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                d = json.loads(s)
                for k, kind in (("accuracy", "acc"), ("acc", "acc"),
                                ("nmse", "nmse"), ("mse", "mse"),
                                ("ber", "ber"), ("snr", "snr"), ("snr_db", "snr")):
                    if k in d and isinstance(d[k], (int, float)):
                        return float(d[k]), kind, "high"
            except json.JSONDecodeError:
                pass
    for pat, kind in _ACC_PATTERNS:
        m = pat.search(stdout)
        if m:
            return float(m.group(1)), kind, "high"
    return 0.0, "unknown", "low"


def _run(cmd: str, cwd: str) -> str:
    """shell 执行用户命令，返回 stdout。fail loud on 非零退出。"""
    proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"eval_command 非零退出({proc.returncode}): {cmd}\nstderr:\n{proc.stderr[-1000:]}"
        )
    return proc.stdout


# ── 主流程 ────────────────────────────────────────────────────────────────────
def teacher_setup(args) -> dict:
    import torch

    out_dir = os.path.abspath(args.output_dir)
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    # 1. 动态 import teacher + build + load ckpt
    mod, factory = _load_teacher_module(args.teacher_model_path, args.build_fn)
    teacher = factory()
    if not isinstance(teacher, torch.nn.Module):
        raise TypeError(f"{args.build_fn}() 返回 {type(teacher).__name__}，期望 nn.Module")
    teacher = teacher.eval()
    teacher.requires_grad_(False)

    if args.teacher_ckpt and args.teacher_ckpt.strip():
        ckpt_path = os.path.abspath(args.teacher_ckpt)
        if not os.path.isfile(ckpt_path):
            raise FileNotFoundError(f"teacher_ckpt 不存在: {ckpt_path}")
        ckpt = torch.load(ckpt_path, map_location="cpu")
        sd = ckpt.get("state_dict", ckpt) if isinstance(ckpt, dict) else ckpt
        missing, unexpected = teacher.load_state_dict(sd, strict=False)
        if missing:
            print(f"[teacher_setup] WARN missing keys (top5): {list(missing)[:5]}",
                  file=sys.stderr)
        if unexpected:
            print(f"[teacher_setup] WARN unexpected keys (top5): {list(unexpected)[:5]}",
                  file=sys.stderr)

    # 2. hook 探测
    hook_names = _discover_hook_names(teacher)
    # 若 teacher 自报 feature_hook_names，优先用它
    if hasattr(teacher, "feature_hook_names") and callable(teacher.feature_hook_names):
        try:
            declared = list(teacher.feature_hook_names())
            if declared:
                hook_names = declared
        except Exception:  # noqa: BLE001
            pass

    handles, sink = _register_hooks(teacher, hook_names)

    # 3. sanity forward（验证 hook 可取 feature）
    batch, spec_used = _build_proxy_batch(args.proxy_dataset_spec, args.dummy_input)
    with torch.no_grad():
        out_tensor = teacher(batch)
    feature_dims: dict[str, list[int]] = {}
    for n in hook_names:
        feat = sink.get(n)
        if feat is None:
            raise RuntimeError(
                f"hook {n!r} 未能捕获 feature（forward 未经过该层？）"
            )
        feature_dims[n] = list(feat.shape)
    for h in handles:
        h.remove()

    # 4. 导 ONNX
    teacher_onnx = os.path.join(out_dir, "teacher.onnx")
    _export_onnx(
        args.teacher_model_path, args.build_fn, args.dummy_input,
        args.opset, teacher_onnx,
    )

    # 5. 测 latency
    measure = _load_measure(args.latency_provider)
    latency_ms = float(measure(teacher_onnx))

    # 6. 跑 eval_command
    if args.eval_command and args.eval_command.strip():
        raw = _run(args.eval_command, args.project_root)
        accuracy, kind, confidence = _parse_accuracy(raw)
    else:
        accuracy, kind, confidence = 0.0, "unknown", "low"

    # 7. teacher_cache.pt：state_dict + hook_names + 重建路径 + 元数据
    teacher_cache_path = os.path.join(out_dir, "teacher_cache.pt")
    cache_payload = {
        # TeacherCache.load 契约键（wrapper.py 读取）
        "state_dict": teacher.state_dict(),
        "dummy_input_shape": _dummy_shape(args.dummy_input),
        "teacher_model_path": os.path.abspath(args.teacher_model_path),
        "hook_names": hook_names,
        # 额外元数据（TeacherCache.load 忽略，供调试/其他读取方用）
        "teacher_state_dict": teacher.state_dict(),
        "build_fn": args.build_fn,
        "dummy_input": args.dummy_input,
        "feature_dims": feature_dims,
        "latency_ms": latency_ms,
        "accuracy": accuracy,
        "accuracy_kind": kind,
        "accuracy_confidence": confidence,
        "_format_note": "TeacherCache.load 读 state_dict+dummy_input_shape+teacher_model_path+hook_names 重建 teacher",
    }
    torch.save(cache_payload, teacher_cache_path)

    # 8. teacher_meta.json
    teacher_meta = {
        "teacher_onnx": teacher_onnx,
        "teacher_cache": teacher_cache_path,
        "teacher_latency_ms": latency_ms,
        "teacher_accuracy": accuracy,
        "teacher_accuracy_kind": kind,
        "accuracy_confidence": confidence,
        "teacher_db_baseline": 0.0,  # 自身基准
        "hook_names": hook_names,
        "feature_dims": feature_dims,
        "dummy_input": args.dummy_input,
        "build_fn": args.build_fn,
        "teacher_model_path": os.path.abspath(args.teacher_model_path),
        "teacher_ckpt": os.path.abspath(args.teacher_ckpt) if args.teacher_ckpt else "",
        "opset": args.opset,
        "proxy_dataset_spec_used": spec_used,
    }
    teacher_meta_path = os.path.join(out_dir, "teacher_meta.json")
    Path(teacher_meta_path).write_text(
        json.dumps(teacher_meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return {
        "teacher_onnx": teacher_onnx,
        "teacher_cache": teacher_cache_path,
        "teacher_meta": teacher_meta_path,
        "teacher_latency_ms": latency_ms,
        "teacher_accuracy": accuracy,
        "teacher_db_baseline": 0.0,
        "accuracy_confidence": confidence,
    }


def _main() -> int:
    p = argparse.ArgumentParser(
        description="teacher_setup 确定性后端（契约 §4）"
    )
    p.add_argument("--teacher_model_path", required=True,
                   help="6 层 teacher model.py 绝对路径")
    p.add_argument("--teacher_ckpt", required=True, help="teacher ckpt 路径")
    p.add_argument("--build_fn", required=True, help="model.py 内 build 函数名")
    p.add_argument("--dummy_input", required=True,
                   help='JSON 或文件：{"shape":[B,4,48,64,1],"dtype":"float32"}')
    p.add_argument("--eval_command", required=True,
                   help="测 teacher 精度的 shell 命令")
    p.add_argument("--proxy_dataset_spec", default="",
                   help="JSON：proxy 数据规格；空→随机正态")
    p.add_argument("--output_dir", required=True)
    p.add_argument("--opset", type=int, default=17)
    p.add_argument("--latency_provider", required=True,
                   help="path::func，如 .../latency_onnxrt.py::measure")
    p.add_argument("--project_root", default=".",
                   help="eval_command 的 cwd")
    args = p.parse_args()

    try:
        r = teacher_setup(args)
    except Exception as e:
        print(f"[teacher_setup] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2

    print(f"TEACHER_LATENCY_MS: {r['teacher_latency_ms']:.4f}")
    print(f"TEACHER_ACCURACY: {r['teacher_accuracy']}")
    print(f"TEACHER_DB_BASELINE: {r['teacher_db_baseline']}")
    print(f"TEACHER_ONNX: {r['teacher_onnx']}")
    print(f"TEACHER_CACHE: {r['teacher_cache']}")
    print(f"TEACHER_META: {r['teacher_meta']}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
