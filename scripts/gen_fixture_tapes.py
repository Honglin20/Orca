"""gen_fixture_tapes.py —— AC7 fixture：生成 N 个最小合法 tape 到指定 runs_dir。

SPEC ``docs/specs/2026-08-10-home-list-lazy-index.md`` §5 AC7 线性度计时用。

用法::

    python scripts/gen_fixture_tapes.py --runs-dir /tmp/orca-fixture/runs --count 1354
    python scripts/gen_fixture_tapes.py --runs-dir <dir> --count 13540

每个 tape = 2 个事件（``workflow_started`` + ``workflow_completed``），最小合法且能被
``_scan_meta_overview`` 单遍 fold 出 overview（含 workflow_name / started_ts / ended_ts），
供温路径 discovery（``RunManager.discover_runs``）计时。

典型配合（先 warm persistent cache，再计时命中态 discovery）::

    # 1. 生成 fixture
    python scripts/gen_fixture_tapes.py --runs-dir <dir> --count 1354
    # 2. 注册项目根（<dir>/.. 作为 project root），或直接构造 RunManager 调 discover_runs。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _event(seq: int, etype: str, timestamp: float, data: dict) -> str:
    """构造一行 tape event JSON（与 ``Tape.append`` 同形）。"""
    payload = {
        "seq": seq,
        "type": etype,
        "timestamp": timestamp,
        "node": None,
        "session_id": None,
        "data": data,
    }
    return json.dumps(payload, ensure_ascii=False)


def gen_tape(path: Path, run_id: str, wf_name: str = "fixture_wf") -> None:
    """生成单个最小合法 tape（workflow_started + workflow_completed）。"""
    started = time.time()
    lines = [
        _event(
            1,
            "workflow_started",
            started,
            {
                "inputs": {},
                "node_count": 1,
                "entry": "n1",
                "workflow_name": wf_name,
                "topology": {"nodes": [{"name": "n1"}]},
            },
        ),
        _event(
            2,
            "workflow_completed",
            started + 1.0,
            {"elapsed": 1.0, "outputs": {}},
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="生成 N 个最小合法 tape 到 runs_dir（AC7 线性度 fixture）",
    )
    parser.add_argument(
        "--runs-dir", required=True,
        help="输出 runs 目录（如 /tmp/orca-fixture/runs）",
    )
    parser.add_argument(
        "--count", type=int, required=True,
        help="生成 tape 数（如 1354 / 13540）",
    )
    parser.add_argument(
        "--wf-name", default="fixture_wf",
        help="workflow_name（默认 fixture_wf）",
    )
    args = parser.parse_args()

    runs_dir = Path(args.runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    n = max(1, args.count)
    t0 = time.time()
    for i in range(n):
        run_id = f"fixture-{i:06d}"
        gen_tape(runs_dir / f"{run_id}.jsonl", run_id, wf_name=args.wf_name)
    elapsed = time.time() - t0
    print(
        f"生成 {n} tape → {runs_dir} in {elapsed:.1f}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
