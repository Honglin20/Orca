"""capabilities.py —— ProviderCapabilities（backend 能力声明，frozen pydantic）。

回答「这个 backend 支持什么？」：纯能力声明，``profiles/validate.py`` 静态校验用它
在 spawn 前就 fail loud 拒绝不兼容组合（如 ``output_schema: {...}`` + 不支持结构化输出）。

设计（SPEC §4.4，借自 Conductor ``get_capabilities`` —— 不实例化就能查）：
  - **frozen + extra="forbid"**：构造后不可变、未知字段拒绝（profile 是契约，不能漂移）。
  - 7 个能力字段：mcp_tools / streaming_events / structured_output / interrupt /
    checkpoint_resume / usage_tracking / concurrent_safe。

依赖单向：本模块只依赖 pydantic，不依赖 schema/run/exec/compile。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ProviderCapabilities(BaseModel):
    """backend 能力声明。frozen + extra="forbid"（SPEC §4.4）。

    - ``mcp_tools``：是否支持 ``--mcp-config``（mcp 配置落地后启用该校验）。
    - ``streaming_events``：是否产出结构化流事件（False → validate 报 warning，前端 live 降级）。
    - ``structured_output``：结构化输出支持程度。``"none"`` + 节点声明 ``output_schema``
      → validate 报 error（backend 不支持却用了）。
    - ``interrupt``：是否支持中途打断。
    - ``checkpoint_resume``：是否支持 session resume。
    - ``usage_tracking``：是否产出 token/cost。
    - ``concurrent_safe``：是否可并行 spawn（foreach/parallel body 需要，False → validate 报 error）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mcp_tools: bool
    streaming_events: bool
    structured_output: Literal["native", "prompt_injection", "none"]
    interrupt: bool
    checkpoint_resume: bool
    usage_tracking: bool
    concurrent_safe: bool
