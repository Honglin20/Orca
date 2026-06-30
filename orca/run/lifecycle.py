"""lifecycle.py —— run_id 生成 + workflow 生命周期事件构造（SPEC §4.6 / §5）。

回答「run 怎么开始 / 结束 / 失败？」：``gen_run_id`` 生成全局唯一 run id；
``make_workflow_started/completed/failed`` 构造**给 bus.emit 的 (type, data)**（不预构造
Event —— bus.emit 的签名是 ``emit(type, data, node, session_id)``，seq 由 tape.append
内部分配，预构造的 Event 会被覆盖）。

设计（贴合 bus.emit 真实签名）：
  - 生命周期事件均为 workflow 级（``node=None`` / ``session_id=None``），故 helper 只返回
    ``(type, data)``，由 orchestrator 调 ``bus.emit(type, data)``（后两参默认 None）。
  - ``max_iter`` 解析（SPEC §4.6）：``--max-iter`` > ``inputs.iterations`` > yaml default > 100。

依赖单向：本模块依赖 ``orca.schema``（Workflow）；不依赖 events.bus / exec。
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime

from orca.schema import Workflow

# SPEC §4.6：max_iterations 全局硬上限兜底（所有覆盖源都未指定时用）。
_DEFAULT_MAX_ITER = 100


def gen_run_id(slug: str) -> str:
    """生成 composite run id：``<slug>-<YYYYMMDD-HHMMSS>-<nanoid6>``。

    - ``slug``：来自 workflow 名（小写、去非字母数字），保证人读可识别「这是哪个 wf 的 run」。
    - 时间戳：本地时间（运行者视角），``YYYYMMDD-HHMMSS``。
    - 6 字符 nanoid（``uuid4().hex`` 取前 6）：同秒并发也不撞。

    全局唯一性靠时间戳 + 随机后缀；同 workflow 两次运行 id 必不同（测试覆盖）。
    """
    safe_slug = _slugify(slug) or "run"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    nano = uuid.uuid4().hex[:6]
    return f"{safe_slug}-{ts}-{nano}"


def _slugify(name: str) -> str:
    """workflow 名 → run id slug：小写 + 保 [a-z0-9_] + 其余转 '-' + 去首尾 '-'。

    保 ``_``（``demo_linear`` → ``demo_linear``，比 ``demolinear`` 更可读）；
    空格 / 标点 / 中文等非 ASCII 转 '-'。
    """
    return "".join(
        c if (c.isascii() and (c.isalnum() or c == "_")) else "-"
        for c in name.lower()
    ).strip("-")


def make_workflow_started(
    run_id: str,
    wf: Workflow,
    inputs: dict,
) -> tuple[str, dict]:
    """构造 ``workflow_started`` 的 (type, data)（SPEC §3.4 data payload）。

    data: ``{inputs, node_count, entry, workflow_name, topology}``。

    ``topology``（phase 9c）：静态 DAG 拓扑摘要，让前端 DAG 在第一个事件即能布局
    （无需等 ``route_taken`` 增量拼边）。设计决策：拓扑进 tape（单一真相源），live 和
    历史 run replay 都从事件拿，无第二数据源 / 额外 endpoint。摘要含 node name+kind、
    routes（from→to）、parallel 组（name+branches）—— **非完整 yaml**，保持 payload 小。
    """
    data = {
        "inputs": dict(inputs),
        "node_count": len(wf.nodes),
        "entry": wf.entry,
        "workflow_name": wf.name,
        "topology": _topology_summary(wf),
    }
    return "workflow_started", data


def _topology_summary(wf: Workflow) -> dict:
    """构造紧凑拓扑摘要（phase 9c DAG 布局用）。

    nodes: ``[{name, kind}]``；routes: ``[{from, to, when?}]``（含 node + parallel 组的
    出边，from/to 为节点/组名）；parallel: ``[{name, branches}]``。when 缺省表示兜底路由。
    foreach 的 body 不展开（它是动态并行，运行时才知道分支数；前端按 foreach_started
    事件的 item_count 渲染分支）。
    """
    nodes = [{"name": n.name, "kind": n.kind} for n in wf.nodes]
    routes: list[dict] = []
    for n in wf.nodes:
        for r in n.routes:
            edge: dict = {"from": n.name, "to": r.to}
            if r.when is not None:
                edge["when"] = r.when
            routes.append(edge)
    parallel = [{"name": g.name, "branches": list(g.branches)} for g in wf.parallel]
    for g in wf.parallel:
        for r in g.routes:
            edge = {"from": g.name, "to": r.to}
            if r.when is not None:
                edge["when"] = r.when
            routes.append(edge)
    return {"entry": wf.entry, "nodes": nodes, "routes": routes, "parallel": parallel}


def make_workflow_completed(
    wf: Workflow,
    outputs: dict,
    *,
    elapsed: float,
) -> tuple[str, dict]:
    """构造 ``workflow_completed`` 的 (type, data)（SPEC §3.4）。

    data: ``{elapsed, outputs}``。``outputs`` 为 ``evaluate_outputs`` 求值后的最终输出。
    """
    return "workflow_completed", {"elapsed": elapsed, "outputs": outputs}


def make_workflow_failed(
    error_type: str,
    message: str,
    *,
    node: str | None = None,
) -> tuple[str, dict]:
    """构造 ``workflow_failed`` 的 (type, data)（SPEC §3.4）。

    data: ``{error_type, message, node}``。``node`` = 导致失败的 node（payload）；
    workflow 级失败（如 MaxIterations 在路由前）可 None。
    """
    return "workflow_failed", {
        "error_type": error_type,
        "message": message,
        "node": node,
    }


def resolve_max_iter(wf: Workflow, inputs: dict, *, cli_override: int | None = None) -> int:
    """解析 max_iterations（SPEC §4.6 / §5 优先级）。

    优先级：``--max-iter`` (cli_override) > ``inputs["iterations"]`` > yaml
    ``wf.inputs["iterations"].default`` > 全局兜底 100。

    fail loud（铁律 4）：``inputs["iterations"]`` / yaml default 显式声明却**非法**
    （非数字 / 无法 int 化）→ ``ValueError``（让上层 orchestrator 捕获 → workflow_failed），
    不静默降级 —— 否则用户以为覆盖生效实际没用，是隐性 bug。
    ``cli_override`` 由 argparse ``type=int`` 保证类型合法；programmatic API 传非法值
    同样 raise（int() 自身会抛）。
    """
    if cli_override is not None:
        return int(cli_override)
    if "iterations" in inputs:
        # 显式声明的 iterations 非法 → fail loud（不降级，否则用户感知不到覆盖失效）
        return int(inputs["iterations"])
    declared = wf.inputs.get("iterations")
    if declared is not None and declared.default is not None:
        # yaml default 非法 → 同样 fail loud（schema 已声明却给坏值，是配置错误）
        return int(declared.default)
    return _DEFAULT_MAX_ITER


def now_monotonic() -> float:
    """单调时钟（elapsed 计算用，便于测试 mock）。"""
    return time.monotonic()
