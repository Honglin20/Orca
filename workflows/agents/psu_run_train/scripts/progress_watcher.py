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

静态兜底铁律（live push 不可用时**必须落盘**，绝不静默）：
- live 不可用（缺 ORCA_* env / ``orca.chart`` import 失败 / 推送中途失败）→ 每指标一张
  静态图写到 ``$ORCA_ARTIFACTS_DIR/charts/<label 安全化>_<metric>.html``。渲染梯队
  plotly → matplotlib → **零依赖内联 SVG HTML（保底，plotly/matplotlib 均缺也落盘）**；
- **单点数据同样落盘**（1 个点的曲线图 + 标题注明 single point）；
- 训练在本脚本启动前已结束（done-marker 已存在——warmup 窗口内完成的快路径 / 完成后
  agent 补跑）→ 首轮即 drain + 推/落 + exit 0（幂等，可随时补跑）。

fail-soft 铁律（本脚本**绝不**影响训练 rc / 训练 log / 训练进程）：
- orca.chart 不可用 / 缺 ORCA_* env / socket 不可达 → stderr 一次 + 静态落盘 + exit 0；
- progress 文件缺失（训练首次写稍晚创建）→ 轮询等待，直到出现或 ``--max-wait`` 超时；
- 训练进程退出（``--done-marker`` 的 mtime 晚于本脚本启动 = 本次 attempt 结束）→ 最后一次推/落后 exit 0；
- 已推过点后 progress 超过 ``--max-idle`` 秒无增长（异常停滞兜底）→ 推/落后 exit 0；
- **半行缓冲**：读到的不以 ``\n`` 结尾（flush 未完）→ 留 tail 下次 poll 拼接，不丢点不崩。

退出时机与 self-heal 兼容：self-heal 整组 ``kill -- -PID`` 时本脚本随进程组一并被杀，无需自清理；
正常完成由 done-marker 驱动退出（不依赖 idle 空等）。

用法（launch.sh 内训练前启动；agent 完成路径补跑同此命令）：
    python3 progress_watcher.py --progress "runs/train/progress.jsonl" \
        --done-marker "runs/train/.train_rc" \
        --label "puzzle-supernet/train" \
        [--title "<自定义标题>"] \
        [--poll 5] [--max-idle 120] [--max-wait 120]
    --title 缺省 = "<label> (attempt N)"——N 自 done-marker 同目录的
    .train_attempt / .retrain_attempt 文件自推导（缺文件/损坏回退 1），
    launch 伴生启动与完成路径补跑同走本推导 → 同 label+attempt 同 title，
    live 推送的去重替换语义保持。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# repo 根 bootstrap：脚本位于 workflows/agents/<node>/scripts/ → parents[4] = 仓库根。
# artifacts cwd + 无 PYTHONPATH 时 `from orca.chart import render_chart` 也能命中
# （orca.chart 只依赖 stdlib）。幂等：已可 import / 仓库根不含 orca 包时不动 sys.path。
_REPO_ROOT = Path(__file__).resolve().parents[4]
if (_REPO_ROOT / "orca" / "__init__.py").is_file() and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

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


def _resolve(path_str: str) -> Path:
    """Resolve a relative arg against cwd first, then ``$ORCA_ARTIFACTS_DIR``.

    launch.sh 以 cwd=artifacts 启动本脚本；agent 完成路径补跑时 cwd 可能不同——
    相对路径在 cwd 下不存在时回退 artifacts 根（绝对路径原样返回）。
    """
    p = Path(path_str)
    if p.is_absolute() or p.exists():
        return p
    ad = os.environ.get("ORCA_ARTIFACTS_DIR", "")
    if ad:
        cand = Path(ad) / path_str
        if cand.exists():
            return cand
    return p


