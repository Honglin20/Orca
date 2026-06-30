"""validate.py —— capability 静态校验（被 compile 调用）。

回答「这个 workflow 的 executor 配置 spawn 前就 fail loud 拒绝不兼容组合吗？」：
``validate_workflow_profiles`` 对每个 agent / foreach-body agent node 检查 executor
profile 存在 + capabilities 与 node 配置兼容。不 spawn、不实例化 backend。

校验规则（SPEC §4.9，**仅基于 AgentNode 真实字段**，不自创字段）：

| # | 条件 | severity |
|---|---|---|
| 1 | ``get_profile(node.executor)`` 抛 ValueError | error | 未知 executor（含被 disable 的）|
| 2 | ``node.output_schema is not None`` 且 ``cap.structured_output == "none"`` | error |
| 3 | foreach ``body`` 是 AgentNode 且 ``cap.concurrent_safe == False`` | error |
| 4 | ``cap.streaming_events == False`` | warning |

mcp_servers 当前不在 schema（AgentNode 无此字段），故 ``mcp_tools`` 校验待 mcp 配置落地后
启用（SPEC §4.9 注释明确）。

依赖单向：本模块只依赖 ``orca.schema`` + ``orca.profiles.registry``，**不依赖 compile**
（compile 单向调它，SPEC §4.9 依赖方向核对）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from orca.profiles.registry import get_profile
from orca.schema import AgentNode, ForeachNode, Workflow


@dataclass(frozen=True)
class ProfileIssue:
    """单个 capability 校验问题。compile 把它汇入 ValidationResult。"""

    node: str
    severity: Literal["error", "warning"]
    message: str


def validate_workflow_profiles(wf: Workflow) -> list[ProfileIssue]:
    """对每个 agent / foreach-body agent node 做 capability 静态校验。

    返回 issue 列表（compile 汇总进 ValidationResult，走 raise_if_errors 聚合）。
    规则仅基于 AgentNode 真实字段（executor / output_schema / foreach body）。
    """
    issues: list[ProfileIssue] = []

    for node in wf.nodes:
        if isinstance(node, AgentNode):
            _check_agent_node(node.name or "<unnamed>", node, issues)
        elif isinstance(node, ForeachNode):
            # foreach body 若是 AgentNode，body 无独立 name（SPEC §2.3 内嵌模板），
            # 以 ``<foreach>.body`` 标识，便于错误定位。
            body = node.body
            if isinstance(body, AgentNode):
                body_label = f"{node.name}.body"
                _check_agent_node(body_label, body, issues)
                # ③ foreach body 并发安全：body 是 AgentNode 且其 executor
                # concurrent_safe==False → error（foreach 并发执行 body）
                _check_foreach_body_concurrent(body_label, body, issues)

    return issues


def _check_agent_node(label: str, node: AgentNode, issues: list[ProfileIssue]) -> None:
    """对单个 agent node 跑规则 ①②④（SPEC §4.9）。

    规则 ③（foreach 并发安全）按 foreach 上下文判，见 ``_check_foreach_body_concurrent``。
    ``label`` 是错误定位用名（顶层 node.name 或 ``<foreach>.body``）。
    """
    # ① get_profile 失败 → error（未知 executor / 被 disable）
    try:
        profile = get_profile(node.executor)
    except ValueError as e:
        issues.append(
            ProfileIssue(
                node=label,
                severity="error",
                message=f"executor '{node.executor}' 不可用：{e}",
            )
        )
        return  # 后续规则依赖 profile，profile 缺失时不再判

    cap = profile.capabilities

    # ② output_schema 声明但 backend 不支持结构化输出 → error
    if node.output_schema is not None and cap.structured_output == "none":
        issues.append(
            ProfileIssue(
                node=label,
                severity="error",
                message=(
                    f"executor '{node.executor}' 不支持结构化输出"
                    f"（structured_output='none'），但 node 声明了 output_schema"
                ),
            )
        )

    # ④ streaming_events=False → warning（前端 live 观测降级，不阻止）
    if not cap.streaming_events:
        issues.append(
            ProfileIssue(
                node=label,
                severity="warning",
                message=(
                    f"executor '{node.executor}' 不产出结构化流事件"
                    f"（streaming_events=False），前端 live 观测将降级"
                ),
            )
        )


def _check_foreach_body_concurrent(
    label: str, body: AgentNode, issues: list[ProfileIssue]
) -> None:
    """规则 ③：foreach body 是 AgentNode 且其 executor concurrent_safe==False → error。

    foreach 并发执行 body，backend 不可并行 spawn 则必然失败（spawn 前就 fail loud）。
    profile 缺失的情况由 ``_check_agent_node`` 规则 ① 已报，此处跳过。
    """
    try:
        profile = get_profile(body.executor)
    except ValueError:
        return  # 规则 ① 已报此 body 的 executor 问题
    if not profile.capabilities.concurrent_safe:
        issues.append(
            ProfileIssue(
                node=label,
                severity="error",
                message=(
                    f"foreach body executor '{body.executor}' 不可并行 spawn"
                    f"（concurrent_safe=False），不能用于 foreach"
                ),
            )
        )
