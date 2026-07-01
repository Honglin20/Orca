"""errors.py —— 编排层异常（MaxIterationsError + WorkflowAborted + 复用 RouteError）。

回答「编排层失败怎么表达？」：四类编排错误，各自分明、不互相吞（CLAUDE.md 报错处理铁律）：
  - ``RouteError``（router.py）：路由死锁（全 when 不匹配且无兜底）。
  - ``MaxIterationsError``（本文件）：主循环超 ``max_iter`` 仍到不了 ``$end``（死循环）。
  - ``WorkflowAborted``（本文件，phase 11 §3）：用户 Ctrl+G + ABORT 中止 workflow。
  - ``ExecError``（exec/error.py）：executor 失败（node_failed / 生命周期违约）。

四类错误均被 orchestrator 捕获 → emit ``workflow_failed``（error_type 区分）。

依赖单向：本模块不依赖任何 orca 子模块（纯异常定义）。
"""

from __future__ import annotations


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

