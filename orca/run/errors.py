"""errors.py —— 编排层异常（MaxIterationsError + WorkflowAborted + WorkflowTerminated + 复用 RouteError）。

回答「编排层失败怎么表达？」：五类编排错误/信号，各自分明、不互相吞（CLAUDE.md 报错处理铁律）：
  - ``RouteError``（router.py）：路由死锁（全 when 不匹配且无兜底）。
  - ``MaxIterationsError``（本文件）：主循环超 ``max_iter`` 仍到不了 ``$end``（死循环）。
  - ``WorkflowAborted``（本文件，phase 11 §3）：用户 Ctrl+G + ABORT 中止 workflow。
  - ``WorkflowTerminated``（本文件，terminate step）：触达 ``TerminateNode`` —— 业务级
    显式终止（success / failed 两种），由 orchestrator 在 ``_drive_from`` 检测到
    ``kind=terminate`` 时 raise，``run`` / ``run_from_state`` 捕获并据 status 分发
    ``workflow_completed`` 或 ``workflow_failed{WorkflowTerminated}``。
  - ``ExecError``（exec/error.py）：executor 失败（node_failed / 生命周期违约）。

四类错误均被 orchestrator 捕获 → emit ``workflow_failed``（error_type 区分）；
``WorkflowTerminated`` 是例外——它可能成功（status=success），由 ``run`` 据其 ``status``
字段选择 emit ``workflow_completed`` 或 ``workflow_failed``。

依赖单向：本模块不依赖任何 orca 子模块（纯异常定义）。
"""

from __future__ import annotations

from typing import Any


class MaxIterationsError(Exception):
    """主循环超过 ``max_iter`` 仍到不了 ``$end``（SPEC §4.6 / 铁律 4 fail loud）。

    触发：循环 routes 不终止（如 demo_max_iter 的空回环）。
    上抛 → orchestrator 捕获 → emit workflow_failed（error_type=``MaxIterations``）。
    """

    def __init__(self, max_iter: int, *, current: str | None = None):
        self.max_iter = max_iter
        self.current = current  # 卡在哪个 node（诊断用）
        super().__init__(
            f"主循环超过 max_iter={max_iter} 仍未到 $end"
            + (f"（卡在 {current}）" if current else "")
        )


class WorkflowAborted(Exception):
    """用户 Ctrl+G + ABORT 中止 workflow（phase 11 SPEC §3 / 铁律 4 fail loud）。

    触发：``InterruptHandler.resolve(action="abort")`` → orchestrator ``_handle_interrupt``
    收到 ``action="abort"`` → raise 本异常。
    上抛 → orchestrator 捕获 → emit workflow_failed（error_type=``WorkflowAborted``）。

    ``node`` 是用户 abort 时正在跑的 node（诊断用，SPEC §3.4 node 字段）。
    """

    def __init__(self, node: str | None = None):
        self.node = node
        super().__init__(
            "workflow 被用户中止（Ctrl+G + ABORT）"
            + (f"（node={node}）" if node else "")
        )


class WorkflowTerminated(Exception):
    """触达 ``TerminateNode`` 的业务级终止信号（terminate step）。

    由 orchestrator ``_drive_from`` 在 ``_dispatch`` 完成后、route 求值前检测到
    ``TerminateNode`` 时 raise。携带 terminate executor 产出的 ``status`` / ``reason``
    / ``outputs`` / ``node``，由 ``run`` / ``run_from_state`` 捕获并分发：

      - ``status="success"`` → emit ``workflow_completed``，``outputs`` 用 terminate 节点
        的 ``outputs``（渲染后的），**不**走 ``evaluate_outputs(wf.outputs)``。
      - ``status="failed"`` → emit ``workflow_failed``，``error_type="WorkflowTerminated"``，
        ``message=reason``，``node=<terminate node name>``。

    与 ``WorkflowAborted`` 区分：abort 是用户主动中断（控制流信号），terminate 是工作流
    作者显式声明的业务退出点（YAML 里写死的）。两者 error_type 不同，shell 端可据此时
    区分渲染。

    与正常 ``$end`` 终止区分：``$end`` 只能 success；terminate 能显式 failed（典型用途：
    分类器走不到任何 handler 时显式 reject）。
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

