"""validate_teacher.py —— teacher-gen 派生忠实度硬校验（fail loud，确定性脚本）。

teacher 是 baseline 的**纯调参派生**（选项 1：深度轴 ×3 / 宽度轴 ×2，不改架构）。
本脚本对 teacher 文件做 **teacher 专属断言**——契约格式校验由 model-flatten 的
``validate_contract.py`` 复用（teacher 是 KD 变体契约，同门规），此处只校验「派生忠实度」：

  1. **DUMMY_INPUT 逐字一致**：teacher.DUMMY_INPUT == baseline.DUMMY_INPUT（KD 硬约束——
     teacher/student 必须同 I/O shape；任何漂移 → FAIL）。
  2. **派生轴声明存在**：teacher 顶层有 ``DEPTH_AXIS`` / ``WIDTH_AXIS`` 字符串常量（可审计）。
     缺任一 → FAIL。空串允许（baseline 无该类轴时，罕见，须 verifier 复核）。
  3. **深度轴 ×3**：teacher.KNOBS[DEPTH_AXIS].default >= baseline.KNOBS[DEPTH_AXIS].default * 3
     （LLM 取整波动容忍：往上取整不卡；×2 / ×1 → FAIL，不配当 teacher）。
  4. **宽度轴 ×2**：teacher.KNOBS[WIDTH_AXIS].default >= baseline.KNOBS[WIDTH_AXIS].default * 2
     （同上）。
  5. **其余 KNOBS 不变**：非轴 knob 的 default / min / step / leverage 与 baseline 逐字一致
     （teacher 只调轴；动其他 = 改架构，违反纯调参派生）。
  6. **容量上升**：teacher 默认实例的参数总数 > baseline 默认实例的参数总数（防 wrapper bug——
     理论上深度×3/宽度×2 严格放大；CPU Identity 退化模型会卡这里，应被视为不配当 teacher）。

确定性脚本（rule 5）：无 LLM、无网络、不读时钟。stdout emit ``KEY: value`` 行供
agent.md 解析（``DEPTH_AXIS`` / ``WIDTH_AXIS`` / ``CAPACITY_RATIO`` 等）；非零退出 = fail loud。

**Helper 同步策略**：``_emit`` / ``_fail`` / ``_load_module`` / ``_resolve_device`` 与
``model-flatten/scripts/validate_contract.py`` / ``measure_latency.py`` 同款实现（folder-agent
standalone 铁律要求不跨包 import），但**允许独立演化**——本脚本不是副本（与 measure_latency.py
副本的字节同步契约不同），新增 device 分支 / 加载策略时**只改本脚本**，无对齐义务。

校验不通过的 teacher 文件**禁止**进入下游（setup / train）。修法见 FAIL_REASON。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

# 深度/宽度轴比例（CONTRACTS 之外；teacher-gen 派生策略的字面常量）。
_DEPTH_FACTOR = 3
_WIDTH_FACTOR = 2


def _emit(key: str, value: Any) -> None:
    """stdout ``KEY: value`` 行（agent.md awk 解析）。value 非 str → JSON 串。"""
    if isinstance(value, str):
        print(f"{key}: {value}")
    else:
        print(f"{key}: {json.dumps(value, ensure_ascii=False)}")


def _fail(reason: str) -> int:
    """emit FAIL + reason，exit 2（fail loud）。"""
    _emit("VALIDATION", "FAIL")
    _emit("FAIL_REASON", reason)
    return 2


def _load_module(path: str, tag: str) -> Any:
    """按绝对路径加载契约模块（不入 sys.modules 持久化；sibling import 由其顶层自管）。

    与 ``validate_contract._load_module`` 同款：契约文件的 sibling import（如
    ``_demo_blocks`` / ``_model8_blocks``）需要其目录在 sys.path。
    """
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"{tag} 契约文件不存在：{p}")
    spec = importlib.util.spec_from_file_location(f"_teacher_{tag}_{p.stem}", str(p))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {tag} {p} 创建 module spec")
    mod = importlib.util.module_from_spec(spec)
    parent = str(p.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec.loader.exec_module(mod)
    return mod


def _resolve_device(device_arg: str) -> Any:
    """torch.device 解析（auto → cuda→npu→cpu，对齐 validate_contract._resolve_device）。"""
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


def _count_parameters(model: Any) -> int:
    """``sum(p.numel() for p in model.parameters())``（容量度量，整数）。"""
    return sum(p.numel() for p in model.parameters())


def validate_teacher(
    baseline_path: str,
    teacher_path: str,
    device_arg: str = "auto",
    seed: int = 0,
) -> dict[str, Any]:
    """纯函数校验：返回 {ok, reason, depth_axis, width_axis, ...}。

    任一 teacher 专属断言不符 → ok=False + reason（caller emit FAIL）。raise = caller 包成 FAIL。
    不重复 validate_contract 的格式校验（BUILD_FN / KNOBS schema / forward shape）——那是
    model-flatten ``validate_contract.py`` 的职责，本函数假定它已 PASS。
    """
    import torch

    try:
        baseline = _load_module(baseline_path, "baseline")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"加载 baseline 失败：{type(e).__name__}: {e}"}
    try:
        teacher = _load_module(teacher_path, "teacher")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"加载 teacher 失败：{type(e).__name__}: {e}"}

    # 1) DUMMY_INPUT 逐字一致（KD 硬约束）
    b_di = getattr(baseline, "DUMMY_INPUT", None)
    t_di = getattr(teacher, "DUMMY_INPUT", None)
    if not isinstance(b_di, dict) or not isinstance(t_di, dict):
        return {"ok": False, "reason": f"DUMMY_INPUT 须为 dict（baseline={type(b_di).__name__}, teacher={type(t_di).__name__}）"}
    if t_di != b_di:
        return {"ok": False, "reason": f"teacher.DUMMY_INPUT != baseline.DUMMY_INPUT（KD 要求逐字一致；teacher={t_di}, baseline={b_di}）"}

    # 2) 派生轴声明存在（字符串；空串允许但记 low-confidence，由 verifier 复核）
    depth_axis = getattr(teacher, "DEPTH_AXIS", None)
    width_axis = getattr(teacher, "WIDTH_AXIS", None)
    if not isinstance(depth_axis, str):
        return {"ok": False, "reason": f"teacher 缺 DEPTH_AXIS 字符串常量（可审计轴名；得到 {depth_axis!r}）"}
    if not isinstance(width_axis, str):
        return {"ok": False, "reason": f"teacher 缺 WIDTH_AXIS 字符串常量（可审计轴名；得到 {width_axis!r}）"}

    # 3-5) KNOBS 校验：轴 ×N + 其余不变
    b_knobs = getattr(baseline, "KNOBS", None)
    t_knobs = getattr(teacher, "KNOBS", None)
    if not isinstance(b_knobs, dict) or not b_knobs:
        return {"ok": False, "reason": "baseline.KNOBS 必须非空 dict（teacher-gen 假定 flatten 已校验）"}
    if not isinstance(t_knobs, dict) or not t_knobs:
        return {"ok": False, "reason": "teacher.KNOBS 必须非空 dict"}

    # 键集合一致（teacher 不许增删 knob——同 schema 派生）
    if set(b_knobs.keys()) != set(t_knobs.keys()):
        missing = set(b_knobs) - set(t_knobs)
        extra = set(t_knobs) - set(b_knobs)
        return {"ok": False, "reason": f"teacher.KNOBS 键集合 != baseline（missing={sorted(missing)}, extra={sorted(extra)}）"}

    # 轴名必须在 KNOBS 里（空串除外）
    if depth_axis and depth_axis not in b_knobs:
        return {"ok": False, "reason": f"DEPTH_AXIS={depth_axis!r} 不在 baseline.KNOBS（{sorted(b_knobs)}）"}
    if width_axis and width_axis not in b_knobs:
        return {"ok": False, "reason": f"WIDTH_AXIS={width_axis!r} 不在 baseline.KNOBS（{sorted(b_knobs)}）"}
    if depth_axis and depth_axis == width_axis:
        return {"ok": False, "reason": f"DEPTH_AXIS == WIDTH_AXIS == {depth_axis!r}（深度轴与宽度轴不能是同一个 knob）"}

    # 逐 knob 校验：轴 ×N / 其余逐字不变
    for name, b_kn in b_knobs.items():
        t_kn = t_knobs[name]
        if not isinstance(t_kn, dict):
            return {"ok": False, "reason": f"teacher.KNOBS[{name!r}] 不是 dict"}
        for field in ("default", "min", "step", "leverage"):
            if field not in t_kn:
                return {"ok": False, "reason": f"teacher.KNOBS[{name!r}] 缺字段 {field!r}"}

        b_default = b_kn["default"]
        t_default = t_kn["default"]
        if isinstance(b_default, bool) or not isinstance(b_default, (int, float)):
            return {"ok": False, "reason": f"baseline.KNOBS[{name!r}].default 须为数值（得到 {b_default!r}）"}
        if isinstance(t_default, bool) or not isinstance(t_default, (int, float)):
            return {"ok": False, "reason": f"teacher.KNOBS[{name!r}].default 须为数值（得到 {t_default!r}）"}

        if name == depth_axis:
            # 深度轴 ×3（向上取整容忍：teacher_default >= baseline_default * 3）
            floor = b_default * _DEPTH_FACTOR
            if t_default < floor:
                return {"ok": False, "reason": f"深度轴 {name!r}.default={t_default} < baseline.default×{_DEPTH_FACTOR}={floor}（不配当 teacher）"}
        elif name == width_axis:
            # 宽度轴 ×2
            floor = b_default * _WIDTH_FACTOR
            if t_default < floor:
                return {"ok": False, "reason": f"宽度轴 {name!r}.default={t_default} < baseline.default×{_WIDTH_FACTOR}={floor}（不配当 teacher）"}
        else:
            # 非轴 knob：default 逐字不变
            if t_default != b_default:
                return {"ok": False, "reason": f"非轴 knob {name!r}.default 被改（teacher={t_default} vs baseline={b_default}；纯调参派生只动深度/宽度轴）"}

        # min / step / leverage 始终逐字不变（轴只动 default）
        for field in ("min", "step", "leverage"):
            if t_kn[field] != b_kn[field]:
                return {"ok": False, "reason": f"KNOBS[{name!r}].{field} 被改（teacher={t_kn[field]!r} vs baseline={b_kn[field]!r}；teacher 只调 default，min/step/leverage 须继承 baseline）"}

    # 6) 容量上升（wrapper bug 防护）：teacher 默认实例参数 > baseline 默认实例参数
    try:
        device = _resolve_device(device_arg)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"device 解析失败（{device_arg!r}）：{type(e).__name__}: {e}"}

    torch.manual_seed(seed)
    b_defaults = {k: v["default"] for k, v in b_knobs.items()}
    t_defaults = {k: v["default"] for k, v in t_knobs.items()}

    b_build = getattr(baseline, "build_model", None)
    t_build = getattr(teacher, "build_model", None)
    if not callable(b_build):
        return {"ok": False, "reason": "baseline 缺 callable build_model"}
    if not callable(t_build):
        return {"ok": False, "reason": "teacher 缺 callable build_model"}

    try:
        b_model = b_build(**b_defaults).to(device).eval()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"baseline.build_model(**defaults) 实例化失败：{type(e).__name__}: {e}"}
    try:
        t_model = t_build(**t_defaults).to(device).eval()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"teacher.build_model(**defaults) 实例化失败（wrapper 是否正确委托？）：{type(e).__name__}: {e}"}

    b_params = _count_parameters(b_model)
    t_params = _count_parameters(t_model)
    if not (t_params > b_params):
        return {"ok": False, "reason": f"teacher 容量未上升（teacher={t_params} params <= baseline={b_params} params；深度×{_DEPTH_FACTOR}/宽度×{_WIDTH_FACTOR} 应严格放大）"}

    capacity_ratio = (t_params / b_params) if b_params > 0 else float("inf")

    return {
        "ok": True,
        "reason": "",
        "depth_axis": depth_axis,
        "width_axis": width_axis,
        "baseline_params": b_params,
        "teacher_params": t_params,
        "capacity_ratio": capacity_ratio,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="teacher-gen 派生忠实度硬校验（fail loud）")
    parser.add_argument("--baseline", required=True, help="baseline 契约 .py 绝对路径")
    parser.add_argument("--teacher", required=True, help="teacher 候选 .py 绝对路径")
    parser.add_argument("--device", default="auto", help="实例化 device（auto/cuda/npu/cpu，默认 auto）")
    parser.add_argument("--seed", type=int, default=0, help="实例化种子（默认 0）")
    args = parser.parse_args()

    try:
        result = validate_teacher(
            args.baseline, args.teacher, device_arg=args.device, seed=args.seed
        )
    except Exception as e:  # noqa: BLE001
        return _fail(f"校验异常：{type(e).__name__}: {e}")

    if not result["ok"]:
        return _fail(result["reason"])

    _emit("DEPTH_AXIS", result["depth_axis"])
    _emit("WIDTH_AXIS", result["width_axis"])
    _emit("BASELINE_PARAMS", result["baseline_params"])
    _emit("TEACHER_PARAMS", result["teacher_params"])
    _emit("CAPACITY_RATIO", f"{result['capacity_ratio']:.4f}")
    _emit("VALIDATION", "PASS")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
