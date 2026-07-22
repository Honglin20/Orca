"""backend.py —— 子 agent 后端的抽象（SubagentBackend）。

**为什么需要抽象**：SPEC §2 的 driver 逻辑（spawn → 检测哨兵 → resume 同一子 agent →
拿真实 output）跨三种执行场景：

1. **CC in-session**：主 agent 用 Task 工具派子 agent，PostToolUse hook 拿
   ``tool_response.agentId`` 作为 task_id，SendMessage 恢复。**这是生产路径**。
2. **opencode in-session**：Task 返回 ``<task id="ses_xxx">``，恢复 = ``Task(task_id=ses_xxx)``。
3. **headless spike / E2E harness**：从 Python 驱动，没有 CC session 工具——
   用 ``claude -p --session-id`` spawn + ``--resume <id>`` 续跑（最接近的独立 analog），
   或用 Mock 完全确定性地验证 driver 逻辑。

driver 不关心后端具体形态，只要 ``spawn`` / ``resume`` 两个方法 + ``SubagentResult``
携带 task_id（让 driver 能在日志和断言里证明「恢复的是同一子 agent」）。

**诊断字段契约**（driver / 测试 / Stage 3 harness 共享）：本 ABC 内置了一套
``spawn_count`` / ``resume_count`` / ``spawned_task_ids`` / ``resumed_task_ids`` /
``calls_per_task()`` / ``total_calls()`` 计数器，所有后端共享——避免每个子类复制
7 个字段（DRY）。子类只需在 ``spawn`` / ``resume`` 实现里调 ``_record_spawn`` /
``_record_resume`` / ``_record_call`` 即可。

依赖单向：本模块只依赖 stdlib；具体后端（mock / claude）依赖本模块。

扩展点（OCP）：新后端 = 新增 SubagentBackend 子类 + 调三个 ``_record_*`` helper，
driver 与测试零改动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SubagentResult:
    """子 agent 单次执行的产物。

    - ``output``：子 agent 的**最终消息原文**（哨兵 or 真实 output）。driver 用
      :func:`tests.spike_ask_user.sentinel.is_sentinel` 判类型。
    - ``task_id``：**恢复句柄**。CC = ``agentId``（Task 工具返回 / PostToolUse hook
      ``tool_response.agentId``）；opencode = ``ses_xxx``；claude-cli = ``--session-id``；
      mock = 合成 id。driver 把同一 task_id 喂回 ``resume``，证明「恢复的是同一子 agent」。
    - ``backend_specific``：后端自定义诊断字段（usage / cost / latency 等），driver 不解读。
    - ``call_index``：本 task_id 的第几次调用（0=spawn，1+=resume）。让 driver / 测试
      能轻易证明「子 agent 被恢复了 N 次」，不必外部记账。
    """

    output: str
    task_id: str
    call_index: int = 0
    backend_specific: dict[str, Any] = field(default_factory=dict)


class SubagentBackend(ABC):
    """子 agent 后端抽象（SPEC §2 + §6 spike）。

    两个方法覆盖哨兵路径的全部交互：

    - ``spawn(prompt)``：首次派子 agent。返回 ``SubagentResult``，其 ``task_id`` 是
      后续 ``resume`` 的句柄。
    - ``resume(task_id, message)``：**对同一子 agent** 追加用户消息并继续（SPEC §2）。
      ``task_id`` 必须是 ``spawn`` 返回的那个；后端负责保证「同一性」（CC SendMessage
      / opencode Task(task_id=) / claude --resume 都是如此）。

    失败路径 fail loud：实现应在子 agent 崩溃 / 超时 / 不存在 task_id 时 raise，
    而非返回空 output（避免被 driver 误判为「真实 output 是空字符串」）。

    **诊断字段契约**：本 ABC 内置计数器，子类在 ``spawn`` / ``resume`` 实现里调
    ``_record_spawn(task_id)`` / ``_record_resume(task_id)`` 即可自动维护：
    ``spawn_count`` / ``resume_count`` / ``spawned_task_ids`` / ``resumed_task_ids``
    / ``calls_per_task()`` / ``total_calls()``。driver / 测试 / Stage 3 harness
    跨后端一致地读这些字段，避免新后端漏字段导致测试静默通过。
    """

    # 子类可覆盖（类属性即可，无需 @property）
    name: str = "<abstract-backend>"

    def __init__(self) -> None:
        # 诊断计数器（所有后端共享，避免子类复制 7 个字段——DRY）
        self.spawn_count: int = 0
        self.resume_count: int = 0
        self.spawned_task_ids: list[str] = []
        self.resumed_task_ids: list[str] = []
        self._calls_per_task: dict[str, int] = {}

    # ── 子类必须实现 ────────────────────────────────────────────────────────

    @abstractmethod
    def spawn(self, prompt: str) -> SubagentResult:
        """首次派子 agent 跑 ``prompt``；返回最终消息 + task_id。"""

    @abstractmethod
    def resume(self, task_id: str, message: str) -> SubagentResult:
        """对**同一**子 agent（由 task_id 标识）追加 ``message`` 并继续。

        抽象契约：调用方保证 ``task_id`` 来自先前 ``spawn`` 的返回值。实现侧若发现
        unknown task_id，应 fail loud（``KeyError`` / 自定义异常）。
        """

    # ── 诊断 helper（子类 spawn/resume 实现里调） ──────────────────────────

    def _record_spawn(self, task_id: str) -> None:
        """子类 ``spawn`` 调一次：记 spawn_count + spawned_task_ids。"""
        self.spawn_count += 1
        self.spawned_task_ids.append(task_id)

    def _record_resume(self, task_id: str) -> None:
        """子类 ``resume`` 调一次：记 resume_count + resumed_task_ids。"""
        self.resume_count += 1
        self.resumed_task_ids.append(task_id)

    def _record_call(self, task_id: str) -> None:
        """记 per-task 调用次数（spawn + resume 合计）。

        子类每次 spawn/resume 都调一次——让 ``calls_per_task`` / ``total_calls``
        给 driver 测试断言「task 被调 N 次」用。
        """
        self._calls_per_task[task_id] = self._calls_per_task.get(task_id, 0) + 1

    def _task_known(self, task_id: str) -> bool:
        """子类 ``resume`` 用来判 task_id 是否已 ``_record_call`` 过。"""
        return task_id in self._calls_per_task

    # ── 诊断 reader（driver / 测试用） ─────────────────────────────────────

    def calls_per_task(self) -> dict[str, int]:
        """每个 task_id 被调了几次（spawn + resume 总和）。"""
        return dict(self._calls_per_task)

    def total_calls(self) -> int:
        """总调用次数（spawn + resume 全局合计）。"""
        return sum(self._calls_per_task.values())
