"""run_diagnostic.py —— 真实 bitx diagnostic pipeline driver（diagnostic_saver agent 执行）。

替代直接调 ``bitx.api.diagnostic_api.run_diagnostic_pipeline``：bitx 1.1.1.dev395 含
``DistOverlayData.to_chart_data`` bug（用 ``int(self.fp32[i])`` 但 HistogramObserver 某些
stages 把 fp32_hist 序列化成 ``tensor([...])`` 字符串 → ``int('t')`` raise）。本 driver
在调 ``run_diagnostic_pipeline`` 之前在**本进程内** monkey-patch ``to_chart_data``，
让 diagnostic_pipeline 跑通写 ``<output_dir>/diagnostic/`` 全套 JSON。

用法::

    python tests/e2e_mxint/tools/run_diagnostic.py <output_dir>

退出码：0 = 成功；非 0 = bitx raise（fail loud）。stdout 末行 ``DIAGNOSTIC_DIR=<path>``。
"""
from __future__ import annotations

import sys
from pathlib import Path


def _patch_dist_overlay_to_chart_data() -> None:
    """Patch bitx DistOverlayData.to_chart_data to tolerate tensor-repr strings.

    bitx 1.1.1.dev395 HistogramObserver 在某些 stages 把 fp32_hist 写成
    ``"tensor([3., 1., ...])"`` 字符串（json.dump tensor → str）。原生
    ``int(self.fp32[i])`` 取字符串首字符 't' → ``int('t')`` raise ValueError。

    Patch 策略：number → round 4 位；非 number（str 等） → 0（异常信号位）。
    Patch 限本进程内，不污染 bitx 全局行为（其它用户不受影响）。
    """
    try:
        from bitx.api.diagnostic_api import DistOverlayData
    except ImportError:
        return  # bitx 旧版无 DistOverlayData → 无需 patch

    def _coerce(v):
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (int, float)):
            return round(float(v), 4)
        return 0  # tensor-repr string 等异常 → 0（保留 chart 结构，数值缺失位）

    def _patched(self: DistOverlayData) -> list[dict]:
        return [
            {"bin": round(float(b), 4),
             "fp32": _coerce(self.fp32[i]),
             "quant": _coerce(self.quant[i]),
             "error": _coerce(self.error[i])}
            for i, b in enumerate(self.bins)
            if i < len(self.fp32)
        ]

    DistOverlayData.to_chart_data = _patched  # type: ignore[assignment]


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: run_diagnostic.py <output_dir>", file=sys.stderr)
        return 2

    output_dir = str(Path(sys.argv[1]).resolve())
    results_json = Path(output_dir) / "results.json"
    if not results_json.is_file():
        print(f"FATAL: {results_json} not found (run_analysis 未跑或失败)", file=sys.stderr)
        return 2

    try:
        from bitx.api.diagnostic_api import run_diagnostic_pipeline
    except ImportError as e:
        print(f"FATAL: bitx import failed: {e}", file=sys.stderr)
        return 2

    # patch 必须在 import 后、调 run_diagnostic_pipeline 前应用
    _patch_dist_overlay_to_chart_data()

    print(f"[run_diagnostic] running diagnostic_pipeline on {output_dir}...")
    diag_dir = run_diagnostic_pipeline(output_dir)
    print(f"[run_diagnostic] diagnostic data → {diag_dir}")
    print(f"DIAGNOSTIC_DIR={diag_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
