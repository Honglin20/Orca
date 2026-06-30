"""context.py —— RunContext（节点间数据传递契约）。

回答「执行单个 node 时上下游数据怎么取？」：frozen dataclass，由 phase 5 orchestrator
构造传给 ``executor.exec(node, ctx)``。

字段（SPEC §4.7）：
  - ``inputs``：workflow 输入（``{{ inputs.iterations }}``）
  - ``outputs``：已完成 node 的输出累积（``{node_name: {"output": node_output}}``）；
    Jinja2 渲染时 ``{{ optimizer.output.structure }}`` 从 ``outputs["optimizer"]`` 取。
    （注意：存的是 ``{"output": raw}`` 包装，与 render.py ``_namespace`` 约定一致 ——
    模板统一 ``{{ node.output.field }}``。）
  - ``run_id``：当前 run id（透传到事件 / 日志）。
  - ``task``：可选位置参数 task（CLI ``orca run <yaml> <task>`` 语法糖），
    同时注入 ``inputs.task``；保留此字段供 lifecycle 事件 / 日志引用（非必须，默认 None）。
  - ``locals``：foreach body 注入的局部变量（``{{ item }}`` / ``{{ _index }}``）。
    空 dict = 非 foreach 上下文；foreach 时由 orchestrator 经 ``with_locals`` 派生新实例。

frozen：执行上下文是不可变快照（一个 node 执行期间上游输出不应变）；
orchestrator 在 node 间构造新 RunContext（累加新输出）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RunContext:
    """单 node 执行上下文（SPEC §4.7）。

    frozen：执行期间不可变。orchestrator 在 node 间构造新实例（append 上游输出）。

    ``locals`` 默认空 dict（普通 node 执行）；foreach body 经 ``with_locals`` 派生带
    item/index 的新实例，render 的 ``_namespace`` 把 locals 摊到 Jinja2 顶层。
    """

    inputs: dict[str, Any]  # workflow 输入
    outputs: dict[str, Any]  # {node_name: {"output": raw}} 累积 map
    run_id: str  # 当前 run id
    task: str | None = None  # 位置参数 task（同时进 inputs.task；此字段供日志/事件）
    locals: dict[str, Any] = field(default_factory=dict)  # foreach body 局部变量

    def with_locals(self, locals_: dict[str, Any]) -> RunContext:
        """派生带 locals 的新 frozen 实例（foreach body 用，注入 item / _index）。

        不 mutate：返回新 dataclass 实例（frozen 语义）。普通 node 不调用此方法。
        """
        return RunContext(
            inputs=self.inputs,
            outputs=self.outputs,
            run_id=self.run_id,
            task=self.task,
            locals=dict(locals_),  # 拷贝，避免外部 mutate 污染 frozen 快照
        )
