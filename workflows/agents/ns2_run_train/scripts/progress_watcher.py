#!/usr/bin/env python3
"""progress_watcher.py —— tail 训练 progress.jsonl，边训练边推多指标实时曲线到前端。

训练 detach 后本脚本在训练进程组内伴生运行（由 ``launch.sh`` 在 setsid wrapper 里、训练脚本
**之前**后台启动）。解析生成契约的结构化进度行（逐字，见生成契约 §3(b)）：

    {"step": <int>, "metrics": {"<name>": <float>, ...}}

每出现新进度点，把**每指标**的累计点经 ``orca.chart.render_chart`` 推一条 line 图
（title 带真实指标名，同 label+title 重复推送 → 前端替换 = 实时更新语义，
phase-9d §2.7 dedup）。

**指标名/种类不可预测**（用户训练/评估代码有什么就推什么）：消费端**零硬编码 metric 名**——
遍历 ``metrics`` dict，每个数值项**各推一张独立图**（loss / test_acc / 任意自定义指标
各一张，不再混在一张多线图里）。``loss`` 非特例：用户没 loss 就不出现 loss。

fail-soft 铁律（本脚本**绝不**影响训练 rc / 训练 log / 训练进程）：
- orca.chart 不可用 / 缺 ORCA_* env / socket 不可达 → stderr 一次 + exit 0（断更不轰炸）；
- progress 文件缺失（训练首次写稍晚创建）→ 轮询等待，直到出现或 ``--max-wait`` 超时；
- 训练进程退出（``--done-marker`` 的 mtime 晚于本脚本启动 = 本次 attempt 结束）→ 最后一次推图后 exit 0；
- 已推过点后 progress 超过 ``--max-idle`` 秒无增长（异常停滞兜底）→ exit 0；
- **半行缓冲**：读到的不以 ``\n`` 结尾（flush 未完）→ 留 tail 下次 poll 拼接，不丢点不崩。

退出时机与 self-heal 兼容：self-heal 整组 ``kill -- -PID`` 时本脚本随进程组一并被杀，无需自清理；
正常完成由 done-marker 驱动退出（不依赖 idle 空等）。

用法（launch.sh 内、训练脚本前启动）：
    python3 progress_watcher.py --progress "runs/train/progress.jsonl" \
        --done-marker "runs/train/.train_rc" \
        --label "nas-supernet/train" --title "Training Metrics (attempt 1)" \
        [--poll 5] [--max-idle 120] [--max-wait 120]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# render_chart 强依赖的 4 个身份键（与 ``orca.chart._render._REQUIRED_ENV`` 同集）。
_REQUIRED_ENV = ("ORCA_RUN_ID", "ORCA_NODE", "ORCA_SESSION_ID", "ORCA_CHART_SOCK")


def _is_number(v: Any) -> bool:
    """True 仅对真实数值（排除 bool——``isinstance(True, int)`` 为真，但 bool 非 metric）。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _drain(
    progress_path: Path,
    offset: int,
    tail: str,
    series: dict[str, list[tuple[float, float]]],
) -> tuple[int, str, bool]:
    """Incremental read of new bytes from progress.jsonl + half-line buffer parse.

    Appends parsed metric points into ``series`` (mutated in place). Returns
    ``(new_offset, new_tail, new_point)`` where ``new_point=True`` if any metric
    point was added. If the file is missing or has no new bytes beyond ``offset``,
    returns the inputs unchanged + ``False``.

    Shared by the done-marker exit path (drains final ~5s of points before the
    last push) and the main poll loop (DRY: zero behavioral drift between paths).
    """
    try:
        size = progress_path.stat().st_size
    except OSError:
        return offset, tail, False
    if size <= offset:
        return offset, tail, False

    with progress_path.open("rb") as f:
        f.seek(offset)
        chunk_bytes = f.read(size - offset)
    new_offset = size
    chunk = chunk_bytes.decode("utf-8", errors="replace")

    # Half-line buffer: join previous residue, split on \n; trailing partial stays as tail.
    buf = tail + chunk
    parts = buf.split("\n")
    if buf.endswith("\n"):
        new_tail = ""
    else:
        new_tail = parts.pop()  # last segment incomplete, keep for next call

    new_point = False
    for line in parts:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue  # malformed JSON (half-line residue / corruption) skip, don't crash
        if not isinstance(row, dict):
            continue
        step = row.get("step")
        metrics = row.get("metrics")
        if not _is_number(step) or not isinstance(metrics, dict):
            continue  # non-contract line (missing step / metrics not dict) skip
        x = float(step)
        for name, val in metrics.items():
            if not _is_number(val):
                continue  # non-numeric metric (string / null / nested) skip
            series.setdefault(str(name), []).append((x, float(val)))
            new_point = True

    return new_offset, new_tail, new_point


