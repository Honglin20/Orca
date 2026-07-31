"""validate_contract.py —— model-flatten 产出的契约 .py 硬校验（fail loud）。

契约（``workflows/agents/_kd_scripts/CONTRACTS.md`` §1）逐字对齐：
  - ``DUMMY_INPUT = {"shape": [<非空 list>], "dtype": "float32"}``
  - ``BUILD_FN = "build_model"``（字面量）
  - ``KNOBS = {<knob>: {"default","min","step","leverage"}}``（非空 dict；step<0、leverage∈{high,medium,low}）
  - ``def build_model(**cfg) -> nn.Module``（零参用 KNOBS.default；cfg 覆盖旋钮）

校验项（任一不过 → VALIDATION: FAIL + exit 2）：
  1. import 成功（语法 / 依赖缺失 / 顶层异常）
  2. ``BUILD_FN`` 字面量 == ``"build_model"``
  3. ``build_model`` callable
  4. ``DUMMY_INPUT.shape`` 非空 list + ``dtype`` 显式声明且是合法 torch dtype 名（CONTRACTS §1 要求）
  5. ``KNOBS`` 非空 dict + 每 knob 字段齐全 + step<0 + leverage 合法 + default/min 数值（排除 bool）
  6. ``build_model(**defaults)`` 实例化 + ``.to(device)`` 成功（与 device 解析分阶段归因）
  7. forward 出来的 shape ``== DUMMY_INPUT["shape"]``（device 可移植自检）
  8. ``build_model(**mins)`` 也能 forward —— min 是结构地板，gate.tune_latency 会缩到这里
     （SKILL.md Step 5 宣称「Step 6 hard-validation will catch invalid min」由本步闭环）

确定性脚本（rule 5）：无 LLM、无网络、不读时钟。stdout emit ``KEY: value`` 行供
agent.md 解析；非零退出 = fail loud（不假装成功）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

# 与 _kd_scripts/kd_common.py RANK 同款（避免跨包依赖，本地复制；变更需同步）。
RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}
_VALID_LEVERAGE = set(RANK)
REQUIRED_KNOB_FIELDS = ("default", "min", "step", "leverage")


def _emit(key: str, value: Any) -> None:
    """stdout ``KEY: value`` 行（agent.md awk/cut 解析）。value 非 str → JSON 串。"""
    if isinstance(value, str):
        print(f"{key}: {value}")
    else:
        print(f"{key}: {json.dumps(value, ensure_ascii=False)}")


def _fail(reason: str) -> int:
    """emit FAIL + reason，exit 2（fail loud）。"""
    _emit("VALIDATION", "FAIL")
    _emit("FAIL_REASON", reason)
    return 2


def _load_module(path: str) -> Any:
    """import .py 文件为 module（不入 sys.modules 持久化——校验只跑一次）。"""
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"contract 文件不存在：{p}")
    spec = importlib.util.spec_from_file_location(f"_flatten_contract_{p.stem}", str(p))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {p} 创建 module spec")
    mod = importlib.util.module_from_spec(spec)
    # 契约文件的 sibling import（如 _model8_blocks）需要 sys.path 含其目录。
    parent = str(p.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec.loader.exec_module(mod)
    return mod


def _resolve_device(device_arg: str) -> Any:
    """torch.device 解析（auto → cuda→npu→cpu，对齐 _kd_scripts/_device.py 顺位）。

    显式串（cuda/npu/cpu/cuda:1）原样解析。NPU = Ascend + CANN（torch_npu 包）；
    torch_npu 未装 → 跳过 NPU 分支。不 import _kd_scripts（model-flatten 保 standalone）。
    """
    import torch

    if device_arg and device_arg != "auto":
        return torch.device(device_arg)
    if torch.cuda.is_available():
        return torch.device("cuda")
    try:
        import torch_npu  # noqa: F401
        if hasattr(torch, "npu") and torch.npu.is_available():
            return torch.device("npu")
    except ImportError:
        pass
    return torch.device("cpu")


def validate_contract(path: str, device_arg: str = "auto", seed: int = 0) -> dict[str, Any]:
    """纯函数校验：返回 {ok, reason, dummy_input, knobs, build_fn, forward_shape}。

    任一契约不符 → ok=False + reason（caller emit FAIL）。raise = caller 包成 FAIL。
    """
    import torch

    mod = _load_module(path)

    # 2) BUILD_FN 字面量
    build_fn = getattr(mod, "BUILD_FN", None)
    if build_fn != "build_model":
        return {"ok": False, "reason": f"BUILD_FN 必须是 'build_model'（得到 {build_fn!r}）"}

    # 3) build_model callable
    build_fn_obj = getattr(mod, "build_model", None)
    if not callable(build_fn_obj):
        return {"ok": False, "reason": "模块缺 callable build_model（契约必备）"}

    # 4) DUMMY_INPUT.shape 非空 list + dtype 必须显式声明（CONTRACTS §1：dtype 是契约的一部分，不默认）
    di = getattr(mod, "DUMMY_INPUT", None)
    if not isinstance(di, dict) or not isinstance(di.get("shape"), list) or not di["shape"]:
        return {"ok": False, "reason": "DUMMY_INPUT 缺 shape（非空 list）——禁硬编码回退"}
    if "dtype" not in di:
        return {"ok": False, "reason": "DUMMY_INPUT 缺 dtype（CONTRACTS §1 要求显式声明，如 'float32'）"}
    dtype_str = di["dtype"]
    if not isinstance(dtype_str, str) or not hasattr(torch, dtype_str):
        return {"ok": False, "reason": f"DUMMY_INPUT.dtype={dtype_str!r} 不是合法 torch dtype 名（如 'float32'）"}
    dummy_input = di
    expected_shape = list(di["shape"])

    # 5) KNOBS 非空 dict + 字段齐全
    knobs = getattr(mod, "KNOBS", None)
    if not isinstance(knobs, dict) or not knobs:
        return {"ok": False, "reason": "KNOBS 必须是非空 dict（flatten 须识别至少一个可调维度）"}
    for k, kn in knobs.items():
        if not isinstance(kn, dict):
            return {"ok": False, "reason": f"KNOBS[{k!r}] 不是 dict"}
        for field in REQUIRED_KNOB_FIELDS:
            if field not in kn:
                return {"ok": False, "reason": f"KNOBS[{k!r}] 缺字段 {field!r}"}
        if not isinstance(kn["step"], (int, float)) or kn["step"] >= 0:
            return {"ok": False, "reason": f"KNOBS[{k!r}].step 必须 <0（缩容方向；得到 {kn['step']!r}）"}
        if kn["leverage"] not in _VALID_LEVERAGE:
            return {"ok": False, "reason": f"KNOBS[{k!r}].leverage={kn['leverage']!r} 非法；须 ∈ {sorted(_VALID_LEVERAGE)}"}
        # bool 是 int 子类，须显式排除（True/False 不应作 default/min）
        for num_field in ("default", "min"):
            v = kn[num_field]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return {"ok": False, "reason": f"KNOBS[{k!r}].{num_field} 须为数值（得到 {v!r}）"}

    # 6) build_model(**defaults) 实例化（device 解析 / 实例化 / .to(device) 分阶段归因）
    torch.manual_seed(seed)
    defaults = {k: v["default"] for k, v in knobs.items()}
    try:
        device = _resolve_device(device_arg)
    except Exception as e:  # noqa: BLE001 — device 串非法（极少见）
        return {"ok": False, "reason": f"device 解析失败（{device_arg!r}）：{type(e).__name__}: {e}"}
    try:
        model = build_fn_obj(**defaults)
        model = model.to(device)
    except Exception as e:  # noqa: BLE001 — fail loud 须捕获实例化所有异常
        return {"ok": False, "reason": f"build_model(**defaults) 实例化失败：{type(e).__name__}: {e}"}

    # 7) forward shape == DUMMY_INPUT.shape
    dtype = getattr(torch, dtype_str)
    try:
        dummy = torch.randn(*expected_shape, dtype=dtype, device=device)
        with torch.no_grad():
            out = model(dummy)
        actual_shape = list(out.shape)
    except Exception as e:  # noqa: BLE001 — dummy 创建 + forward 任一异常 = FAIL
        return {"ok": False, "reason": f"forward 失败：{type(e).__name__}: {e}"}

    if actual_shape != expected_shape:
        return {
            "ok": False,
            "reason": f"forward shape {actual_shape} != DUMMY_INPUT.shape {expected_shape}",
        }

    # 8) build_model(**mins) 也能 forward —— min 是结构地板，gate.tune_latency 会缩到这里
    # （SKILL.md Step 5 宣称「Step 6 hard-validation will catch invalid min」由本步闭环）
    mins = {k: v["min"] for k, v in knobs.items()}
    if mins != defaults:
        try:
            model_min = build_fn_obj(**mins).to(device)
            with torch.no_grad():
                model_min(dummy)
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False,
                "reason": (
                    f"build_model(**mins={mins}) forward 失败：min 不是合法结构地板"
                    f"（tune_latency 会缩到这里）：{type(e).__name__}: {e}"
                ),
            }

    return {
        "ok": True,
        "reason": "",
        "build_fn": build_fn,
        "dummy_input": dummy_input,
        "knobs": knobs,
        "forward_shape": actual_shape,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="model-flatten 契约硬校验（fail loud）")
    parser.add_argument("--contract", required=True, help="展平产出的契约 .py 绝对路径")
    parser.add_argument("--device", default="auto", help="forward 校验设备（auto/cuda/npu/cpu，默认 auto）")
    parser.add_argument("--seed", type=int, default=0, help="实例化种子（默认 0）")
    args = parser.parse_args()

    try:
        result = validate_contract(args.contract, device_arg=args.device, seed=args.seed)
    except Exception as e:  # noqa: BLE001 — 任何未捕获异常 = FAIL（不假装成功）
        return _fail(f"校验异常：{type(e).__name__}: {e}")

    if not result["ok"]:
        return _fail(result["reason"])

    # PASS 路径：emit 全字段
    _emit("IMPORT_OK", args.contract)
    _emit("BUILD_FN", result["build_fn"])
    _emit("DUMMY_INPUT", result["dummy_input"])
    _emit("KNOBS", result["knobs"])
    _emit("FORWARD_SHAPE", result["forward_shape"])
    _emit("SHAPE_MATCH", "true")
    _emit("VALIDATION", "PASS")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
