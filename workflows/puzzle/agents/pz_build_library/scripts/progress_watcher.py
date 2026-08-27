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
    python3 progress_watcher.py --progress "runs/bld/progress.jsonl" \
        --done-marker "runs/bld/.bld_rc" \
        --label "puzzle/bld" --title "BLD Metrics (attempt 1)" \
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
                    help="progress.jsonl 路径（相对 $ORCA_ARTIFACTS_DIR 或绝对）")
    ap.add_argument("--done-marker", required=True,
                    help="本次 attempt 完成 marker（launch.sh wrapper 末尾写 .bld_rc）")
    ap.add_argument("--label", required=True, help="chart 分组键（dedup 维度 1）")
    ap.add_argument("--title", required=True, help="chart 标题（dedup 维度 2，同 label 下唯一）")
    ap.add_argument("--poll", type=float, default=5.0, help="轮询间隔秒数")
    ap.add_argument("--max-idle", type=float, default=120.0,
                    help="已推过点后 progress 无增长的退出兜底秒数")
    ap.add_argument("--max-wait", type=float, default=120.0,
                    help="progress 文件未出现时等待超时秒数（超时 exit 0，不轰炸）")
    args = ap.parse_args()

    # 1. env 检查（缺任一 → stderr 一次 + exit 0；训练照跑，只是不推图）。
    missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        sys.stderr.write(
            f"[progress_watcher] 缺 ORCA_* env（{', '.join(missing)}）——"
            "实时推送不可用，退出（不影响训练）\n"
        )
        return 0

    # 2. orca.chart 可用性（import 失败 → 同样静默退）。
    try:
        from orca.chart import render_chart  # noqa: PLC0415 -- 仅此处需要
    except Exception:  # noqa: BLE001 -- fail-soft：缺包不阻断训练
        sys.stderr.write("[progress_watcher] orca.chart 不可用——实时推送不可用，退出（不影响训练）\n")
        return 0

    progress_path = Path(args.progress)
    done_marker = Path(args.done_marker)
    start_mtime = done_marker.stat().st_mtime if done_marker.is_file() else 0.0

    # series: metric 名 -> 该 metric 的 (x, y) 累计点（插入序保持，每指标一张独立图）。
    series: dict[str, list[tuple[float, float]]] = {}
    last_growth = 0.0  # 0 = 尚无任何点（首点前不启用 idle 退出——首个 unit 可能极慢）
    offset = 0  # 已消费的字节偏移（与 stat st_size 同基，二进制读）
    tail = ""  # 半行缓冲：上次读到的不完整行（无 \n 结尾），下次拼接
    wait_started = time.monotonic()

    while True:
        # 2a. done-marker 驱动退出（mtime 晚于本脚本启动 = 本次 attempt 真结束，
        # 防前次 attempt 的 stale marker 让续训 watcher 一启动就退）。
        # drain 末点：done-marker touch 与上次 poll 间最多隔一个 poll 周期（~5s），
        # 这段时间 progress.jsonl 新写的点（往往是最后一个 epoch）必须先 drain 再推。
        try:
            if done_marker.stat().st_mtime > start_mtime:
                offset, tail, _ = _drain(progress_path, offset, tail, series)
                _push(args, render_chart, series)  # 最后一次推图（失败也照退）
                return 0
        except OSError:
            pass

        # 2b/2c. 增量读新字节 → 半行缓冲拼接 → 解析 JSONL 行 → 累计点（DRY：_drain
        # helper 与 done-marker 退出路径共用，零行为漂移）。
        offset, tail, new_point = _drain(progress_path, offset, tail, series)

        # progress 文件缺失 → 等待（训练首次 append 创建稍晚）。
        if not progress_path.is_file():
            if time.monotonic() - wait_started > args.max_wait:
                return 0
            time.sleep(args.poll)
            continue

        if new_point:
            last_growth = time.monotonic()
            if not _push(args, render_chart, series):
                return 0  # 断更：stderr 已写，退出（不影响训练）

        # 2d. idle 兜底：仅对「已推过点」生效（首个 unit 可能超过 --max-idle）。
        if last_growth and time.monotonic() - last_growth > args.max_idle:
            return 0

        time.sleep(args.poll)


def _push(args: argparse.Namespace, render_chart: object,
          series: dict[str, list[tuple[float, float]]]) -> bool:
    """推一次每指标的独立曲线（同 label+title → 前端替换）。成功返 True；失败 → stderr 一次 + 返 False。

    失败即断更（socket 断 / 守护退 / run 终态）——调用方 return 0，不重试轰炸。
    """
    if not series:
        return True
    # 每指标一张独立图：同 label + 同 title → 前端替换（实时更新语义）；不同 title → 独立图。
    # 指标名/种类不可预测（用户有什么推什么），故 title 带真实指标名，不做 loss/acc 假设。
    for name, pts in series.items():
        data = [{"x": x, "y": y} for x, y in pts]
        try:
            render_chart(  # type: ignore[misc] -- render_chart 经 import 检查
                chart_type="line",
                data=data,
                label=args.label,
                title=f"{args.title}: {name}",
                x="x",
                y="y",
                x_label="step",
                y_label=name,
            )
        except Exception as exc:  # noqa: BLE001 -- fail-soft：socket 断（daemon 退 / run 终态）→ 断更
            sys.stderr.write(
                f"[progress_watcher] render_chart 失败：{exc}——实时推送断更，退出（不影响训练）\n"
            )
            return False
    return True


if __name__ == "__main__":
    sys.exit(main())
