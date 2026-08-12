"""data/gen.py —— 生成 tiny 合成 OFDM .pt（演示数据管线占位）。

跑：``python data/gen.py``（在 fixture 目录下）
输出：``data/ofdm_tiny.pt``，含 ``{"x": [...], "y": [...]}``，shape ``[8, 4, 48, 64, 1]``。

train_rx.py 可通过 ``--data data/ofdm_tiny.pt`` 加载；不加则用内联 ``torch.randn`` 合成
（确定性相同——两者都靠 ``manual_seed(0)``）。
"""

from __future__ import annotations

from pathlib import Path

import torch


def main():
    torch.manual_seed(0)
    # 合成 OFDM-like 资源栅格：x = TX, y = h·x + n（toy 信道模型，非真实链路）
    x = torch.randn(8, 4, 48, 64, 1)             # TX
    h = torch.randn(8, 4, 48, 64, 1) * 0.1       # 多径信道（小幅度）
    n = torch.randn(8, 4, 48, 64, 1) * 0.01      # AWGN
    y = h * x + n                                # RX

    out = Path(__file__).resolve().parent / "ofdm_tiny.pt"
    torch.save({"x": x, "y": y}, out)
    print(f"[gen] wrote {out} x={tuple(x.shape)} y={tuple(y.shape)}", flush=True)


if __name__ == "__main__":
    main()
