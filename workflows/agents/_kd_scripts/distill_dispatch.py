"""distill_dispatch.py —— BLK-17 确定性 gate：据 selector.tune_status 决定 distill 动作。

为什么需要：distill 节点对 FAIL_latency 变体应 **no-op**（不训练，直接出 status=FAIL_latency）。
若让 LLM 自行判断，可能误 emit SUCCESS（跳训练）或误 emit FAIL_latency（对 tune-ACCEPTED 变体），
recorder 无法检测 → 错行被 done 谓词永久跳过（静默且永久）。本脚本把「noop vs train」从 LLM
判断下沉为确定性求值：distill agent 必须先调本脚本，据 ``DISTILL_ACTION`` 分支，且**禁止**在
noop 时 emit SUCCESS。recorder 另行断言 ``selector.tune_status`` 与 ``distill.status`` 一致。

CLI::
    python3 distill_dispatch.py --tune_status <ACCEPTED|FAIL_latency>

stdout::
    DISTILL_ACTION: noop    # tune_status=FAIL_latency → 不训练
    DISTILL_ACTION: train   # tune_status=ACCEPTED → 走完整蒸馏训练

fail loud：tune_status 非法（既非 ACCEPTED 也非 FAIL_latency）→ exit 2。
"""

from __future__ import annotations

import argparse
import sys


def dispatch(tune_status: str) -> str:
    s = (tune_status or "").strip()
    if s == "FAIL_latency":
        return "noop"
    if s == "ACCEPTED":
        return "train"
    raise ValueError(
        f"tune_status 非法：{tune_status!r}；须 ∈ {{ACCEPTED, FAIL_latency}}"
    )


def _main() -> int:
    p = argparse.ArgumentParser(description="BLK-17 distill 确定性 gate（noop|train）")
    p.add_argument("--tune_status", required=True,
                   help="selector.output.tune_status（ACCEPTED|FAIL_latency）")
    args = p.parse_args()
    try:
        action = dispatch(args.tune_status)
    except ValueError as e:
        print(f"[distill_dispatch] FAIL: {e}", file=sys.stderr)
        return 2
    print(f"DISTILL_ACTION: {action}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
