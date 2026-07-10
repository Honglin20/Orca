"""orca.schema.render_item —— RenderItem 契约（render layer v1）。

回答「工具调用怎么渲染？」：在 canonical Event 之上加一层 **iface 内的渲染抽象**，
由两段纯函数组成（render-layer-design-draft §3）::

    Event ──[tool normalizer]──▶ RenderItem ──[renderer registry]──▶ 像素
            (backend 工具差异归一)   (canonical kind+payload)   (per-end: TUI Rich / Web React)

唯一真相源链（§3.2）：
  - tape（canonical Event）= 运行时唯一真相
  - RenderItem = **派生投影**（pure function），不持久化、不进 tape、不缓存 mutable 状态
  - 本模块（schema）= **规范层唯一真相**（TUI/Web 两端共享的契约）

依赖单向：仅 pydantic + typing，与 ``event.py`` 一致零下游依赖。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

# Canonical 工具渲染类型（render-layer-design-draft §5.1）。
#
# - file_read/file_write/file_edit：文件读写改，diff hunks 结构化
# - shell：终端命令 + 输出块
# - glob/grep：文件名 / 内容搜索
# - unknown：兜底（未知工具走 args JSON 美化 + result 截断预览）
RenderToolKind = Literal[
    "file_read",
    "file_write",
    "file_edit",
    "shell",
    "glob",
    "grep",
    "unknown",
]

# 工具状态（reducer 按 tool_call_id 配对 call/result 时维护）。
#
# - running：tool_call 已到，result 未到
# - completed：tool_result 已到（成功）
# - error：tool_result 标记错误（v1 通过 exit_code!=0 / 异常 result 文本判定；v1 不强求）
# - interrupted：tool_call 无对应 tool_result（agent 被中断，reducer 在终态时回填）
ToolStatus = Literal["running", "completed", "error", "interrupted"]


class RenderItem(BaseModel):
    """canonical 渲染单元。Event 的幂等投影，非真相源（重算必等价，§3.2 / §3.3）。

    payload 按 ``kind`` 分派（见 ``NormalizeError`` / per-kind normalizer）。
    ``raw`` 保留原始 args/result 供「查看原始」调试，**永不参与渲染决策**。

    不可变（``extra="forbid"``）：v1 schema 锁定，新字段走 v2 + 兼容层（§13 风险）。
    """

    model_config = ConfigDict(extra="forbid")

    kind: RenderToolKind
    status: ToolStatus
    title: str                 # 一行摘要（"src/foo.py" / "$ ls -la" / "pattern: *.py"）
    subtitle: str = ""         # 可选副标题（"+12 -3" / "3 matches"）
    payload: dict[str, Any]    # per-kind 结构化字段（§5.2，由 normalizer 填充）
    raw: dict[str, Any]        # 原始 args + result，调试兜底（永不参与渲染决策）


__all__ = ["RenderItem", "RenderToolKind", "ToolStatus"]
