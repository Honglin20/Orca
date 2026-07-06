"""errors.py —— 编排层异常（ExecError 子类 + WorkflowTerminated 独立 signal）。

phase-11 SPEC v2.1 §4.2 / ADR §4.1 决策 1.2：编排 exception 归类如下：

  | exception           | 处置                       | kind                |
  |---------------------|----------------------------|---------------------|
  | WorkflowAborted     | ExecError 子类             | BUSINESS_GATE       |
  | MaxIterationsError  | ExecError 子类             | BUSINESS_CONFIG     |
  | RouteError          | ExecError 子类             | BUSINESS_CONFIG     |
  | WorkflowTerminated  | 保留独立（非 ExecError 子类） | —                  |

WorkflowTerminated 为什么保留独立（闭环审视 I7/B10）：它可 ``status="success"``
（orchestrator success 路径 emit workflow_completed），是控制流 signal 不是 error。
若归 ExecError，success 路径会 raise 一个 BUSINESS_AGENT error 被 ``except ExecError``
误捕获误日志。

**子类构造器契约**（v2.1 闭环审视 Q5）：固定 ``(kind, phase)`` 元组，调用方不能覆盖：
  - WorkflowAborted     → (BUSINESS_GATE,    "interrupted")
  - MaxIterationsError  → (BUSINESS_CONFIG,  "max_iterations")
  - RouteError          → (BUSINESS_CONFIG,  "route_deadlock")

子类保留各自额外诊断字段（MaxIter.max_iter / RouteError.node+output / WorkflowAborted.node）。

依赖单向：本模块依赖 ``orca.exec.error``（ExecError），不依赖 schema/events/iface。
"""

from __future__ import annotations

from typing import Any

from orca.exec.error import ExecError
from orca.exec.error_kinds import ErrorKind


class MaxIterationsError(ExecError):
    """主循环超过 ``max_iter`` 仍到不了 ``$end``（SPEC §4.6 / 铁律 4 fail loud）。

    固定 kind=``BUSINESS_CONFIG``，phase=``max_iterations``（编排层 phase，仅诊断）。
    上抛 → orchestrator 捕获 → emit workflow_failed{kind: business_config}。

    保留语义字段 ``max_iter`` / ``current``（诊断用）。
    """

    def __init__(self, max_iter: int, *, current: str | None = None):
        self.max_iter = max_iter
        self.current = current  # 卡在哪个 node（诊断用）
        super().__init__(
            phase="max_iterations",
            message=(
                f"主循环超过 max_iter={max_iter} 仍未到 $end"
                + (f"（卡在 {current}）" if current else "")
            ),
            kind=ErrorKind.BUSINESS_CONFIG,
            node=current,  # 失败 node 名（adapter 注入；orchestrator _error_node 读 e.node）
        )


class WorkflowAborted(ExecError):
    """用户 Ctrl+G + ABORT 中止 workflow（phase 11 SPEC §3 / 铁律 4 fail loud）。

    固定 kind=``BUSINESS_GATE``，phase=``interrupted``。
    上抛 → orchestrator 捕获 → emit workflow_failed{kind: business_gate}。

    ``node`` 是用户 abort 时正在跑的 node（诊断用，SPEC §3.4 node 字段）。
    """

    def __init__(self, node: str | None = None):
        self.node = node
        super().__init__(
            phase="interrupted",
            message=(
                "workflow 被用户中止（Ctrl+G + ABORT）"
                + (f"（node={node}）" if node else "")
            ),
            kind=ErrorKind.BUSINESS_GATE,
            node=node,
        )


class WorkflowTerminated(Exception):
    """触达 ``TerminateNode`` 的业务级终止信号（terminate step，**保留独立**）。

    由 orchestrator ``_drive_from`` 在 ``_dispatch`` 完成后、route 求值前检测到
    ``TerminateNode`` 时 raise。携带 terminate executor 产出的 ``status`` / ``reason``
    / ``outputs`` / ``node``，由 ``run`` / ``run_from_state`` 捕获并分发：

      - ``status="success"`` → emit ``workflow_completed``
      - ``status="failed"`` → orchestrator 翻译为 ``node_failed{kind=BUSINESS_AGENT}``
        事件（ADR §4.1 决策 1.2 翻译规则）

    **非 ExecError 子类**：success 路径不发 error；failed 路径由 orchestrator 显式翻译
    为 node_failed，不经 ``_classify_error``。
    """

    def __init__(
        self,
        *,
        status: str,
        reason: str,
        outputs: dict[str, Any],
        node: str,
    ):
        self.status = status  # "success" / "failed"
        self.reason = reason  # 渲染后的 reason 字符串
        self.outputs = outputs  # 渲染后的 outputs dict
        self.node = node  # terminate 节点名（workflow_failed.data.node 用）
        super().__init__(
            f"workflow 在 {node!r} 被 terminate（status={status}"
            + (f"，reason={reason!r}" if reason else "")
            + "）"
        )
