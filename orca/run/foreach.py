"""foreach.py —— 动态并行（运行时数组分批 + Semaphore 限并发 + 聚合）。

回答「foreach 怎么跑？」：运行时求值 ``source`` 得数组，对每个元素 spawn body，
``asyncio.Semaphore(max_concurrent)`` 限并发，全部完成后聚合（SPEC §4.5）。

执行流程（SPEC §4.5）：
  1. ``arr = eval_jinja2(node.source, ctx)``（运行时取数组，如 ``finder.output.candidates``）。
     非数组 / 非可迭代 → fail loud。
  2. ``sem = asyncio.Semaphore(node.max_concurrent)``。
  3. 对每个 ``(idx, item)``：``body_ctx = ctx.with_locals({item_var: item, index_var: idx})``，
     ``make_executor(node.body)`` + ``execute_and_emit``。
  4. ``asyncio.gather(return_exceptions=True)``：全部完成（失败不中断其他）。
  5. 按 ``node.failure_mode`` 决策（共享 ``aggregate.decide_failure``）。
  6. 聚合 ``{outputs: [raw...], errors: {idx: msg}, count}``。

locals 注入：body 模板用 ``{{ item }}`` / ``{{ _index }}`` 裸引用（render._namespace 摊
ctx.locals 到顶层）。这是 phase 4 render.py 注释预示的扩展（``RunContext.with_locals``）。

依赖单向：本模块依赖 ``orca.exec.{factory, render}``（make_executor + render_template）
+ ``orca.run.{executor_adapter, aggregate}``；不依赖 orchestrator。
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
    from orca.schema import ForeachNode

logger = logging.getLogger(__name__)


async def run_foreach(
    node: ForeachNode,
    ctx: RunContext,
    bus: EventBus,
    *,
    agent_tools_server: AgentToolsMcpServer | None = None,
) -> dict[str, Any]:
    """分批并行执行 ``node.body``，返回聚合 dict（{outputs, errors, count, succeeded}）。

    失败：按 ``node.failure_mode`` 决策（共享 ``decide_failure``）。

    phase 11 §5.4：``agent_tools_server`` 透传给 body 的 agent executor（body 是 agent 时
    可用 ask_user）。None == 既有行为（向后兼容）。
    """
    # 1. 运行时求值 source（Jinja2 渲染 source 表达式 → 数组）
    arr = _eval_source_array(node.source, ctx)

    # max(1, ...) 是 defense-in-depth：compile 层（validator._check_foreach_source）
    # 已校验 max_concurrent >= 1，但程序化 API 直接构造 ForeachNode 喂 run_foreach 可
    # 绕过 compile —— 此处兜底防止 Semaphore(0) 触发永久阻塞 RuntimeError。
    sem = asyncio.Semaphore(max(1, node.max_concurrent))

    async def run_one(idx: int, item: Any) -> Any:
        async with sem:
            body_ctx = ctx.with_locals({
                node.item_var: item,
                node.index_var: idx,
            })
            # lazy import：便于测试 monkeypatch orca.exec.factory.make_executor 统一生效
            from orca.exec.factory import make_executor

            executor = make_executor(node.body, agent_tools_server)
            return await execute_and_emit(executor, node.body, body_ctx, bus)

    raw_results = await asyncio.gather(
        *[run_one(i, x) for i, x in enumerate(arr)],
        return_exceptions=True,
    )

    return _aggregate_foreach(raw_results, node, len(arr))


def _eval_source_array(source: str, ctx: RunContext) -> list[Any]:
    """求值 ``source`` 表达式 → 强制 list 化（非 list / 非可迭代 fail loud）。

    ``source`` 如 ``"maker.output.candidates"`` 是点路径，指向 ctx 内某个 list。
    Jinja2 的 ``compile_expression`` 对 dict 的点访问会优先取**属性**（``dict.items``
    是 builtin method，与名为 ``items`` 的 key 冲突）。为正确取 dict 的项，本函数
    **手写点路径遍历**：每跳优先 ``dict[key]``（``__getitem__``），缺失才报错。

    这样 ``maker.output.items`` 取到的是 ``outputs["maker"]["output"]["items"]``（list），
    而非 dict 的 ``items`` 方法。

    支持：
      - 第一段是顶层变量名（``inputs`` / node 名 / locals 注入的变量）
      - 后续段是 dict key 或对象属性（dict 优先 key）
      - 末值非 list/tuple/set → fail loud
    """
    parts = source.split(".")
    if not parts or not parts[0]:
        raise ValueError(f"foreach source {source!r} 是空路径")

    # 顶层命名空间：inputs + outputs（node 名）+ locals（foreach 嵌套时）
    ns: dict[str, Any] = {"inputs": dict(ctx.inputs)}
    ns.update(ctx.outputs)
    ns.update(ctx.locals)

    head = parts[0]
    if head not in ns:
        raise ValueError(
            f"foreach source {source!r} 顶层变量 {head!r} 未定义（可用：{sorted(ns)}）"
        )
    value: Any = ns[head]
    for seg in parts[1:]:
        value = _step(value, seg, source)

    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    if value is None:
        raise ValueError(f"foreach source {source!r} 求值为 None（期望数组）")
    # SetNode 的 values 经 Jinja2 渲染成 str（如 "[1,2,3]"），此处尝试 JSON 解析回 list。
    # 支持 source 指向 set 节点产出的「字符串化数组」场景（demo_foreach 即此）。
    if isinstance(value, str):
        parsed = _try_parse_list_str(value)
        if parsed is not None:
            return parsed
    raise ValueError(
        f"foreach source {source!r} 求值非数组（得到 {type(value).__name__}）：{value!r}"
    )


def _try_parse_list_str(s: str) -> list[Any] | None:
    """尝试把 ``"[1,2,3]"`` / ``"['a','b']"`` 等 JSON 字符串解析为 list。

    失败（非 JSON / 非 list）返回 None（不 fail loud，让上层报「非数组」）。
    """
    import json

    stripped = s.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _step(obj: Any, seg: str, source: str) -> Any:
    """点路径单跳：dict 优先 key（避开 builtin method 冲突），否则属性，再否则 fail loud。"""
    # dict 优先 __getitem__（避免 dict.items 等 builtin method 与 key 名冲突）
    if isinstance(obj, dict):
        if seg in obj:
            return obj[seg]
        raise ValueError(
            f"foreach source {source!r}：dict {list(obj.keys())} 无 key {seg!r}"
        )
    # 非 dict：试属性 → 试 item
    attr = getattr(obj, seg, _MISSING)
    if attr is not _MISSING:
        return attr
    try:
        return obj[seg]  # type: ignore[index]
    except (TypeError, KeyError, IndexError):
        raise ValueError(
            f"foreach source {source!r}：{type(obj).__name__} 无属性/key {seg!r}"
        )


_MISSING: Any = object()


def _aggregate_foreach(
    raw_results: list[Any],
    node: ForeachNode,
    total: int,
) -> dict[str, Any]:
    """聚合 foreach 结果 → ``{outputs: [raw...], errors: {idx: msg}, count}``。

    失败项按 failure_mode 决策（共享 ``decide_failure``）。
    outputs 是有序 list（按 idx），失败项占位 None（保持索引对齐）。
    """
    outputs: list[Any] = [None] * total
    errors: dict[int, str] = {}
    failures: list[tuple[int, Exception]] = []

    for idx, item in enumerate(raw_results):
        if isinstance(item, Exception):
            failures.append((idx, item))
            errors[idx] = str(item)
            continue
        outputs[idx] = item  # raw output（未包装 —— foreach 聚合在 output.outputs 下）

    success_count = total - len(failures)
    aggregated = {
        "outputs": outputs,
        "errors": errors,
        "count": total,
        "succeeded": success_count,
    }

    decision = decide_failure(
        failures, success_count, total, node.failure_mode,
        group_name=node.name or "<foreach>", aggregated=aggregated,
    )
    if decision is not None:
        raise decision.exception
    # 返回 raw aggregated（orchestrator 统一包 {"output": raw}，不在此处重复包装）
    return aggregated
