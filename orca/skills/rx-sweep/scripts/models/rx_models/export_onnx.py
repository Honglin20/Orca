"""export_onnx.py —— rx_models 任意方案 → 单文件 ONNX（无 .data，昇腾 ATC 友好）。

用法（包已就位，cwd 任意）::

    # 单项维度参数
    python rx_models/export_onnx.py --model pure_cnn --num-symbols 32 --out pure_cnn.onnx
    # 或完整 RxConfig JSON（覆盖单项）
    python rx_models/export_onnx.py --model feat_complex \
        --cfg-json '{"num_symbols":32,"num_blocks":4}' --out feat_complex.onnx
    # 或 -m
    python -m rx_models.export_onnx --model model8_trf --num-symbols 32 --out model8_trf.onnx

昇腾 ATC 友好（降 .om 编译/推理时延的硬杠杆）：
  - ``static shape``（batch=1，固定 P/F/S），**不导 dynamic_axes**。
  - ``model.eval()`` → BN 推理形态（ATC 自行 conv-bn 融合）。
  - ``opset=13``（ATC 支持稳）。
  - ``do_constant_folding=True``。
  - ``save_as_external_data=False`` → 权重内联，**单文件无 .data**。

打印算子清单 —— 一眼看到 model8 的 MatMul/Softmax（要降的对象）、feat_fft 的 DFT
（Vector 算子）、pure_cnn 的纯 Conv（目标形态）。

依赖：``torch`` + ``onnx``（``pip install onnx``，用于 checker + 强制内联）。
"""
import os
import sys

# ---------------------------------------------------------------------------
# 自举：直接脚本运行（python rx_models/export_onnx.py）时，重定向为包模块，
# 让下面的相对 import（from .config / from . import）可解。
#  - __package__ 为空 = 直接脚本运行 → 自举
#  - __package__ == "rx_models" = -m 或包内 import → 跳过
# ---------------------------------------------------------------------------
if __package__ in (None, ""):
    _HERE = os.path.dirname(os.path.abspath(__file__))      # .../rx_models
    _PARENT = os.path.dirname(_HERE)                         # .../rx_sweep_models 或 .../models
    if _PARENT not in sys.path:
        sys.path.insert(0, _PARENT)
    import importlib
    _mod = importlib.import_module("rx_models.export_onnx")
    sys.exit(_mod.main())

import argparse  # noqa: E402

import torch  # noqa: E402

from .config import RxConfig  # noqa: E402
from . import get_model, list_models  # noqa: E402


def _fail(msg: str, code: int = 2) -> None:
    print(f"export_onnx: {msg}", file=sys.stderr)
    sys.exit(code)


def main() -> int:
    ap = argparse.ArgumentParser(description="rx_models → 单文件 ONNX（昇腾友好）")
    ap.add_argument("--model", required=True,
                    help=f"方案名，可用：{list_models()}")
    ap.add_argument("--num-ports", type=int, default=4)
    ap.add_argument("--num-subcarriers", type=int, default=48)
    ap.add_argument("--num-symbols", type=int, default=32,
                    help="按工程改（beam_num=32 等）")
    ap.add_argument("--num-blocks", type=int, default=4)
    ap.add_argument("--embed-dim", type=int, default=16,
                    help="须 ÷16（昇腾 Cube 对齐）")
    ap.add_argument("--cfg-json", default="",
                    help="完整 RxConfig JSON；非空则覆盖上面单项参数")
    ap.add_argument("--out", required=True, help="输出 .onnx 路径")
    ap.add_argument("--opset", type=int, default=13)
    args = ap.parse_args()

    if args.cfg_json.strip():
        cfg = RxConfig.from_json(args.cfg_json)
    else:
        cfg = RxConfig(
            num_ports=args.num_ports,
            num_subcarriers=args.num_subcarriers,
            num_symbols=args.num_symbols,
            num_blocks=args.num_blocks,
            embed_dim=args.embed_dim,
        )

    available = list_models()
    if args.model not in available:
        _fail(f"未知 model {args.model!r}，可用 {available}")

    try:
        import onnx
    except ImportError:
        _fail("缺 onnx 包（强制单文件 + 校验需要）：pip install onnx")

    model = get_model(args.model, cfg).eval()
    # static dummy [1, P, F, S, 1]（CPU；导出不需 GPU）
    dummy = torch.zeros(*cfg.io_shape, dtype=torch.float32)

    print(
        f"[export_onnx] model={args.model} "
        f"P/F/S={cfg.num_ports}/{cfg.num_subcarriers}/{cfg.num_symbols} "
        f"blocks={cfg.num_blocks} embed={cfg.embed_dim} opset={args.opset}"
    )

    # dynamo=False → 传统 trace-based 路径（不依赖 onnxscript，opset 支持广、
    # 算子映射稳，昇腾 ATC 友好）。torch 2.6+ 默认 dynamo=True 会强依赖 onnxscript。
    # 旧 torch (<2.6) 无 dynamo 参数 → 退回零参（同样传统路径）。
    export_kwargs = dict(
        opset_version=args.opset,
        input_names=["input"], output_names=["output"],
        do_constant_folding=True,
    )
    try:
        try:
            torch.onnx.export(model, (dummy,), args.out, dynamo=False, **export_kwargs)
        except TypeError:
            torch.onnx.export(model, (dummy,), args.out, **export_kwargs)
    except Exception as e:  # noqa: BLE001
        _fail(f"torch.onnx.export 失败：{e!r}")

    # 强制单文件（权重内联）+ 结构校验
    onnx_model = onnx.load(args.out)
    onnx.checker.check_model(onnx_model)
    onnx.save_model(onnx_model, args.out, save_as_external_data=False)
    data_file = args.out + ".data"
    had_data = os.path.exists(data_file)
    if had_data:
        os.remove(data_file)

    ops = sorted({n.op_type for n in onnx_model.graph.node})
    in_shape = [d.dim_value for d in onnx_model.graph.input[0].type.tensor_type.shape.dim]
    out_shape = [d.dim_value for d in onnx_model.graph.output[0].type.tensor_type.shape.dim]
    size_kb = os.path.getsize(args.out) / 1024.0

    print(f"[export_onnx] OK out={args.out}")
    print(f"[export_onnx] size={size_kb:.1f} KB single_file={not had_data} (no .data)")
    print(f"[export_onnx] input_shape={in_shape} output_shape={out_shape}")
    print(f"[export_onnx] ops({len(ops)})={ops}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
