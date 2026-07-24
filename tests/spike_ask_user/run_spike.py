"""run_spike.py —— P3:0-b ask-user spike 的 CLI 入口。

两种后端可选：
- ``--backend mock``（默认）：完全确定性，不依赖 claude / API key。验证 driver 逻辑：
  哨兵检测 → task_id 捕获 → resume → orca next 闭环 + 重入 3 次 fail loud。
- ``--backend claude``：真 spawn ``claude -p``。前置：claude CLI 在 PATH + 配了 API key。

**典型用法（mock 主路径）**::

    python -m tests.spike_ask_user.run_spike --backend mock
    python -m tests.spike_ask_user.run_spike --backend mock --scenario reentry

**典型用法（claude 真路径）**::

    python -m tests.spike_ask_user.run_spike --backend claude

输出：``WorkflowDriveLog`` 的 JSON dump（每个节点的 spawn/resume/sentinel/answer/final）+
最终 ``done:true`` 证据。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from tests.spike_ask_user.backend import SubagentBackend
from tests.spike_ask_user.claude_backend import (
    ClaudeCliBackend,
    ClaudeCLINotAvailable,
)
from tests.spike_ask_user.mock_backend import MockSubagentBackend
from tests.spike_ask_user.sentinel import (
    MAX_ASK,
    SentinelLoopExhausted,
    build_sentinel_message,
)
from tests.spike_ask_user.tars_loop import (
    AnswerProvider,
    WorkflowDriveLog,
    drive_workflow,
)

LOGGER = logging.getLogger("spike_ask_user")

# spike workflow yaml 的绝对路径（driver 给 orca CLI 的 ``wf`` 参数）。
_THIS_DIR = Path(__file__).resolve().parent
WF_YAML = str(_THIS_DIR / "spike_ask_user.yaml")


# ── Mock scenarios ───────────────────────────────────────────────────────────


def _mock_scenario_closed_loop() -> list[str]:
    """Mock scenario：哨兵 1 次 → 真实 output。

    - 节点 A spawn：返回哨兵（问 calib loader）。
    - 节点 A resume #1：收到用户答 ``myproj.data:load_calib``，返回真实 output JSON。
    - 节点 B spawn：直接返回真实 output（summary）—— B 不缺数据，不返回哨兵。
    - 节点 B resume：不应被调（断言用）。

    注意：driver 会先调 ``spawn`` 跑节点 A，然后 ``resume`` 一次拿到真实 output，喂 orca next；
    然后 driver 调 ``spawn`` 跑节点 B（**新 task_id**），不 resume。所以 scenario 数组是
    [A_spawn_sentinel, A_resume_real, B_spawn_real]。Mock backend 的 calls_per_task 会区分
    两个不同的 task_id。
    """
    return [
        # A.spawn
        build_sentinel_message(
            question="calib loader 在你项目的 dotted-path 是什么？",
            options=["myproj.data:load_calib", "myproj.dataset:make_loader"],
            context=(
                "我已 glob project_root 下 **/*.py 并 grep DataLoader，"
                "但 project_root 为空/无匹配；请直接给 dotted-path"
            ),
        ),
        # A.resume #1 → 真实 output
        json.dumps(
            {"calib_loader": "myproj.data:load_calib", "source": "user"},
            ensure_ascii=False,
        ),
        # B.spawn → 真实 output（B 不缺数据）
        json.dumps(
            {
                "summary": "已拿到 calib_loader=myproj.data:load_calib，"
                "可继续下游量化流程。"
            },
            ensure_ascii=False,
        ),
    ]


def _mock_scenario_reentry_blocked() -> list[str]:
    """Mock scenario：连续哨兵 > MAX_ASK，driver 必须 fail loud。

    返回 MAX_ASK+1 个哨兵：driver 应在 MAX_ASK（=3）后抛 ``SentinelLoopExhausted``，
    不会触发第 4 次调用。scenario 多给一个是为了证明「即使第 4 个还是哨兵，driver
    也已经在第 3 次后中断了」（Mock backend 的 calls_per_task 应显示 task_id 只被调了
    MAX_ASK 次 = spawn 1 + resume 3 = 4 次；scenario[4] 不该被读到）。
    """
    sentinel_msg = build_sentinel_message(
        question="还是缺 calib loader，再问一次：dotted-path 是什么？",
        options=["myproj.data:load_calib"],
        context="前 N 次问用户都说不知道；继续哨兵直到 driver 中断",
    )
    # MAX_ASK=3 → driver 在 attempts=3 时 raise。spawn + 3 次 resume = 4 次调用。
    # 故 scenario 需 ≥4 个哨兵（spawn + 3 次 resume 都返回哨兵），保险起见多给一个。
    return [sentinel_msg] * (MAX_ASK + 2)


def _mock_scenario_node_b_sentinel() -> list[str]:
    """Mock scenario：节点 A 顺利，节点 B 也哨兵 1 次 → 真实 output。

    用于证明 driver 对**每个节点**都跑哨兵循环（不只在节点 A）。
    """
    return [
        # A.spawn → 真实 output（不哨兵，因为这次 project_root 给了）
        json.dumps(
            {"calib_loader": "myproj.data:load_calib", "source": "inferred"},
            ensure_ascii=False,
        ),
        # B.spawn → 哨兵（B 也缺个东西，比如 dotted-path 里某个参数）
        build_sentinel_message(
            question="summary 文案想要中文还是英文？",
            options=["中文", "English"],
            context="节点 B 哨兵一次（驱动循环应再次跑哨兵循环）",
        ),
        # B.resume #1 → 真实 output
        json.dumps({"summary": "已拿到 calib_loader，闭环成立。"}, ensure_ascii=False),
    ]


SCENARIOS = {
    "closed_loop": _mock_scenario_closed_loop,
    "reentry": _mock_scenario_reentry_blocked,
    "node_b_sentinel": _mock_scenario_node_b_sentinel,
}


# ── Answer providers（模拟「问用户」） ─────────────────────────────────────


def _fixed_answer_provider(answer: str) -> AnswerProvider:
    """恒答 ``answer`` 的 provider（spike 默认用这个）。"""

    def _provide(_question) -> str | None:
        return answer

    return _provide


def _dont_know_provider() -> AnswerProvider:
    """恒答 None（「不知道」）的 provider；用于重入测试。"""

    def _provide(_question) -> str | None:
        return None

    return _provide


# ── Backend factory ──────────────────────────────────────────────────────────


def _make_backend(args: argparse.Namespace) -> SubagentBackend:
    if args.backend == "mock":
        scenario_factory = SCENARIOS[args.scenario]
        return MockSubagentBackend(scenario_factory(), backend_name="mock")
    if args.backend == "claude":
        try:
            return ClaudeCliBackend(
                timeout_s=args.claude_timeout,
                model=args.claude_model or None,
                allowed_tools=tuple(args.claude_tools.split()) if args.claude_tools else ("Bash", "Read"),
            )
        except ClaudeCLINotAvailable as e:
            print(f"[spike] claude backend 不可用：{e}", file=sys.stderr)
            print(
                "[spike] 退回 mock backend（--backend mock）或安装 claude CLI",
                file=sys.stderr,
            )
            sys.exit(2)
    # argparse ``choices=("mock", "claude")`` 已在解析期拦住其他值，此处 unreachable。
    raise AssertionError(f"unreachable: unknown backend {args.backend!r}")


def _make_answer_provider(args: argparse.Namespace) -> AnswerProvider:
    if args.scenario == "reentry":
        # 重入测试：故意答「不知道」3 次，driver 应在 MAX_ASK 后 fail loud。
        return _dont_know_provider()
    return _fixed_answer_provider(args.answer)


# ── Log dump ─────────────────────────────────────────────────────────────────


def _dump_log(log: WorkflowDriveLog, out_path: Path | None) -> None:
    """把 WorkflowDriveLog 序列化成 JSON（诊断 / 测试断言原料）。"""
    payload: dict[str, Any] = {
        "run_id": log.run_id,
        "final_done": log.final_done,
        "final_raw": log.final_raw,
        "nodes": [asdict(n) for n in log.nodes],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"[spike] drive log dumped to {out_path}", file=sys.stderr)
    else:
        print(text)


# ── main ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m tests.spike_ask_user.run_spike",
        description="P3:0-b ask-user spike：哨兵→恢复→真实 output→orca next 闭环验证。",
    )
    p.add_argument(
        "--backend",
        choices=("mock", "claude"),
        default="mock",
        help="子 agent 后端。mock=确定性（默认）；claude=真 spawn claude -p。",
    )
    p.add_argument(
        "--scenario",
        choices=tuple(SCENARIOS.keys()),
        default="closed_loop",
        help="mock scenario 名（仅 --backend mock 生效）。",
    )
    p.add_argument(
        "--answer",
        default="myproj.data:load_calib",
        help="模拟用户答案（仅 closed_loop / node_b_sentinel 生效；reentry 自动用「不知道」）。",
    )
    p.add_argument(
        "--wf",
        default=WF_YAML,
        help="workflow yaml 路径（默认 spike_ask_user.yaml）。",
    )
    p.add_argument(
        "--inputs",
        default="{}",
        help="workflow inputs JSON 串（默认 {}）。",
    )
    p.add_argument(
        "--claude-model",
        default="",
        help="claude backend 的 --model（仅 claude）。",
    )
    p.add_argument(
        "--claude-tools",
        default="Bash Read",
        help="claude backend 的 --allowed-tools，空格分隔（仅 claude）。",
    )
    p.add_argument(
        "--claude-timeout",
        type=float,
        default=120.0,
        help="claude backend 子进程超时秒（仅 claude）。",
    )
    p.add_argument(
        "--log-file",
        default="",
        help="drive log 落盘路径（默认仅 stdout）。",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="DEBUG 日志。",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    backend = _make_backend(args)
    answer_provider = _make_answer_provider(args)
    try:
        inputs = json.loads(args.inputs)
    except json.JSONDecodeError as e:
        print(f"[spike] --inputs 不是合法 JSON: {e}", file=sys.stderr)
        return 2

    LOGGER.info(
        "spike 启动 backend=%s scenario=%s wf=%s", args.backend, args.scenario, args.wf
    )

    exit_code = 0
    try:
        final_result, log = drive_workflow(
            backend=backend,
            wf=args.wf,
            inputs=inputs,
            answer_provider=answer_provider,
        )
        LOGGER.info(
            "spike 完成: done=%s nodes=%d", final_result.done, len(log.nodes)
        )
        log_path = Path(args.log_file) if args.log_file else None
        _dump_log(log, log_path)
    except SentinelLoopExhausted as e:
        # 重入场景的预期出口：fail loud 但 exit code 标识「按设计 fail」。
        LOGGER.warning("spike fail loud（按设计）: %s", e)
        print(f"[spike] SentinelLoopExhausted (expected for reentry): {e}", file=sys.stderr)
        exit_code = 3
    except Exception as e:
        LOGGER.exception("spike 失败")
        print(f"[spike] FAILED: {e.__class__.__name__}: {e}", file=sys.stderr)
        exit_code = 1

    # 诊断：backend 计数（让 Stage 3 harness 复用）
    if hasattr(backend, "spawn_count"):
        LOGGER.info(
            "backend 诊断: spawn_count=%d resume_count=%d task_ids=%s",
            backend.spawn_count, backend.resume_count, backend.spawned_task_ids,  # type: ignore[attr-defined]
        )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
