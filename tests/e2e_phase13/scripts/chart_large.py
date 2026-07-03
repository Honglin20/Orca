"""chart_large.py —— E2E-3 大数据（100k 行）降采样 + E2E-4 超限（500k + max_points=200000）。

E2E-3：100k 行 fixture（每行 ~25 字节编码），调 render_chart 默认 max_points=2000 →
        client 端降采样到 ≤2000 行 + 整条消息 < 2MB → 推到 ingestor 落 tape。
E2E-4：500k 行 + max_points=200000 → client 降采样后 200k 行（远超 2MB）→ client raise
        ValueError，script 非零退出，tape 无对应事件。

行数 + max_points 由 argv 控制（默认 E2E-3 模式）。
"""

from __future__ import annotations

import json
import sys

from orca.chart import render_chart


def _build_rows(n: int) -> list[dict]:
    """n 行 fixture：每行 ~25 字节 JSON 编码（{"x": i, "y": float}）。"""
    return [{"x": i, "y": float(i) * 0.001} for i in range(n)]


def main() -> int:
    # argv[1] = 行数；argv[2] = max_points（可选，默认 2000）。
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
    max_points = int(sys.argv[2]) if len(sys.argv) > 2 else 2000

    rows = _build_rows(n)
    try:
        seq = render_chart(
            chart_type="line",
            data=rows,
            label="big_data",
            title=f"rows-{n}",
            x="x",
            y="y",
            max_points=max_points,
        )
    except ValueError as e:
        # E2E-4 期望路径：client 端大小检查 raise，stdout 显式标注 + 非零退出。
        print(f"[chart_large] REJECTED rows={n} max_points={max_points}: {e}", flush=True)
        return 2
    print(f"[chart_large] OK rows={n} max_points={max_points} seq={seq}", flush=True)
    # 透传降采样后的实际行数（test 解析 tape 验证），通过 stderr。
    sys.stderr.write(f"[chart_large] sent rows={n} max_points={max_points}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
