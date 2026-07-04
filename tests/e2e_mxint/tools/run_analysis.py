"""run_analysis.py —— 真实 bitx 量化误差分析 driver（runner agent 执行）。

替代直接调 ``python -m bitx.api.mxint_error_analysis``：bitx CLI 只渲染图表 + print，
**不写 results.json**。但下游 ``diagnostic_saver`` 调的
``bitx.api.diagnostic_api.run_diagnostic_pipeline`` 要求 ``<output_dir>/results.json``
存在。所以本 driver：

  1. import bitx，load adapter
  2. build MXInt8 config（w=8, a=8, block=16）+ attach 5 observers
  3. ``Session.run()`` 跑量化 + 分析
  4. ``StudyReport({"quant": [result]}).save(output_dir)`` 写 results.json + figures
  5. stdout 末行 ``OUTPUT_DIR=<path>``（runner agent grep 它）

用法::

    python tests/e2e_mxint/tools/run_analysis.py \\
        --adapter tests/e2e_mxint/target_project/_adapter.py \\
        --device cpu \\
        --output-dir tests/e2e_mxint/output/run_<timestamp>

退出码：0 = 成功；非 0 = bitx raise（fail loud，runner agent 看 stderr）。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--adapter", required=True, help="adapter .py 路径（get_model/get_eval_fn/get_data）")
    p.add_argument("--device", default="cpu", help="cpu / cuda / mps")
    p.add_argument("--output-dir", required=True, help="bitx StudyReport 落盘目录")
    p.add_argument("--w-bits", type=int, default=8)
    p.add_argument("--a-bits", type=int, default=8)
    p.add_argument("--block-size", type=int, default=16)
    p.add_argument("--skip-recovery", action="store_true", help="跳过 precision recovery ablation")
    args = p.parse_args()

    # 延迟 import：让 --help 不需要 bitx 可用（fail-loud on real invocation）。
    try:
        import torch  # noqa: F401
        from bitx.api.mxint_error_analysis import (
            build_mxint_config, load_adapter,
        )
        from bitx.session import Session
        from bitx.analysis.observers import (
            QSNRObserver, MSEObserver,
            DistributionObserver, HistogramObserver, PerBlockQSNRObserver,
        )
        from bitx.report._study_report import StudyReport
    except ImportError as e:
        print(f"[run_analysis] FATAL: bitx/torch import failed: {e}", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    print(f"[run_analysis] adapter={args.adapter}")
    print(f"[run_analysis] device={args.device}  output={output_dir}")
    print(f"[run_analysis] config: MXInt{args.w_bits} (w={args.w_bits}, a={args.a_bits}, block={args.block_size})")

    # ── Load adapter + build model/eval_fn/data ─────────────────────
    adapter = load_adapter(args.adapter)
    model = adapter.get_model().to(args.device).eval()
    eval_fn = adapter.get_eval_fn()
    calib_data, eval_data = adapter.get_data()

    n_params = sum(p.numel() for p in model.parameters())
    print(f"[run_analysis] model={type(model).__name__}  params={n_params:,}")
    print(f"[run_analysis] calib_batches={len(calib_data)}  eval_loader={type(eval_data).__name__}")

    # ── Build MXInt8 config ─────────────────────────────────────────
    config = build_mxint_config(
        w_bits=args.w_bits, a_bits=args.a_bits, block_size=args.block_size,
    )

    # ── Run quantization + analysis ────────────────────────────────
    print("[run_analysis] running quantization + analysis (this is the slow part)...")
    session = Session(
        model, config,
        observers=[
            QSNRObserver(), MSEObserver(),
            DistributionObserver(), HistogramObserver(), PerBlockQSNRObserver(),
        ],
        keep_fp32=True,
    )
    result = session.run(
        calib_data,
        eval_data=eval_data,
        eval_fn=eval_fn,
    )

    # ── Save StudyReport → output_dir/results.json ──────────────────
    # diagnostic_saver 调 run_diagnostic_pipeline(output_dir) 时会读 results.json。
    print(f"[run_analysis] saving StudyReport → {output_dir}")
    study = StudyReport({"quant": [result]})
    study.save(str(output_dir))

    # ── Report metrics ──────────────────────────────────────────────
    fp32_acc = (result.fp32_metrics or {}).get("accuracy")
    quant_acc = (result.quant_metrics or {}).get("accuracy")
    delta = (result.delta or {}).get("accuracy")
    qsnr_per_layer = result.qsnr_per_layer or {}
    if qsnr_per_layer:
        worst_layer, worst_qsnr = min(
            qsnr_per_layer.items(), key=lambda kv: kv[1]
        )
    else:
        worst_layer, worst_qsnr = "unknown", 0.0

    elapsed = time.time() - t0
    print(f"[run_analysis] elapsed: {elapsed:.1f}s")
    print(f"[run_analysis] fp32_acc={fp32_acc}  quant_acc={quant_acc}  delta={delta}")
    print(f"[run_analysis] worst_layer={worst_layer}  worst_qsnr_db={worst_qsnr:.2f}")
    print(f"[run_analysis] results.json → {output_dir / 'results.json'}")
    # runner agent grep 这一行回传 output_dir（diagnostic_saver 下游用）
    print(f"OUTPUT_DIR={output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
