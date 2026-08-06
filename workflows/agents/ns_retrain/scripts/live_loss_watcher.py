#!/usr/bin/env python3
"""live_loss_watcher.py —— tail 训练 log，边训练边推 loss 曲线到前端。

训练 detach 后本脚本在训练进程组内伴生运行（由 ``launch.sh`` 在 setsid wrapper 里、
训练脚本**之前**后台启动）。解析 ns_train_script / ns_retrain 生成契约的进度行
（逐字，见生成契约）：
    epoch <cur>/<total> loss <v>     （epoch-based）
    step  <cur>/<total> loss <v>     （step-based）
每出现新进度点，把**全量**累计点经 ``orca.chart.render_chart`` 推 line 图——同
label+title 重复推送 → 前端替换旧图（实时更新语义，phase-9d §2.7 dedup）。

fail-soft 铁律（本脚本**绝不**影响训练 rc / 训练 log / 训练进程）：
- orca.chart 不可用 / 缺 ORCA_* env / socket 不可达 → stderr 一次 + exit 0（断更不轰炸）；
- log 文件缺失（wrapper 重定向稍晚创建）→ 轮询等待，直到出现或 ``--max-wait-log`` 超时；
- 训练进程退出（``--done-marker`` 的 mtime 晚于本脚本启动 = 本次 attempt 结束）→ 最后一次
  推图后 exit 0；
- 已推过点后 log 超过 ``--max-idle`` 秒无增长（异常停滞兜底）→ exit 0。

退出时机与 self-heal 兼容：self-heal 整组 ``kill -- -PID`` 时本脚本随进程组一并被杀，
无需自清理；正常完成由 done-marker 驱动退出（不依赖 idle 空等）。

用法（launch.sh 内、训练脚本前启动）：
    python3 live_loss_watcher.py --log "runs/train/train.attempt1.log" \
        --done-marker "runs/train/.train_rc" \
        --label "nas-supernet/train" --title "Supernet Training Loss (attempt 1)" \
        [--poll 5] [--max-idle 120] [--max-wait-log 120]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

# render_chart 强依赖的 4 个身份键（与 ``orca.chart._render._REQUIRED_ENV`` 同集）。
_REQUIRED_ENV = ("ORCA_RUN_ID", "ORCA_NODE", "ORCA_SESSION_ID", "ORCA_CHART_SOCK")

# 生成契约进度行（逐字）：``epoch <cur>/<total> loss <v>`` / ``step <cur>/<total> loss <v>``。
# 严格整行匹配：行首锚定 epoch/step + 数字 + loss + 数值，防 log 里其他 ``epoch``/``step``
# 词的歧义行误伤（生成契约本就禁歧义行，此处再收一道）。
_PROGRESS_RE = re.compile(
    r"^(?P<unit>epoch|step)\s+(?P<cur>\d+)\s*/\s*(?P<total>\d+)\s+loss\s+"
    r"(?P<loss>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*$"
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Tail training log and push live loss curve.")
    ap.add_argument("--log", required=True, help="训练 log 路径（相对 $ORCA_ARTIFACTS_DIR 或绝对）")
    ap.add_argument("--done-marker", required=True,
                    help="本次 attempt 完成 marker（launch.sh wrapper 末尾写 .train_rc / .retrain_rc）")
    ap.add_argument("--label", required=True, help="chart 分组键（dedup 维度 1）")
    ap.add_argument("--title", required=True, help="chart 标题（dedup 维度 2，同 label 下唯一）")
    ap.add_argument("--poll", type=float, default=5.0, help="轮询间隔秒数")
    ap.add_argument("--max-idle", type=float, default=120.0,
                    help="已推过点后 log 无增长的退出兜底秒数")
    ap.add_argument("--max-wait-log", type=float, default=120.0,
                    help="log 文件未出现时等待超时秒数（超时 exit 0，不轰炸）")
    args = ap.parse_args()

    # 1. env 检查（缺任一 → stderr 一次 + exit 0；训练照跑，只是不推图）。
    missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        sys.stderr.write(
            f"[live_loss_watcher] 缺 ORCA_* env（{', '.join(missing)}）——"
            "实时推送不可用，退出（不影响训练）\n"
        )
        return 0

    # 2. orca.chart 可用性（import 失败 → 同样静默退）。
    try:
        from orca.chart import render_chart  # noqa: PLC0415 -- 仅此处需要
    except Exception:  # noqa: BLE001 -- fail-soft：缺包不阻断训练
        sys.stderr.write("[live_loss_watcher] orca.chart 不可用——实时推送不可用，退出（不影响训练）\n")
        return 0

    log_path = Path(args.log)
    done_marker = Path(args.done_marker)
    start_ts = time.monotonic()
    start_mtime = done_marker.stat().st_mtime if done_marker.is_file() else 0.0

    points: list[dict[str, float]] = []
    unit = ""
    offset = 0
    last_growth = 0.0  # 0 = 尚无任何点（首点前不启用 idle 退出——首个 epoch 可能极慢）
    log_wait_started = time.monotonic()

    while True:
        # 2a. done-marker 驱动退出（mtime 晚于本脚本启动 = 本次 attempt 真结束，
        # 防前次 attempt 的 stale marker 让续训 watcher 一启动就退）。
        try:
            if done_marker.stat().st_mtime > start_mtime:
                _push(args, render_chart, points, unit)  # 最后一次推图（失败也照退）
                return 0
        except OSError:
            pass

        # 2b. log 文件缺失 → 等待（wrapper 重定向创建稍晚）。
        try:
            size = log_path.stat().st_size
        except OSError:
            if time.monotonic() - log_wait_started > args.max_wait_log:
                return 0
            time.sleep(args.poll)
            continue

        # 2c. 增量读新行 → 解析契约进度行 → 累计点。
        if size > offset:
            with log_path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                new_lines = f.read().splitlines()
            offset = size
            new_point = False
            for line in new_lines:
                m = _PROGRESS_RE.match(line.strip())
                if not m:
                    continue
                if not unit:
                    unit = m.group("unit")  # 首个匹配决定轴名（epoch / step）
                try:
                    points.append({"x": float(m.group("cur")), "y": float(m.group("loss"))})
                except ValueError:
                    continue
                new_point = True
            if new_point:
                last_growth = time.monotonic()
                if not _push(args, render_chart, points, unit):
                    return 0  # 断更：stderr 已写，退出（不影响训练）

        # 2d. idle 兜底：仅对「已推过点」生效（首个 epoch 可能超过 --max-idle）。
        if last_growth and time.monotonic() - last_growth > args.max_idle:
            return 0

        time.sleep(args.poll)


def _push(args: argparse.Namespace, render_chart: object, points: list[dict[str, float]],
          unit: str) -> bool:
    """推一次全量曲线（同 title → 前端替换）。成功返 True；失败 → stderr 一次 + 返 False。

    失败即断更（socket 断 / 守护退 / run 终态）——调用方 return 0，不重试轰炸。
    """
    if not points:
        return True
    try:
        render_chart(  # type: ignore[misc] -- render_chart 经 import 检查
            chart_type="line",
            data=points,
            label=args.label,
            title=args.title,
            x="x",
            y="y",
            x_label=unit or "epoch",  # 首个点前不会走到这里；unit 保底
            y_label="loss",
        )
    except Exception as exc:  # noqa: BLE001 -- fail-soft：socket 断（daemon 退 / run 终态）→ 断更
        sys.stderr.write(
            f"[live_loss_watcher] render_chart 失败：{exc}——实时推送断更，退出（不影响训练）\n"
        )
        return False
    return True


if __name__ == "__main__":
    sys.exit(main())
