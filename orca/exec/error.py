"""error.py —— ExecError（含 phase / error_type / message）+ 错误→事件映射辅助。

回答「executor 失败时怎么 fail loud？」：所有失败路径 raise ``ExecError(phase=...)``，
executor 捕获后 emit ``node_failed`` + ``error`` 双事件（SPEC §6 / 铁律 4）。

phase 取值（SPEC §6 错误映射表，有序互斥判定 §2.4）：
  - ``timeout``       → ``ExecTimeout``        ``proc.wait()`` 超时
  - ``spawn``         → ``CliExitNonZero``     exit_code != 0
  - ``stream``        → ``ClaudeStreamError``  result.is_error == true
  - ``result_parse``  → ``NoResultEvent``      exit 0 但无 result 事件
  - ``schema``        → ``SchemaValidationError`` 结构化提取 / schema 校验失败
  - ``render``        → ``RenderError``        Jinja2 渲染失败

``json_decode`` 是例外（非 fail loud）：claude 偶发非 JSON 心跳行，debug log + 跳过，
不进入此错误体系。
"""

from __future__ import annotations


class ExecError(Exception):
    """executor 失败的统一异常（SPEC §6）。

    携带四类诊断维度：
      - ``phase``：错误阶段（6 选 1，见模块 docstring），驱动 ``error_type`` 映射。
      - ``error_type``：机器可读的错误类别（``phase_to_error_type`` 派生）。
      - ``message``：人读错误描述。
      - ``node``：导致失败的 node 名（可选；executor 自身不知「在哪个 node 跑」——
        由上层 adapter / orchestrator 在桥接时注入，便于 ``workflow_failed.data.node``
        精确定位失败位置，SPEC §3.4）。

    executor 捕获后 emit ``node_failed``（给状态机）+ ``error``（给诊断）双事件。
    """

    def __init__(
        self,
        phase: str,
        message: str,
        error_type: str | None = None,
        *,
        node: str | None = None,
    ) -> None:
        self.phase = phase
        self.message = message
        # error_type 默认由 phase 派生；显式传入可覆盖（如 stream 附 api_error_status）。
        self.error_type = error_type if error_type is not None else phase_to_error_type(phase)
        self.node = node  # 失败 node 名（adapter 注入；None = executor 内部异常未关联 node）
        super().__init__(f"[{self.phase}] {self.message}")

    @classmethod
    def from_failed_data(cls, err_data: dict, *, node: str | None = None) -> ExecError:
        """从 ``node_failed`` 事件的 data 构造 ExecError（DRY 单点）。

        ``executor_adapter.execute_and_emit`` 与 ``run.retry.execute_with_retry`` 都从
        ``node_failed.data`` 透传 ``phase`` / ``error_type`` / ``message`` 构造 ExecError ——
        本方法是这两处（及未来 wave 3 validator）共享的唯一构造点，避免逻辑漂移。

        Args:
            err_data: ``node_failed`` 事件的 data dict（含 error_type / message / phase）。
                缺字段用合理 default（phase="node_failed"，message="executor 产出
                node_failed（无消息）"），error_type=None 走 phase 派生。
            node: 失败 node 名（adapter / retry loop 注入；None = 未关联）。
        """
        return cls(
            phase=err_data.get("phase", "node_failed"),
            message=err_data.get("message", "executor 产出 node_failed（无消息）"),
            error_type=err_data.get("error_type"),
            node=node,
        )


# phase → error_type 映射表（SPEC §6）。新增 phase 在此补一行即可（OCP 局部扩展）。
_PHASE_TO_ERROR_TYPE: dict[str, str] = {
    "timeout": "ExecTimeout",
    "spawn": "CliExitNonZero",
    "stream": "ClaudeStreamError",
    "result_parse": "NoResultEvent",
    "schema": "SchemaValidationError",
    "render": "RenderError",
}


def phase_to_error_type(phase: str) -> str:
    """phase → error_type 映射（SPEC §6）。

    未知 phase 抛 ``ValueError``（fail loud：映射表漏补是 bug，不应静默兜底）。
    """
    try:
        return _PHASE_TO_ERROR_TYPE[phase]
    except KeyError:
        raise ValueError(
            f"未知 error phase {phase!r}（合法：{sorted(_PHASE_TO_ERROR_TYPE)}）"
        ) from None
