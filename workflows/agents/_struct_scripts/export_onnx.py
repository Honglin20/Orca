"""export_onnx.py —— 把用户的 build_fn 实例化模型导出为 ONNX（§4 / §10）。

契约（草稿 §4 评测环 / §10 inputs）：每轮 candidate 由 Engineer 写入 model.py，
Evaluator / family_detect / finalize 用本脚本统一导出 ONNX，再交 cost_model.measure() 取时延。

入参：
    --model_path   : 用户 model.py 绝对路径（被 import；其所在目录入 sys.path）
    --build_fn     : model.py 内的实例化函数名（如 build_model），零参 → 返回 nn.Module
    --dummy_input  : JSON 字符串或文件路径，形如 {"shape":[1,3,224,224],"dtype":"float32"}
    --opset        : ONNX opset 版本（默认 17）
    --out          : 输出 .onnx 绝对路径（缺失则派生为 model_path 同名 .onnx）
    --device       : 导出设备（默认 auto：cuda→npu→cpu；导出确定性与实测 device 解耦）
    --no-external-data / --allow-external-data :
                     外部 .data 伴生文件策略（P7 新增）。默认 --no-external-data：导出后
                     断言无 `<out>.data` 伴生（model 权重 inline 进 protobuf）。
                     --allow-external-data 显式允许伴生（超大模型 >2GB 时必开）。
    --seed         : 随机种子（dummy 输入用；默认 0，保复现）

出参（stdout，结构化 key=value）：
    ONNX: <绝对路径>
    OPSET: <int>
    STATUS: ok

fail loud（草稿 §4 "exotic 结构导不出 → 记 FAIL_export"）：
    任何异常（import 失败 / build_fn 报错 / torch.onnx.export 失败 /
    --no-external-data 模式下意外产出 .data）→ 非零退出 + stderr 完整 traceback。
    本脚本不写 ledger，由 curator reducer 入账。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any


# JSON dtype 名 → torch dtype（dummy_input.dtype 解析用）。
def _torch_dtype(name: str):
    import torch

    table = {
        "float32": torch.float32,
        "float": torch.float32,
        "float64": torch.float64,
        "double": torch.float64,
        "float16": torch.float16,
        "half": torch.float16,
        "bfloat16": torch.bfloat16,
        "int32": torch.int32,
        "int": torch.int32,
        "int64": torch.int64,
        "long": torch.int64,
        "int16": torch.int16,
        "short": torch.int16,
        "int8": torch.int8,
        "uint8": torch.uint8,
        "bool": torch.bool,
    }
    key = name.strip().lower()
    if key not in table:
        raise ValueError(
            f"不支持的 dtype: {name!r}；支持: {sorted(table)}"
        )
    return table[key]


def _load_dummy_input(spec_raw: str) -> dict[str, Any]:
    """解析 dummy_input：接受 JSON 字符串 或 含 JSON 的文件路径。fail loud。"""
    import torch

    if not spec_raw or not spec_raw.strip():
        raise ValueError("dummy_input 为空")

    # 文件优先（路径存在）：读文件内容。
    if os.path.isfile(spec_raw):
        text = Path(spec_raw).read_text(encoding="utf-8")
    else:
        text = spec_raw
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"dummy_input 不是合法 JSON：{e}\n原文：{text!r}"
        ) from e

    if not isinstance(data, dict):
        raise ValueError(f"dummy_input 必须是 JSON object（得到 {type(data).__name__}）")

    # 形态一（推荐 / 草稿 §10）：{"shape":[...],"dtype":"float32"} → 单输入 tuple。
    # 形态二（多输入）：{"inputs":[{"name":..,"shape":..,"dtype":..}, ...]}。
    if "shape" in data:
        shape = data.get("shape")
        dtype_name = data.get("dtype", "float32")
        if not isinstance(shape, list) or not shape:
            raise ValueError(f"dummy_input.shape 必须非空 list（得到 {shape!r}）")
        return {
            "kind": "tuple",
            "tensors": [
                {"name": "input", "shape": list(shape), "dtype": dtype_name}
            ],
        }
    if "inputs" in data:
        inputs = data["inputs"]
        if not isinstance(inputs, list) or not inputs:
            raise ValueError("dummy_input.inputs 必须非空 list")
        tensors = []
        for i, item in enumerate(inputs):
            if not isinstance(item, dict) or "shape" not in item:
                raise ValueError(f"dummy_input.inputs[{i}] 缺 shape")
            tensors.append(
                {
                    "name": item.get("name", f"input_{i}"),
                    "shape": list(item["shape"]),
                    "dtype": item.get("dtype", "float32"),
                }
            )
        return {"kind": "dict", "tensors": tensors}

    raise ValueError(
        "dummy_input 必须含 shape（单输入）或 inputs（多输入），得到 keys="
        f"{sorted(data)}"
    )


def _materialize_dummy(spec: dict[str, Any], device: str):
    """把解析后的 spec 物化为 torch dummy 输入（tuple 或 dict）。"""
    import torch

    tensors = []
    for t in spec["tensors"]:
        dtype = _torch_dtype(t["dtype"])
        shape = list(t["shape"])
        tensors.append(
            (t["name"], torch.randn(*shape, dtype=dtype, device=device))
        )
    if spec["kind"] == "tuple":
        return tensors[0][1]
    return {name: tensor for name, tensor in tensors}


def export_onnx(
    model_path: str,
    build_fn: str,
    dummy_input: str,
    opset: int = 17,
    out: str | None = None,
    device: str = "auto",
    no_external_data: bool = True,
    seed: int = 0,
) -> str:
    """导出 ONNX，返回绝对路径。任何失败 raise（caller fail loud）。"""
    import torch

    torch.manual_seed(seed)

    model_path = os.path.abspath(model_path)
    if not os.path.isfile(model_path):
        raise FileNotFoundError(f"model_path 不存在: {model_path}")

    # 把 model.py 所在目录加到 sys.path 头，import 它。
    model_dir = os.path.dirname(model_path)
    module_name = Path(model_path).stem
    if model_dir not in sys.path:
        sys.path.insert(0, model_dir)
    # 用 importlib 显式从文件加载（防同名 module 干扰）。
    spec = importlib.util.spec_from_file_location(module_name, model_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {model_path} 构造 import spec")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)

    if not hasattr(mod, build_fn):
        raise AttributeError(
            f"{module_name}（{model_path}）无属性 {build_fn!r}；可用: "
            f"{[n for n in dir(mod) if not n.startswith('_')]}"
        )
    factory = getattr(mod, build_fn)
    if not callable(factory):
        raise TypeError(f"{module_name}.{build_fn} 不可调用（{type(factory)}）")

    # 实例化模型 + 物化 dummy 输入。
    model = factory()
    if not isinstance(model, torch.nn.Module):
        raise TypeError(
            f"{build_fn}() 返回 {type(model).__name__}，期望 torch.nn.Module"
        )
    # device 解析：auto → cuda→npu→cpu（NAS resolve_device）；显式串原样用。
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from _device import resolve_device  # type: ignore

    torch_device = resolve_device(device)
    model = model.to(torch_device).eval()
    dummy_spec = _load_dummy_input(dummy_input)
    dummy = _materialize_dummy(dummy_spec, device=str(torch_device))

    # 输出路径。
    if out is None or not out:
        out = str(Path(model_path).with_suffix(".onnx"))
    out = os.path.abspath(out)
    Path(out).parent.mkdir(parents=True, exist_ok=True)

    # 导出。input_names 来自 dummy spec；dynamic_axes 留空（cost model 用静态 shape）。
    input_names = [t["name"] for t in dummy_spec["tensors"]]
    if isinstance(dummy, dict):
        torch.onnx.export(
            model,
            (dummy,),
            out,
            input_names=input_names,
            opset_version=opset,
            dynamo=False,
        )
    else:
        torch.onnx.export(
            model,
            dummy,
            out,
            input_names=input_names,
            opset_version=opset,
            dynamo=False,
        )

    # --no-external-data 守门（P7）：默认断言无 .data 伴生（model 权重 inline）。
    if no_external_data:
        data_companion = out + ".data"
        if os.path.isfile(data_companion):
            raise RuntimeError(
                f"导出产生 .data 伴生文件 ({data_companion})，但 --no-external-data 模式禁止。"
                " 模型权重可能 >2GB protobuf 上限；如需外部数据，显式传 --allow-external-data。"
            )
    return out


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="实例化 build_fn → 导出 ONNX（草稿 §4/§10 契约）。fail loud。"
    )
    parser.add_argument("--model_path", required=True, help="用户 model.py 绝对路径")
    parser.add_argument(
        "--build_fn", required=True, help="model.py 内实例化函数名（零参 → nn.Module）"
    )
    parser.add_argument(
        "--dummy_input",
        required=True,
        help='JSON 字符串或文件路径：{"shape":[1,3,224,224],"dtype":"float32"}',
    )
    parser.add_argument("--opset", type=int, default=17, help="ONNX opset（默认 17）")
    parser.add_argument(
        "--out", default="", help="输出 .onnx 路径（空 → model_path 同名 .onnx）"
    )
    parser.add_argument(
        "--device",
        default="auto",
        choices=["auto", "cuda", "npu", "cpu"],
        help="导出设备（默认 auto：cuda→npu→cpu 探测；与实测 device 解耦）",
    )
    # 外部 .data 伴生文件策略（P7）：默认无 .data；--allow-external-data 显式开禁。
    parser.add_argument(
        "--no-external-data",
        dest="no_external_data",
        action="store_true",
        default=True,
        help="(默认 True) 导出后断言无 .data 伴生文件；违例 fail loud",
    )
    parser.add_argument(
        "--allow-external-data",
        dest="no_external_data",
        action="store_false",
        help="显式允许 .data 伴生文件（超大模型 >2GB 时必开）",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="dummy 输入随机种子（默认 0，保复现）",
    )
    args = parser.parse_args()

    try:
        onnx_path = export_onnx(
            model_path=args.model_path,
            build_fn=args.build_fn,
            dummy_input=args.dummy_input,
            opset=args.opset,
            out=args.out or None,
            device=args.device,
            no_external_data=args.no_external_data,
            seed=args.seed,
        )
    except Exception as e:
        print(f"[export_onnx] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2  # 非零：FAIL_export，整轮停（草稿 §4）。
    print(f"STATUS: ok")
    print(f"ONNX: {onnx_path}")
    print(f"OPSET: {args.opset}")
    print(f"DEVICE: {args.device}")
    print(f"NO_EXTERNAL_DATA: {args.no_external_data}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
