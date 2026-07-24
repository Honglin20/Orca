"""pick_student.py —— Phase1 确定性 student 选择（契约 §4）。

对齐 `workflows/agents/_kd_scripts/CONTRACTS.md` §4 `pick_student`。

Phase1 做线性 sweep：第 N 轮取 `registry[N]`（不取模），把该条目组装成 SelectionSpec 写盘。
N ≥ len(registry) 时 Phase1 已耗尽 → 退出码 1 + stderr `PHASE1_EXHAUSTED`（告诉 hypothesizer
切 Phase2，进入 agent 自由发挥）。

CLI（契约 §4）::

    python3 pick_student.py --registry students/registry.json --round <N> --out <spec.json>

stdout::

    SELECTION_SPEC: <abs path>

fail loud：registry 读不到 / 格式非法 / round < 0 → 非零退出 + stderr。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path


def pick_student(registry_path: str, round_idx: int, out_path: str) -> dict:
    if round_idx < 0:
        raise ValueError(f"round 不能为负，得到 {round_idx}")

    registry_abspath = os.path.abspath(registry_path)
    if not os.path.isfile(registry_abspath):
        raise FileNotFoundError(f"registry 文件不存在: {registry_abspath}")
    try:
        registry = json.loads(Path(registry_abspath).read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"registry 非合法 JSON: {e}") from e

    if not isinstance(registry, list) or not registry:
        raise ValueError(
            f"registry 必须是非空 list，得到 {type(registry).__name__}"
            f"(len={len(registry) if hasattr(registry, '__len__') else '?'})"
        )

    n = len(registry)
    if round_idx >= n:
        # Phase1 sweep 耗尽：用专用 exit code 1 + stderr tag。
        # 不抛异常（traceback 太吵）；上层用返回码 + stderr 判定。
        raise _Phase1Exhausted(n)

    entry = registry[round_idx]
    if not isinstance(entry, dict):
        raise ValueError(f"registry[{round_idx}] 不是 dict：{entry!r}")
    if "family" not in entry:
        raise ValueError(f"registry[{round_idx}] 缺 family 字段：{entry!r}")

    family = str(entry["family"])
    build_cfg = entry.get("build_cfg", {}) or {}
    kd_config = entry.get("kd_config", {}) or {}

    spec = {
        "candidate_id": f"f{family}_r{round_idx}",
        "phase": 1,
        "round": round_idx,
        "family": family,
        "build_cfg": build_cfg,
        "kd_config": kd_config,
        "rationale": "Phase1 deterministic sweep",
    }

    out_abspath = os.path.abspath(out_path)
    Path(out_abspath).parent.mkdir(parents=True, exist_ok=True)
    Path(out_abspath).write_text(
        json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return spec


class _Phase1Exhausted(Exception):
    """Phase1 sweep 耗尽（round ≥ len(registry)）。专用信号，非 fail loud。"""
    def __init__(self, n: int):
        super().__init__(f"PHASE1_EXHAUSTED (registry len={n})")
        self.n = n


def _main() -> int:
    p = argparse.ArgumentParser(
        description="Phase1 确定性 student sweep（契约 §4）"
    )
    p.add_argument("--registry", required=True, help="students/registry.json")
    p.add_argument("--round", type=int, required=True, help="当前轮次（从 0 起）")
    p.add_argument("--out", required=True, help="输出 SelectionSpec json 路径")
    args = p.parse_args()

    try:
        spec = pick_student(args.registry, args.round, args.out)
    except _Phase1Exhausted as e:
        # 专用退出码 1 + stdout/stderr 双通道 PHASE1_EXHAUSTED（hypothesizer 从 stdout 读，确定性）。
        print(f"PHASE1_EXHAUSTED: true")
        print(f"# {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[pick_student] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2

    print(f"PHASE1_EXHAUSTED: false")
    print(f"SELECTION_SPEC: {os.path.abspath(args.out)}")
    print(f"# family={spec['family']} round={spec['round']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
