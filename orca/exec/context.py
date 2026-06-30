"""context.py —— RunContext（节点间数据传递契约）。

回答「执行单个 node 时上下游数据怎么取？」：frozen dataclass，由 phase 5 orchestrator
构造传给 ``executor.exec(node, ctx)``。

字段（SPEC §4.7）：
  - ``inputs``：workflow 输入（``{{ inputs.iterations }}``）
  - ``outputs``：已完成 node 的输出累积（``{node_name: node_output}``）；
    Jinja2 渲染时 ``{{ optimizer.output.structure }}`` 从 ``outputs["optimizer"]`` 取。
  - ``run_id``：当前 run id（透传到事件 / 日志）。

frozen：执行上下文是不可变快照（一个 node 执行期间上游输出不应变）；
orchestrator 在 node 间构造新 RunContext（累加新输出）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RunContext:
    """单 node 执行上下文（SPEC §4.7）。

    frozen：执行期间不可变。orchestrator 在 node 间构造新实例（append 上游输出）。
    """

    inputs: dict[str, Any]  # workflow 输入
    outputs: dict[str, Any]  # {node_name: node_output} 累积 map
    run_id: str  # 当前 run id
