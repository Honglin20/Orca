"""gen_big_fixture.py —— 生成 50MB / 500k-event fixture tape（SPEC web-attach §8 perf AC）。

用法::

    python scripts/gen_big_fixture.py [--out runs/big.jsonl] [--events 500000]

产出格式：每行一个完整 Event JSON（与真实 tape 同形），含 ``workflow_started`` +
N 条 ``agent_thinking`` / ``agent_message`` / ``agent_tool_call`` / ``agent_usage`` /
``node_started`` / ``node_completed`` / ``route_taken`` + 末尾 ``workflow_completed``。

测试 perf AC（§8.4）：
  - ``GET /meta`` P99 < 100ms
  - ``GET /events?tail=500`` P99 < 300ms
  - 浏览器首屏无 console error
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def gen_event(seq: int, type: str, node: str, data: dict) -> str:
    """构造一行 tape event JSON（与 ``Tape.append`` 同形）。"""
    payload = {
        "seq": seq,
        "type": type,
        "timestamp": time.time(),
        "node": node,
        "session_id": f"big-fix-session-{seq % 7}",
        "data": data,
    }
    return json.dumps(payload, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 50MB/500k-event perf fixture tape")
    parser.add_argument(
        "--out", default="runs/big.jsonl", help="输出 tape 文件路径（默认 runs/big.jsonl）"
    )
    parser.add_argument(
        "--events", type=int, default=500_000, help="事件数（默认 500k，约 50MB）"
    )
    args = parser.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    n = max(2, args.events)  # 至少 2 个事件（首 workflow_started + 末 workflow_completed）
    started = time.time()
    # topology：5 个 node，链式
    topology = {
        "entry": "n1",
        "nodes": [
            {"name": f"n{i}", "kind": "agent"} for i in range(1, 6)
        ],
        "routes": [
            {"from": f"n{i}", "to": f"n{i + 1}" if i < 5 else "$end"}
            for i in range(1, 6)
        ],
        "parallel": [],
    }

    with out.open("w", encoding="utf-8") as f:
        seq = 0
        # workflow_started
        seq += 1
        f.write(
            gen_event(
                seq,
                "workflow_started",
                None,
                {
                    "inputs": {"task": "perf fixture"},
                    "node_count": 5,
                    "entry": "n1",
                    "workflow_name": "big_fixture",
                    "topology": topology,
                },
            )
            + "\n"
        )
        # N - 2 个 agent_* 事件循环（轮转 node n1..n5）
        nodes = [f"n{i}" for i in range(1, 6)]
        event_types = [
            "agent_thinking",
            "agent_message",
            "agent_tool_call",
            "agent_tool_result",
            "agent_usage",
        ]
        for i in range(n - 2):
            seq += 1
            node = nodes[seq % 5]
            etype = event_types[seq % len(event_types)]
            data: dict
            if etype == "agent_thinking":
                data = {"text": f"thinking chunk #{i} " + ("x" * 60)}
            elif etype == "agent_message":
                data = {"text": f"message chunk #{i} " + ("y" * 60)}
            elif etype == "agent_tool_call":
                data = {"tool": "shell", "tool_call_id": f"call_{i}", "args": {"cmd": "ls"}}
            elif etype == "agent_tool_result":
                data = {"tool": "shell", "tool_call_id": f"call_{i}", "output": "ok"}
            else:  # agent_usage
                data = {
                    "input_tokens": 1000 + (i % 100),
                    "output_tokens": 500 + (i % 50),
                    "reasoning_tokens": 50 + (i % 10),
                    "cost_usd": 0.001,
                }
            f.write(gen_event(seq, etype, node, data) + "\n")
        # workflow_completed
        seq += 1
        f.write(
            gen_event(
                seq,
                "workflow_completed",
                None,
                {"elapsed": 123.4, "outputs": {"result": "done"}},
            )
            + "\n"
        )

    size_mb = out.stat().st_size / (1024 * 1024)
    elapsed = time.time() - started
    print(
        f"生成 {seq} 事件 → {out} ({size_mb:.1f}MB) in {elapsed:.1f}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
