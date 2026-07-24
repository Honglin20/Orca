"""mock_backend.py —— 确定性的子 agent 后端（spike 主路径）。

**为什么需要 mock**：真实 claude 后端（claude -p subprocess）依赖 API key、慢、输出非
确定；而 driver 的核心逻辑（哨兵检测 → task_id 捕获 → resume → orca next）是确定性
控制流，应该用确定性 mock 覆盖（铁律 5：deterministic 逻辑用代码，不靠模型）。

**Mock 的语义**：把子 agent 的「行为剧本」外化成 ``scenario: list[str]``——
scenario[0] 是 spawn 的 output，scenario[1..] 是第 1.. 次 resume 的 output。每个
task_id 独立维护调用计数（多节点 / 多 task 不互相干扰）。

**测试友好**：每次 ``spawn`` / ``resume`` 把 ``spawned`` / ``resumed`` 列表记下，
断言可证明「task_id 捕获对了、resume 调的是同一 id、调了 N 次」。

依赖单向：仅依赖本目录的 ``backend``。
"""

from __future__ import annotations

from tests.spike_ask_user.backend import SubagentBackend, SubagentResult


class ScenarioExhausted(RuntimeError):
    """scenario 用尽（调用次数超过预设）。fail loud，不静默循环。"""


class MockSubagentBackend(SubagentBackend):
    """确定性 mock 后端。

    ``scenario`` 是一个**全局时序脚本**——``scenario[i]`` 是 driver 第 ``i`` 次
    调用（spawn 或 resume，跨 task_id）的 output。这样多节点 workflow 的「A.spawn
    → A.resume → B.spawn → ...」可以一眼写成一个扁平列表，最符合测试作者直觉。

    例：

    >>> scenario = [
    ...     sentinel_msg,        # A.spawn
    ...     real_a_output,       # A.resume
    ...     real_b_output,       # B.spawn（B 不缺数据，不 resume）
    ... ]
    >>> backend = MockSubagentBackend(scenario)

    每次调用都会从 scenario 取下一个 output；用尽 → ``ScenarioExhausted`` fail loud
    （让测试明确发现「driver 多调了 N 次」的 bug，而不是静默返回 None）。

    诊断字段：
    - ``spawned_task_ids`` / ``resumed_task_ids``：spawn / resume 的 task_id 时序记录。
    - ``calls_per_task()``：每个 task_id 被调了几次（让测试断言「A 的 task_id 被调 2 次」）。
    - ``total_calls()``：总调用次数。
    """

    def __init__(self, scenario: list[str], *, backend_name: str = "mock") -> None:
        super().__init__()
        if not scenario:
            raise ValueError("scenario 不能为空（至少要给出 spawn 的 output）")
        self._scenario: tuple[str, ...] = tuple(scenario)
        self.name = backend_name  # 覆盖 ABC 类属性，per-instance 可命名
        # 全局时序 cursor：scenario[global_call_index] 是下次要返的 output
        self._global_call_index: int = 0
        # SubagentResult.call_index 是 per-task 视角，独立于 global cursor
        self._next_id: int = 0

    def spawn(self, prompt: str) -> SubagentResult:
        task_id = f"mock-task-{self._next_id:04d}"
        self._next_id += 1
        self._record_spawn(task_id)
        return self._dispatch(task_id, prompt, is_spawn=True)

    def resume(self, task_id: str, message: str) -> SubagentResult:
        if not self._task_known(task_id):
            # SPEC §2 抽象契约：unknown task_id → fail loud。
            raise KeyError(
                f"resume 收到 unknown task_id={task_id!r}；"
                f"已 spawn 的 task_ids={self.spawned_task_ids}"
            )
        self._record_resume(task_id)
        return self._dispatch(task_id, message, is_spawn=False)

    def _dispatch(self, task_id: str, prompt_or_message: str, *, is_spawn: bool) -> SubagentResult:
        # 全局时序取 scenario[_global_call_index]
        global_index = self._global_call_index
        if global_index >= len(self._scenario):
            raise ScenarioExhausted(
                f"scenario 全局调用 #{global_index} 超出 scenario 长度 "
                f"{len(self._scenario)}；scenario 全部 output 已耗尽。"
                f"（这是 fail loud——driver 应在更早处中断，例如哨兵 MAX_ASK；"
                f"或测试 scenario 漏写了 output）"
            )
        output = self._scenario[global_index]
        # 调 ABC 的 _record_call（维护 calls_per_task / total_calls）
        self._record_call(task_id)
        # SubagentResult.call_index 给一个 per-task 视角（与 ABC._calls_per_task 对齐）
        task_local_count = self._calls_per_task[task_id] - 1  # 已 +1，回退到本次索引
        # 推进全局 cursor
        self._global_call_index = global_index + 1
        return SubagentResult(
            output=output,
            task_id=task_id,
            call_index=task_local_count,
            backend_specific={
                "backend": self.name,
                "input_preview": prompt_or_message[:120],
                "is_spawn": is_spawn,
                "global_call_index": global_index,
            },
        )

    # 覆盖 ABC 的 total_calls：mock 后端的「真实总调用数」=global_call_index
    # （=sum(_calls_per_task.values()) 也成立，但 global_call_index 更直观且语义对齐 scenario）
    def total_calls(self) -> int:
        return self._global_call_index
