"""parallel.py —— 静态并行组执行（asyncio.gather + failure_mode + 幂等跳过）。

回答「parallel 组怎么跑？」：所有 branches 并行（``asyncio.gather``），等全部完成才
推进（SPEC §1.4 / §4.4）。已执行的 branch 跳过（幂等，反「researcher_a 被执行两次」）。

执行流程（SPEC §4.4）：
  1. 对每个 branch：若已在 ``ctx.outputs``（如作为 entry 跑过）→ 跳过用旧结果（幂等）。
  2. 否则 ``make_executor(branch_node)`` + ``execute_and_emit`` 拿 raw output。
  3. ``asyncio.gather(return_exceptions=True)``：等全部完成（不等就推进 = 歧义）。
  4. 按 ``failure_mode`` 决策（共享 ``aggregate.decide_failure``）。
  5. 聚合输出 ``{outputs: {branch: {"output": raw}}, errors: {branch: msg}, count}``。

聚合输出形状：与普通 node 一致存 ``{"output": raw}``（render 约定），下游模板用
``{{ group.output.outputs.branch_a.x }}``（聚合在 ``output`` 键下）。

依赖单向：本模块依赖 ``orca.exec.factory``（make_executor）+ ``orca.run.executor_adapter``
+ ``orca.run.aggregate``；不依赖 orchestrator（被 orchestrator 调用）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from orca.run.aggregate import decide_failure
from orca.run.executor_adapter import execute_and_emit

if TYPE_CHECKING:
    from orca.events.bus import EventBus
    from orca.exec.context import RunContext
    from orca.exec.mcp_tools.server import AgentToolsMcpServer
    from orca.schema import ParallelGroup, Workflow

logger = logging.getLogger(__name__)


async def run_parallel_group(
    group: ParallelGroup,
    ctx: RunContext,
    bus: EventBus,
    wf: Workflow,
    *,
    agent_tools_server: AgentToolsMcpServer | None = None,
) -> dict[str, Any]:
    """并行执行 ``group.branches``，返回**raw 聚合 dict**（orchestrator 统一包
    ``{"output": raw}``，本函数不重复包装）。

    返回形状：``{outputs: {branch: raw_output}, errors: {}, count, succeeded}``。

    幂等：branch 已在 ``ctx.outputs`` 则跳过（用已有 ``{"output": raw}`` 包装结果）。
    失败：按 ``group.failure_mode`` 决策（共享 ``decide_failure``）；抛 ``GroupFailure``。

    phase 11 §5.4：``agent_tools_server`` 透传给 branch 的 agent executor（branch 是 agent
    时可用 ask_user）。None == 既有行为（向后兼容）。
    """
    node_by_name = {n.name: n for n in wf.nodes}

    async def run_one(branch_name: str) -> tuple[str, Any]:
        """返回 (branch_name, result)。失败时 result 是 Exception（不抛，保 branch 名）。

        gather(return_exceptions=True) 无法保留抛错协程的入参 branch 名，故 run_one
        自己吞异常返回 (name, exc)，由 _aggregate_parallel 统一分类。
        """
        # 幂等：已执行（如 entry=researcher_a 已跑，组里再遇则跳过）
        if branch_name in ctx.outputs:
            logger.debug("parallel 组 %s: branch %s 已执行，跳过（幂等）", group.name, branch_name)
            return branch_name, ctx.outputs[branch_name]
        node = node_by_name.get(branch_name)
        if node is None:
            # compile 层已校验 branches ∈ nodes，到这里是 schema 漏校验 → fail loud
            return branch_name, ValueError(
                f"parallel 组 {group.name!r} 的 branch {branch_name!r} 不在 nodes 中"
            )
        # lazy import：便于测试 monkeypatch orca.exec.factory.make_executor 统一生效
        from orca.exec.factory import make_executor

        try:
            executor = make_executor(node, agent_tools_server)
            raw = await execute_and_emit(executor, node, ctx, bus)
            # 包装成 {"output": raw}（render 约定 + 与 ctx.outputs 同形）
            return branch_name, {"output": raw}
        except Exception as e:
            # 不抛 —— 保 branch 名进 failures，由 failure_mode 决策
            return branch_name, e

    # gather 全部完成（run_one 内部吞异常，故 return_exceptions 实际不触发，但保留防御）
    raw_results = await asyncio.gather(
        *[run_one(b) for b in group.branches],
        return_exceptions=True,
    )

    return _aggregate_parallel(raw_results, group)


def _aggregate_parallel(
    raw_results: list[Any],
    group: ParallelGroup,
) -> dict[str, Any]:
    """聚合并行结果 → ``{outputs: {branch: raw}, errors: {branch: msg}, count}``。

    失败项按 failure_mode 决策（共享 ``decide_failure``）。
    每项是 ``(branch_name, {"output": raw} | Exception)``（run_one 已吞异常保 name）。
    """
    outputs: dict[str, Any] = {}
    errors: dict[str, str] = {}
    failures: list[tuple[str, Exception]] = []

    for item in raw_results:
        if isinstance(item, Exception):
            # gather 层面的异常（run_one 自身崩，理论不可达 —— 已 try/except）—— 用占位 key
            failures.append((f"<branch#{len(failures)}>", item))
            continue
        branch_name, result = item
        if isinstance(result, Exception):
            failures.append((branch_name, result))
            continue
        # result 是 {"output": raw}（run_one 包装），剥壳存 raw 到 outputs[branch]
        outputs[branch_name] = result["output"]

    success_count = len(outputs)
    total = len(group.branches)
    # 始终把失败细节填进 errors（部分成功时也要可见，不能只在不抛时填）
    for key, exc in failures:
        errors[key] = str(exc)
    aggregated = {
        "outputs": dict(outputs),  # 已是 raw output（_aggregate_parallel 上面剥了 {"output": raw} 外壳）
        "errors": errors,
        "count": total,
        "succeeded": success_count,
    }

    decision = decide_failure(
        failures, success_count, total, group.failure_mode,
        group_name=group.name, aggregated=aggregated,
    )
    if decision is not None:
        raise decision.exception
    # 返回 raw aggregated（orchestrator 统一包 {"output": raw}，不在此处重复包装）
    return aggregated
