"""base.py —— CliProfile（命令替换层核心抽象）+ 类型别名。

回答「如何把 executor 名变成可执行的 CLI 调用？」：CliProfile 把 ``executor: <name>``
解析为 binary/flags/env/translator/capabilities。加新后端 = 丢一个 profile 文件，
零 exec/factory/schema/compile 改动（OCP，SPEC §4.1）。

设计（SPEC §4.3）：
  - **frozen dataclass**：profile 是契约，构造后不可变。
  - ``resolve_cli_path()``：env > default，**运行时读**（canary 切换无需重启）。
  - translator / result_extractor 是 callable：phase 3 用 dummy 占位（真实现 phase 4），
    dummy 须类型匹配含 ``session_id`` 的 ``Event``。

命令替换三摩擦层（SPEC §4.6）：env 覆盖 / project profile 文件 / + translator。

依赖单向：本模块依赖 ``orca.profiles.capabilities`` + ``orca.schema``（Event 类型别名），
不依赖 exec/run/compile。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Literal

from orca.profiles.capabilities import ProviderCapabilities
from orca.profiles.terminal import RESULT_LINE, TerminalContract
from orca.schema import Event

# translator：stream-json 一行 → 一批 Event（phase 4 落真实现，phase 3 用 dummy）。
# 入参含 session_id 上下文（translator 把流片段映射成带 session_id 的事件）。
Translator = Callable[[str, str], list[Event]]
# result_extractor：解析 CLI 最终 result 文本 → 产出值（phase 4 落真实现）。
ResultExtractor = Callable[[str], str]


@dataclass(frozen=True)
class CliProfile:
    """单个 CLI backend 的完整描述（命令替换层核心抽象，SPEC §4.3）。

    frozen：构造后不可变（profile 是契约）。所有字段在 builtin profile 或 project
    profile 文件里静态声明；``resolve_cli_path`` 是唯一的运行时读取点。
    """

    # ── 身份 ──
    name: str  # "claude" / "ccr" / "codex"（与 AgentNode.executor 匹配）
    capabilities: ProviderCapabilities  # 能力声明（validate 静态校验用）

    # ── 如何 spawn ──
    cli_path_env: str  # env 变量名，如 "ORCA_CLAUDE_CLI"（env 覆盖，运行时读）
    default_cli_path: str  # 默认 binary，如 "claude" 或 "ccr code"（shlex 拆分）
    flags: tuple[str, ...]  # 固定 argv 片段
    prompt_channel: Literal["stdin", "argv"]  # prompt 投递方式
    mcp_flag_template: str | None  # "--mcp-config {path}" 或 None（不支持 mcp）

    # ── 如何配置环境 ──
    env_overlay_prefixes: tuple[str, ...]  # 透传给子进程的 env 前缀，如 ("ANTHROPIC_", "CLAUDE_")

    # ── 如何解析 ──
    stream_format: Literal["json", "text"]  # stdout 流格式
    translator: Translator  # stream-json line → list[Event]（phase 3 dummy）
    result_extractor: ResultExtractor  # 解析最终 result（phase 3 dummy）

    # ── 如何信号终态（done + 最终答案 + usage + 错误）──
    # result_line：流末尾有终止行（claude/ccr），CLIRunner on_result 回调交终态；
    # events：无终止行，executor 用 RunAccumulator 边流边累积（opencode）。
    # 默认 RESULT_LINE：claude 是基准后端，绝大多数 profile / 测试 helper 都属此模式；
    # events 模式（opencode）显式覆盖。默认值保既有调用零改动（向后兼容）。
    terminal: TerminalContract = field(default_factory=lambda: RESULT_LINE)

    # ── prompt 形状 ──
    prompt_paradigm: Literal["minimal"] = "minimal"  # 暂只支持 minimal

    def resolve_cli_path(self) -> str:
        """返回实际 binary 路径字符串：env > default，运行时读（canary 切换无需重启）。

        ``ORCA_CLAUDE_CLI=claude-ds-flash orca run ...`` 即可二进制替换，零代码改动。

        返回的是**未拆分的原始字符串**（如 ``"ccr code"``）；shlex 拆分为 argv 是
        exec/ 层（phase 4）的职责，本层只解析路径选择。
        """
        return os.environ.get(self.cli_path_env, self.default_cli_path)
