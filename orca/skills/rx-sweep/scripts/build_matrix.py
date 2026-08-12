#!/usr/bin/env python3
"""build_matrix.py —— 生成 rx-sweep 实验矩阵 JSON（contracts §6）。

每 variant × {scratch, kd}（可选 model8_baseline 参考）。每条记录含：
    exp_id / variant / kd(bool) / needs_teacher(kd=True 时 True)

CLI：
    python build_matrix.py --out matrix.json \
        [--variants pure_cnn,pure_cnn_pilot,pure_cnn_lmmse,pure_cnn_pilot_lmmse] \
        [--modes scratch,kd] \
        [--include-baseline]

stdout 末行：``MATRIX: <path>``。

纯 stdlib（用户工程可能没装 orca）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 4 个纯 CNN 族 variant（contracts §1）。
DEFAULT_VARIANTS = (
    "pure_cnn",
    "pure_cnn_pilot",
    "pure_cnn_lmmse",
    "pure_cnn_pilot_lmmse",
)
# 已知 variant 名（contracts §1 + §6）：--variants 早校验用，防 typo 静默产矩阵。
KNOWN_VARIANTS = {
    "pure_cnn",
    "pure_cnn_pilot",
    "pure_cnn_lmmse",
    "pure_cnn_pilot_lmmse",
    "model8",
}
# 训练模式：scratch=从头训，kd=蒸馏（需 teacher）。
DEFAULT_MODES = ("scratch", "kd")
# model8 baseline 的 exp_id（仅作参考线，不蒸馏）。
BASELINE_EXP_ID = "model8_baseline"
BASELINE_VARIANT = "model8"


def build_entries(
    variants: list[str], modes: list[str], include_baseline: bool
) -> list[dict]:
    """按 §6 顺序构造矩阵条目：baseline（可选）→ 每个 variant × 每个模式。"""
    entries: list[dict] = []

    if include_baseline:
        entries.append(
            {
                "exp_id": BASELINE_EXP_ID,
                "variant": BASELINE_VARIANT,
                "kd": False,
                "needs_teacher": False,
            }
        )

    for variant in variants:
        for mode in modes:
            if mode == "scratch":
                entries.append(
                    {
                        "exp_id": f"{variant}_scratch",
                        "variant": variant,
                        "kd": False,
                        "needs_teacher": False,
                    }
                )
            elif mode == "kd":
                entries.append(
                    {
                        "exp_id": f"{variant}_kd",
                        "variant": variant,
                        "kd": True,
                        "needs_teacher": True,
                    }
                )
            else:
                raise ValueError(
                    f"未知 mode {mode!r}（已知：scratch / kd）。请检查 --modes 参数。"
                )

    return entries


def parse_csv(value: str | None, default: tuple[str, ...]) -> list[str]:
    """解析逗号分隔参数；空 → default 列表。逐项 strip，空值 → fail loud。"""
    if value is None or value.strip() == "":
        return list(default)
    items = [s.strip() for s in value.split(",")]
    bad = [s for s in items if s == ""]
    if bad:
        raise ValueError(f"逗号列表含空值：{value!r}")
    return items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成 rx-sweep 实验矩阵 JSON（contracts §6）。"
    )
    parser.add_argument("--out", required=True, help="输出矩阵 JSON 路径。")
    parser.add_argument(
        "--variants",
        default=None,
        help=f"逗号分隔 variant 列表（默认：{','.join(DEFAULT_VARIANTS)}）。",
    )
    parser.add_argument(
        "--modes",
        default=None,
        help=f"逗号分隔模式列表（默认：{','.join(DEFAULT_MODES)}）。",
    )
    parser.add_argument(
        "--include-baseline",
        action="store_true",
        help="包含 model8_baseline 参考实验。",
    )
    args = parser.parse_args(argv)

    variants = parse_csv(args.variants, DEFAULT_VARIANTS)
    modes = parse_csv(args.modes, DEFAULT_MODES)

    # 校验 variant / mode 早失败（fail loud）。
    bad_variants = [v for v in variants if v not in KNOWN_VARIANTS]
    if bad_variants:
        print(
            f"[build_matrix] 错误：未知 variant {bad_variants}"
            f"（已知：{sorted(KNOWN_VARIANTS)}）",
            file=sys.stderr,
        )
        return 2
    known_modes = {"scratch", "kd"}
    bad_modes = [m for m in modes if m not in known_modes]
    if bad_modes:
        print(
            f"[build_matrix] 错误：未知 mode {bad_modes}（已知：{sorted(known_modes)}）",
            file=sys.stderr,
        )
        return 2

    entries = build_entries(variants, modes, args.include_baseline)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # 末行契约：MATRIX: <path> 含 N 实验。
    print(f"MATRIX: {out_path} 含 {len(entries)} 实验")
    return 0


if __name__ == "__main__":
    sys.exit(main())