def main() -> int:
    ap = argparse.ArgumentParser(description="Tail progress JSONL and push a live multi-metric line chart.")
    ap.add_argument("--progress", required=True,
                    help="progress.jsonl path (relative to $ORCA_ARTIFACTS_DIR or absolute)")
    ap.add_argument("--done-marker", required=True,
                    help="attempt completion marker (launch.sh wrapper writes .train_rc / .retrain_rc)")
    ap.add_argument("--label", required=True, help="chart group key (dedup dimension 1)")
    ap.add_argument("--title", required=True, help="chart title (dedup dimension 2, unique per label)")
    ap.add_argument("--poll", type=float, default=5.0, help="poll interval seconds")
    ap.add_argument("--max-idle", type=float, default=120.0,
                    help="exit fallback seconds after no progress growth (post-first-point)")
    ap.add_argument("--max-wait", type=float, default=120.0,
                    help="timeout seconds waiting for progress file to appear")
    args = ap.parse_args()

    # 1. Determine mode: live (env + render_chart available) or static fallback.
    missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    render_chart = None
    if not missing:
        try:
            from orca.chart import render_chart as _rc  # noqa: PLC0415
            render_chart = _rc
        except Exception:  # noqa: BLE001 -- fail-soft
            pass

    live_mode = render_chart is not None
    if not live_mode:
        sys.stderr.write(
            "[progress_watcher] live push unavailable"
            f" ({'missing env: ' + ', '.join(missing) if missing else 'orca.chart import failed'})"
            " — using static HTML fallback\n"
        )

    progress_path = Path(args.progress)
    done_marker = Path(args.done_marker)
    start_mtime = done_marker.stat().st_mtime if done_marker.is_file() else 0.0

    series: dict[str, list[tuple[float, float]]] = {}
    last_growth = 0.0
    offset = 0
    tail = ""
    wait_started = time.monotonic()

    while True:
        # 2a. done-marker exit (mtime later than script start = attempt finished).
        try:
            if done_marker.stat().st_mtime > start_mtime:
                offset, tail, _ = _drain(progress_path, offset, tail, series)
                if live_mode:
                    _push(args, render_chart, series)
                else:
                    _render_static_charts(args, series)
                return 0
        except OSError:
            pass

        # 2b/2c. incremental read.
        offset, tail, new_point = _drain(progress_path, offset, tail, series)

        if not progress_path.is_file():
            if time.monotonic() - wait_started > args.max_wait:
                if not live_mode and series:
                    _render_static_charts(args, series)
                return 0
            time.sleep(args.poll)
            continue

        if new_point:
            last_growth = time.monotonic()
            if live_mode:
                if not _push(args, render_chart, series):
                    # Live socket broke mid-run — switch to static for final render.
                    live_mode = False

        # 2d. idle fallback (only after first point).
        if last_growth and time.monotonic() - last_growth > args.max_idle:
            if not live_mode and series:
                _render_static_charts(args, series)
            return 0

        time.sleep(args.poll)


def _push(args: argparse.Namespace, render_chart: object,
          series: dict[str, list[tuple[float, float]]]) -> bool:
    """Push each metric as an independent live line chart. Returns True on success,
    False on failure (caller switches to static fallback)."""
    if not series:
        return True
    for name, pts in series.items():
        data = [{"x": x, "y": y} for x, y in pts]
        try:
            render_chart(  # type: ignore[misc]
                chart_type="line",
                data=data,
                label=args.label,
                title=f"{args.title}: {name}",
                x="x",
                y="y",
                x_label="step",
                y_label=name,
            )
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(
                f"[progress_watcher] render_chart failed: {exc} — switching to static fallback\n"
            )
            return False
    return True


def _render_static_charts(
    args: argparse.Namespace, series: dict[str, list[tuple[float, float]]]
) -> None:
    """Write each metric as a static HTML line chart when live push is unavailable.

    Fail-soft: any rendering failure writes stderr + returns (never crashes training).
    Output: ``$ORCA_ARTIFACTS_DIR/charts/progress_<safe_label>_<metric>.html`` (plotly)
    or ``.png`` (matplotlib fallback).
    """
    if not series:
        return

    ad = Path(os.environ.get("ORCA_ARTIFACTS_DIR", "."))
    charts_dir = ad / "charts"
    try:
        charts_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        sys.stderr.write(f"[progress_watcher] cannot create charts dir {charts_dir}\n")
        return

    safe_label = args.label.replace("/", "_").replace("\\", "_")

    for name, pts in series.items():
        safe_name = name.replace("/", "_").replace("\\", "_").replace(" ", "_")
        data = [{"x": x, "y": y} for x, y in pts]
        title = f"{args.title}: {name}"
        out_stem = charts_dir / f"progress_{safe_label}_{safe_name}"

        # Try plotly first (self-contained HTML).
        try:
            import plotly.graph_objects as go  # noqa: PLC0415

            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=[d["x"] for d in data],
                y=[d["y"] for d in data],
                mode="lines+markers",
                name=name,
            ))
            fig.update_layout(
                title=title,
                xaxis_title="step",
                yaxis_title=name,
                font=dict(size=12),
            )
            out = out_stem.with_suffix(".html")
            fig.write_html(str(out), include_plotlyjs=True, full_html=True, auto_open=False)
            sys.stderr.write(f"[progress_watcher] static chart: {out}\n")
            continue
        except ImportError:
            pass  # plotly not installed → matplotlib fallback
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[progress_watcher] plotly failed for {name}: {exc}\n")

        # Matplotlib PNG fallback.
        try:
            import matplotlib  # noqa: PLC0415

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt  # noqa: PLC0415

            fig, ax = plt.subplots(figsize=(10, 5))
            ax.plot([d["x"] for d in data], [d["y"] for d in data], "-o", linewidth=1.5, markersize=4)
            ax.set_title(title)
            ax.set_xlabel("step")
            ax.set_ylabel(name)
            fig.tight_layout()
            out = out_stem.with_suffix(".png")
            fig.savefig(str(out), dpi=120, bbox_inches="tight")
            plt.close(fig)
            sys.stderr.write(f"[progress_watcher] static chart: {out}\n")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[progress_watcher] static render failed for {name}: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
