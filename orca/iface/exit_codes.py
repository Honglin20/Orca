"""exit_codes.py —— Orca 主进程退出码 5 档权威（ADR §4.6 / phase-11-process-lifecycle §3）。

回答「``orca run`` / ``orca mcp`` / ``orca serve`` 退出码语义是什么？」：三壳共用一份
``ExitCode`` 枚举 + 一个纯函数派生（workflow 终态 → 退出码）。CI/CD 据此判断结果。

权威性（铁律 8 / ADR §4.6）：
  - **唯一**对外暴露退出码常量与派生函数的位置。三壳 ``__main__.py`` 入口必须经
    ``exit_for_terminal_status`` / 直接引用 ``ExitCode`` 成员，**禁止**裸 ``sys.exit(N)``。
  - 其他层（exec / run / gates）**不许**自定义退出码——若需新增档，先改本文件
    （加枚举值 + 派生规则，符合 ADR §6 P6 扩展规约）。

5 档契约（SPEC §3.1）：

  =========  ===============  ========================================
  退出码     含义              触发场景
  =========  ===============  ========================================
  ``0``      成功              workflow 走到 ``completed`` 终态
  ``1``      配置 / 编译错     yaml 解析失败 / profile 缺失 / capability 校验
  ``2``      业务失败          workflow 走到 ``failed`` 终态
  ``3``      不确定 / 取消     workflow 走到 ``cancelled`` 终态 / MCP stdin EOF
  ``130``    SIGINT 中断       Ctrl+C 未完成清理就退出（不应常态出现）
  =========  ===============  ========================================

依赖单向：本模块只依赖标准库，不依赖 orca 其他子模块（纯数据 + 纯函数）。
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """三壳共用的退出码枚举（SPEC §3.1 / ADR §4.6）。

    ``IntEnum`` 让成员可直接传 ``sys.exit``：``sys.exit(ExitCode.CANCELLED)``。
    """

    SUCCESS = 0
    CONFIG_ERROR = 1          # yaml/profile/compile/capability 校验失败
    BUSINESS_FAILURE = 2      # workflow failed（agent / gate / terminate failed）
    CANCELLED = 3             # workflow cancelled / MCP stdin EOF / 用户主动 cancel
    SIGINT = 130              # POSIX 约定 128 + SIGINT(2)


# workflow 终态 → ExitCode 派生表（纯数据，由 exit_for_terminal_status 消费）。
_STATUS_TO_EXIT: dict[str, ExitCode] = {
    "completed": ExitCode.SUCCESS,
    "failed": ExitCode.BUSINESS_FAILURE,
    "cancelled": ExitCode.CANCELLED,
}


def exit_for_terminal_status(status: str) -> int:
    """workflow 终态（``RunState.status``）→ 退出码（SPEC §3.1）。

    纯函数：同样输入同样输出，无副作用。

    Args:
        status: ``RunState.status`` Literal 值（``completed`` / ``failed`` /
            ``cancelled``）。

    Returns:
        对应的 ``ExitCode`` int 值。

    Raises:
        KeyError: 未知 status（fail loud，铁律 12——CI 不应见到未契约化的退出码，
            出现 = 上层 status 派生漏了分支，须立即可见）。
    """
    try:
        return int(_STATUS_TO_EXIT[status])
    except KeyError:
        raise KeyError(
            f"未知 workflow 终态 {status!r}（期望 completed/failed/cancelled）"
        ) from None
