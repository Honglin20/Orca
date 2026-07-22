"""tars_loop.py —— TARS skill 行为的 Python 投影（SPEC §2 driver 循环）。

**SPEC §2 原伪代码**::

    output = Task(prompt=node_prompt)
    attempts = 0
    while is_sentinel(output) and attempts < MAX_ASK(=3):
        q = parse_sentinel(output)
        answer = ask_user_host_native(q)
        output = resume_same_subagent(answer)
        attempts += 1
    if is_sentinel(output):
        fail_loud(...)
    orca_next_output(output)

本模块把伪代码映射成 Python：

- ``AnswerProvider``：抽象「问用户」这一步。生产 TARS = AskUserQuestion / 聊天问；
  spike = callable(question) -> str | None（None 表示「用户答不知道」）。
- ``drive_node(backend, prompt, answer_provider)``：单节点哨兵循环 → 返回真实 output。
- ``drive_workflow(backend, wf, inputs, answer_provider)``：bootstrap → 逐节点 drive_node
  → orca next → 直到 done。

**可观测性**：``DriveLog`` 记录每个节点的 spawn / resume / sentinel / answer / final，
让测试和 Stage 3 headless harness 都能拿到「哨兵被触发了几次、task_id 是否复用」的证据。

**依赖单向**：依赖本目录 ``sentinel`` + ``backend`` + ``orca_cli``；不依赖 orca 引擎。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from tests.spike_ask_user.backend import SubagentBackend, SubagentResult
from tests.spike_ask_user.orca_cli import (
    BootstrapResult,
    NextResult,
    bootstrap,
    next_step,
    stop,
)
from tests.spike_ask_user.sentinel import (
    MAX_ASK,
    AskUserQuestion,
    SentinelLoopExhausted,
    is_sentinel,
    looks_fabricated,
    parse_sentinel,
)

logger = logging.getLogger(__name__)


# 「问用户」的抽象。生产 TARS = CC AskUserQuestion / opencode 聊天问；spike = 直接 callable。
# 返回 None 表示「用户答不知道」（SPEC §3 子 agent prompt 里允许的 fail_loud 路径）。
AnswerProvider = Callable[[AskUserQuestion], "str | None"]


class FabricationDetected(RuntimeError):
    """driver 检测到真实 output 含造假痕迹（SPEC §3）。fail loud。"""


@dataclass
class NodeAttemptLog:
    """单次 spawn/resume 的记录（诊断 / 断言用）。"""

    phase: str  # "spawn" | "resume"
    task_id: str
    call_index: int
    is_sentinel: bool
    output_preview: str
    answer: str | None = None  # resume 时给的 answer；spawn 恒 None


@dataclass
class NodeDriveLog:
    """单节点驱动过程的完整记录。"""

    node_prompt_preview: str
    attempts: list[NodeAttemptLog] = field(default_factory=list)
    final_task_id: str = ""
    final_output: str = ""
    sentinel_triggered: int = 0  # 哨兵被触发的次数
    resumed_count: int = 0  # resume 被调的次数
    failed_at_max_ask: bool = False

    def assert_task_id_reused(self) -> None:
        """断言：所有 resume 的 task_id == spawn 的 task_id（SPEC §2「同一子 agent」）。"""
        if not self.attempts:
            return
        spawn_task_id = self.attempts[0].task_id
        for attempt in self.attempts:
            if attempt.task_id != spawn_task_id:
                raise AssertionError(
                    f"task_id 未复用：spawn={spawn_task_id}，"
                    f"但 attempt[{attempt.phase}] task_id={attempt.task_id}"
                )


@dataclass
class WorkflowDriveLog:
    """整个 workflow 驱动过程的记录。"""

    run_id: str = ""
    nodes: list[NodeDriveLog] = field(default_factory=list)
    final_done: bool = False
    final_raw: dict[str, Any] = field(default_factory=dict)


def drive_node(
    backend: SubagentBackend,
    node_prompt: str,
    answer_provider: AnswerProvider,
    *,
    node_name: str = "<unknown>",
) -> tuple[str, NodeDriveLog]:
    """驱动单个 Orca 节点的哨兵循环（SPEC §2）。

    返回 ``(real_output, log)``。real_output 保证不是哨兵（要么是真 output，要么已 raise）。

    fail-loud 路径（皆 raise，不返回）：
    - 连续哨兵 ≥ ``MAX_ASK`` → ``SentinelLoopExhausted``。
    - 最终 output 含造假痕迹 → ``FabricationDetected``。
    - 后端 ``spawn`` / ``resume`` 自身异常会原样冒出。
    """
    log = NodeDriveLog(node_prompt_preview=node_prompt[:160])

    result = backend.spawn(node_prompt)
    log.attempts.append(_log_attempt("spawn", result))
    logger.info(
        "drive-node[%s] spawn task_id=%s is_sentinel=%s",
        node_name, result.task_id, is_sentinel(result.output),
    )

    attempts = 0
    while is_sentinel(result.output):
        log.sentinel_triggered += 1
        if attempts >= MAX_ASK:
            # SPEC §4：连续哨兵 ≥ MAX_ASK → fail loud（不无限循环）。
            log.failed_at_max_ask = True
            raise SentinelLoopExhausted(
                f"node {node_name!r} 已连续问用户 {attempts} 次仍返回哨兵；"
                f"达到 MAX_ASK={MAX_ASK} 上限，放弃。"
            )
        question = parse_sentinel(result.output)  # fail loud if 非哨兵
        answer = answer_provider(question)  # None 表示「用户答不知道」
        resume_msg = _build_resume_message(answer)
        result = backend.resume(result.task_id, resume_msg)
        log.attempts.append(
            _log_attempt("resume", result, answer=answer)
        )
        log.resumed_count += 1
        attempts += 1
        logger.info(
            "drive-node[%s] resume #%d task_id=%s answer=%s is_sentinel=%s",
            node_name, attempts, result.task_id,
            "<None>" if answer is None else answer[:60],
            is_sentinel(result.output),
        )

    # Post-condition：退出 ``while is_sentinel(...)`` 循环后必非哨兵（循环不变式）。
    # 单线程 + is_sentinel 是纯函数 → 此 assert 恒 True；但显式 assert 让「读者明白
    # 此处不再有 raise 路径」+ 守护未来对循环条件的回归改动。
    assert not is_sentinel(result.output), (
        f"node {node_name!r} 退出哨兵循环后 output 仍是哨兵（不变式被破坏）；"
        f"attempts={attempts}"
    )

    # SPEC §3：真实 output 不应含造假痕迹（最后一道 sanity check）
    if looks_fabricated(result.output):
        raise FabricationDetected(
            f"node {node_name!r} 真实 output 含造假痕迹（torch.randn/fake_data/...）；"
            f"output_preview={result.output[:200]!r}"
        )

    log.final_task_id = result.task_id
    log.final_output = result.output
    return result.output, log


def _log_attempt(
    phase: str, result: SubagentResult, *, answer: str | None = None
) -> NodeAttemptLog:
    return NodeAttemptLog(
        phase=phase,
        task_id=result.task_id,
        call_index=result.call_index,
        is_sentinel=is_sentinel(result.output),
        output_preview=result.output[:160],
        answer=answer,
    )


def _build_resume_message(answer: str | None) -> str:
    """构造 SendMessage/Task 的恢复消息（SPEC §2「answer + 继续」）。

    SPEC §3 允许「用户也答不出 → 子 agent fail_loud」；answer=None 时我们把
    「用户答不知道」传给子 agent，让它再问一次或返回 fail_loud。driver 侧 MAX_ASK
    兜底，不会无限循环。
    """
    if answer is None:
        return (
            "用户答：不知道。请重新审视：要么再次以哨兵返回更精确的问题，"
            "要么若确实无法获取，返回 {\"_status\":\"fail_loud\",\"reason\":\"...\"}。"
        )
    return (
        f"用户答案：{answer}\n"
        "请基于此答案继续，不要重做已完成的工作。"
    )


class WorkflowDriverProtocol(Protocol):
    """drive_workflow 的依赖注入接口（让测试可注入 fake）。"""

    def bootstrap(
        self, wf: str, inputs: dict[str, Any] | None
    ) -> BootstrapResult: ...

    def next_step(
        self, run_id: str, output: str
    ) -> NextResult: ...

    def stop(self, run_id: str) -> dict[str, Any]: ...


class RealOrcaCLI:
    """真 ``orca`` CLI 的 thin adapter（实现 WorkflowDriverProtocol）。"""

    def __init__(self, orca_bin: str = "orca", inputs: dict[str, Any] | None = None):
        self._orca_bin = orca_bin
        self._inputs = inputs

    def bootstrap(self, wf: str, inputs: dict[str, Any] | None) -> BootstrapResult:
        return bootstrap(wf=wf, inputs=inputs, orca_bin=self._orca_bin)

    def next_step(self, run_id: str, output: str) -> NextResult:
        return next_step(
            run_id=run_id, output=output, orca_bin=self._orca_bin,
            inputs=self._inputs,
        )

    def stop(self, run_id: str) -> dict[str, Any]:
        return stop(run_id=run_id, orca_bin=self._orca_bin)


def drive_workflow(
    backend: SubagentBackend,
    wf: str,
    inputs: dict[str, Any] | None,
    answer_provider: AnswerProvider,
    *,
    orca_cli: WorkflowDriverProtocol | None = None,
    stop_on_exit: bool = True,
) -> tuple[NextResult, WorkflowDriveLog]:
    """驱动整个 workflow 闭环：bootstrap → 逐节点 drive_node → 直到 done。

    返回 ``(final_next_result, log)``。final_next_result.done == True。

    任何节点失败（SentinelLoopExhausted / FabricationDetected / 后端异常）→
    调 ``orca stop`` 清理 marker（避免残留半完成 run），然后原样冒出异常。
    """
    if orca_cli is None:
        orca_cli = RealOrcaCLI()

    wf_log = WorkflowDriveLog()
    boot = orca_cli.bootstrap(wf, inputs)
    wf_log.run_id = boot.run_id
    logger.info(
        "drive-workflow bootstrap run_id=%s node=%s prompt_file=%s",
        boot.run_id, boot.node, boot.prompt_file,
    )

    current_prompt = boot.prompt
    current_node = boot.node
    next_result: NextResult | None = None
    try:
        while True:
            real_output, node_log = drive_node(
                backend, current_prompt, answer_provider, node_name=current_node
            )
            node_log.node_prompt_preview = current_prompt[:160]
            wf_log.nodes.append(node_log)

            next_result = orca_cli.next_step(boot.run_id, real_output)
            logger.info(
                "drive-workflow next done=%s busy=%s node=%s",
                next_result.done, next_result.busy, next_result.node,
            )
            if next_result.busy:
                # SPEC §2 驱动协议：busy 不重派子 agent；spike 路径几乎不撞锁。
                # 此处 fail loud——交给上层决策重试（避免静默吞）。
                raise OrcaBusyError(
                    f"orca next busy run_id={boot.run_id}; "
                    f"retry_after_ms={next_result.retry_after_ms}"
                )
            if next_result.done:
                wf_log.final_done = True
                wf_log.final_raw = next_result.raw
                return next_result, wf_log
            current_prompt = next_result.prompt
            current_node = next_result.node
    except Exception:
        # 任何异常都尝试清理 marker（避免下次 bootstrap 因 marker 冲突 fail）。
        # cleanup 失败不吞主异常（``raise`` 一定让上层看见），但用 ``logger.exception``
        # 带完整 stack——cleanup 失败也要可观测，不能仅 %r 一行 summary。
        if stop_on_exit:
            try:
                orca_cli.stop(boot.run_id)
                logger.warning(
                    "drive-workflow 异常退出，已 orca stop 清理 run_id=%s", boot.run_id
                )
            except Exception:
                logger.exception(
                    "drive-workflow orca stop 失败 run_id=%s（主异常仍会冒出）",
                    boot.run_id,
                )
        raise
    # unreachable（while True 内必有 return 或 raise）
    raise RuntimeError("unreachable")


class OrcaBusyError(RuntimeError):
    """SPEC §2 驱动协议：``orca next`` 返回 busy。spike 不自动重试，交给上层。"""