def _derive_default_title(label: str, done_marker: Path) -> str:
    """``--title`` 缺省时的自推导标题：``<label> (attempt N)``。

    N 读 done-marker 同目录的 ``.*_attempt`` 文件（launch.sh 每 attempt 写
    ``.train_attempt`` / ``.retrain_attempt``；缺失/损坏回退 1）。launch 伴生
    watcher 与 agent 完成路径补跑同走本推导 → 同 label+attempt 必同 title，
    live 推送的同 label+title 替换刷新（dedup）语义保持。
    """
    attempt = 1
    try:
        candidates = sorted(done_marker.parent.glob(".*_attempt"))
        if candidates:
            attempt = int(candidates[0].read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        attempt = 1
    return f"{label} (attempt {attempt})"


def _finish(args: argparse.Namespace, live_mode: bool, render_chart: object,
            series: dict[str, list[tuple[float, float]]]) -> int:
    """Attempt-exit path: final live push; on failure (or non-live) the static floor.

    Guarantees at least one on-disk chart per metric whenever live push is
    unavailable — the static renderer itself has a zero-dependency HTML floor.
    """
    if live_mode:
        if _push(args, render_chart, series):
            return 0
        sys.stderr.write("[progress_watcher] live push failed at exit — falling back to static\n")
    _render_static_charts(args, series)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Tail progress JSONL and push a live multi-metric line chart.")
    ap.add_argument("--progress", required=True,
                    help="progress.jsonl path (relative to $ORCA_ARTIFACTS_DIR or absolute)")
    ap.add_argument("--done-marker", required=True,
                    help="attempt completion marker (launch.sh wrapper writes .train_rc / .retrain_rc)")
    ap.add_argument("--label", required=True, help="chart group key (dedup dimension 1)")
    ap.add_argument("--title", default=None,
                    help='chart title (dedup dimension 2, unique per label); default = "<label> (attempt N)" '
                         "with N derived from the .*_attempt file next to the done-marker")
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

    progress_path = _resolve(args.progress)
    done_marker = _resolve(args.done_marker)

    # --title 缺省 → "<label> (attempt N)"（N 自 done-marker 同目录 attempt 文件自推导）。
    if not args.title:
        args.title = _derive_default_title(args.label, done_marker)

    # Marker 已在启动时存在 = 本次 attempt 已结束（warmup 窗口内完成的快路径 / 完成后补跑）
    # → 首轮即终态退出；不存在时按 mtime 晚于启动时间判定结束。
    marker_existed_at_start = done_marker.is_file()
    start_mtime = done_marker.stat().st_mtime if marker_existed_at_start else 0.0

    series: dict[str, list[tuple[float, float]]] = {}
    last_growth = 0.0
    offset = 0
    tail = ""
    wait_started = time.monotonic()

    while True:
        # 2a. done-marker exit (mtime later than script start = attempt finished;
        #     marker existed at start = attempt already finished before this run).
        try:
            marker_mtime = done_marker.stat().st_mtime
        except OSError:
            marker_mtime = None
        if marker_mtime is not None and (marker_existed_at_start or marker_mtime > start_mtime):
            _drain(progress_path, offset, tail, series)
            return _finish(args, live_mode, render_chart, series)

        # 2b/2c. incremental read.
        offset, tail, new_point = _drain(progress_path, offset, tail, series)

        if not progress_path.is_file():
            if time.monotonic() - wait_started > args.max_wait:
                return _finish(args, live_mode, render_chart, series)
            time.sleep(args.poll)
            continue

        if new_point:
            last_growth = time.monotonic()
            if live_mode:
                if not _push(args, render_chart, series):
                    # Live socket broke mid-run — switch to static for the final render.
                    live_mode = False

        # 2d. idle fallback (only after first point).
        if last_growth and time.monotonic() - last_growth > args.max_idle:
            return _finish(args, live_mode, render_chart, series)

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
    """Write each metric as a static chart when live push is unavailable.

    铁律：**必须落盘**（plotly → matplotlib → 零依赖 SVG HTML 保底），单点数据也落
    （标题注明 single point）。Fail-soft：目录不可写等彻底失败写 stderr 返回，
    绝不影响训练。Output: ``$ORCA_ARTIFACTS_DIR/charts/<safe_label>_<safe_metric>.html``
    (plotly / 纯 HTML) 或 ``.png`` (matplotlib)。
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
        single_note = " (single point)" if len(pts) == 1 else ""
        title = f"{args.title}: {name}{single_note}"
        out_stem = charts_dir / f"{safe_label}_{safe_name}"

        # Try plotly first (self-contained interactive HTML).
        try:
            import plotly.graph_objects as go  # noqa: PLC0415

            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=[x for x, _ in pts],
                y=[y for _, y in pts],
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
            ax.plot([x for x, _ in pts], [y for _, y in pts], "-o", linewidth=1.5, markersize=4)
            ax.set_title(title)
            ax.set_xlabel("step")
            ax.set_ylabel(name)
            fig.tight_layout()
            out = out_stem.with_suffix(".png")
            fig.savefig(str(out), dpi=120, bbox_inches="tight")
            plt.close(fig)
            sys.stderr.write(f"[progress_watcher] static chart: {out}\n")
            continue
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[progress_watcher] matplotlib failed for {name}: {exc}\n")

        # Zero-dependency floor: inline-SVG self-contained HTML. plotly/matplotlib
        # 均不可用也必须落盘（此前这里是静默不落盘的失败终点）。
        try:
            out = out_stem.with_suffix(".html")
            out.write_text(
                _pure_html_line_chart(pts, title, "step", name), encoding="utf-8"
            )
            sys.stderr.write(f"[progress_watcher] static chart (svg floor): {out}\n")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[progress_watcher] static render failed for {name}: {exc}\n")


def _pure_html_line_chart(
    pts: list[tuple[float, float]], title: str, x_label: str, y_label: str
) -> str:
    """Self-contained HTML line chart with an inline SVG polyline (stdlib only).

    Handles the single-point case (marker only, no polyline) — the chart is
    still rendered and the caller's title notes "single point".
    """
    xs = [x for x, _ in pts]
    ys = [y for _, y in pts]
    w, h, ml, mr, mt, mb = 920, 440, 70, 30, 50, 50
    pw, ph = w - ml - mr, h - mt - mb

    def _span(vals: list[float]) -> float:
        return (max(vals) - min(vals)) or 1.0

    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)

    def _px(x: float) -> float:
        return ml + (x - xmin) / _span(xs) * pw

    def _py(y: float) -> float:
        return mt + (1 - (y - ymin) / _span(ys)) * ph

    svg_parts = [
        f"<line x1='{ml}' y1='{mt}' x2='{ml}' y2='{mt + ph}' stroke='#999'/>",
        f"<line x1='{ml}' y1='{mt + ph}' x2='{ml + pw}' y2='{mt + ph}' stroke='#999'/>",
        f"<text x='{ml}' y='{mt - 12}' font-size='12' fill='#333'>{html.escape(f'{y_label} max={ymax:g}')}</text>",
        f"<text x='{ml}' y='{mt + ph + 18}' font-size='12' fill='#333'>{html.escape(f'{x_label} min={xmin:g}')}</text>",
        f"<text x='{ml + pw}' y='{mt + ph + 18}' font-size='12' fill='#333' text-anchor='end'>{html.escape(f'max={xmax:g}')}</text>",
        f"<text x='8' y='{mt + ph / 2}' font-size='12' fill='#333'>{html.escape(f'{y_label} min={ymin:g}')}</text>",
    ]
    if len(pts) > 1:
        pline = " ".join(f"{_px(x):.1f},{_py(y):.1f}" for x, y in pts)
        svg_parts.append(
            f"<polyline fill='none' stroke='#2563eb' stroke-width='2' points='{pline}'/>"
        )
    for x, y in pts:
        svg_parts.append(
            f"<circle cx='{_px(x):.1f}' cy='{_py(y):.1f}' r='4' fill='#2563eb'/>"
        )

    body = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        f"<title>{html.escape(title)}</title></head><body>",
        f"<h2>{html.escape(title)}</h2>",
        f"<svg width='{w}' height='{h}' viewBox='0 0 {w} {h}' "
        "xmlns='http://www.w3.org/2000/svg' style='background:#fafafa'>",
        *svg_parts,
        "</svg>",
        f"<p style='color:#555;font-size:13px'>{len(pts)} point(s). "
        "Static fallback chart (live push unavailable); rendered by progress_watcher.py.</p>",
        "</body></html>",
    ]
    return "\n".join(body)


if __name__ == "__main__":
    sys.exit(main())
