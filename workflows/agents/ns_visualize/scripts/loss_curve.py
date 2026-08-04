#!/usr/bin/env python3
"""loss_curve.py -- Supernet training loss curve for ns_visualize.

Reads the latest training attempt log (``runs/train/train.attempt*.log``) and
parses (step, loss) pairs. Pushes a ``line`` chart showing training convergence.

Fail-soft: no log file or no parseable loss lines -> records "skipped".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import find_latest_attempt_log, parse_loss_log, push_chart  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Push supernet training loss curve chart.")
    ap.add_argument("--artifacts-dir", required=True)
    args = ap.parse_args()
    ad = Path(args.artifacts_dir)

    log_path = find_latest_attempt_log(ad, "train", "train")
    if log_path is None:
        push_chart(
            artifacts_dir_path=ad, script_name="loss_curve", label="nas-supernet/training",
            title="Supernet Training Loss", chart_type="line", data=[],
            skip_reason="no runs/train/train.attempt*.log found",
        )
        return 0

    points = parse_loss_log(log_path)
    if not points:
        push_chart(
            artifacts_dir_path=ad, script_name="loss_curve", label="nas-supernet/training",
            title="Supernet Training Loss", chart_type="line", data=[],
            skip_reason=f"no (step, loss) pairs parsed from {log_path.name}",
        )
        return 0

    push_chart(
        artifacts_dir_path=ad,
        script_name="loss_curve",
        label="nas-supernet/training",
        title="Supernet Training Loss",
        chart_type="line",
        data=points,
        x="step",
        y="loss",
        x_label="Training Step",
        y_label="Loss",
        caption=f"Supernet training loss from {log_path.name} ({len(points)} points). Lower is better.",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
