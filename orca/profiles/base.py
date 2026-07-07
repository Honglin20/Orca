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

import logging
import os
import shlex
from dataclasses import dataclass, field
from typing import Callable, Literal

from orca.profiles.capabilities import ProviderCapabilities
from orca.profiles.terminal import RESULT_LINE, TerminalContract
from orca.schema import Event

logger = logging.getLogger(__name__)

# resolve_prompt_channel 合法值集（与 prompt_channel 字段 Literal 同步）。
_VALID_PROMPT_CHANNELS: frozenset[str] = frozenset({"stdin", "argv"})

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

    # ── flags override 通道（镜像 cli_path_env 的 env 注入机制）──
    # 空串 = 无 override 通道（默认）：``resolve_flags()`` 直接返回 ``self.flags``。
    # 非空 = env 变量名（如 ``ORCA_CLAUDE_FLAGS``）；``orca executor set --flags`` 写 config.json，
    # 启动期 ``apply_config_env`` 把 config 注入此 env，``resolve_flags()`` 运行时读。
    # 默认空保既有测试 fake 零改动（向后兼容）。
    flags_env: str = ""

    # ── prompt_channel override 通道（同构 flags_env，2026-07-07 executor CLI 扩展）──
    # 空串 = 无 override 通道（默认）：``resolve_prompt_channel()`` 直接返回 ``self.prompt_channel``。
    # 非空 = env 变量名（如 ``ORCA_OPENCODE_PROMPT_CHANNEL``）；``orca executor set --prompt-channel``
    # 写 config.json，启动期 ``apply_config_env`` 注入此 env，``resolve_prompt_channel()`` 运行时读。
    prompt_channel_env: str = ""

    # ── prompt 形状 ──
    prompt_paradigm: Literal["minimal"] = "minimal"  # 暂只支持 minimal

    def resolve_cli_path(self) -> str:
        """返回实际 binary 路径字符串：env > default，运行时读（canary 切换无需重启）。

        ``ORCA_CLAUDE_CLI=claude-ds-flash orca run ...`` 即可二进制替换，零代码改动。

        返回的是**未拆分的原始字符串**（如 ``"ccr code"``）；shlex 拆分为 argv 是
        exec/ 层（phase 4）的职责，本层只解析路径选择。
        """
        return os.environ.get(self.cli_path_env, self.default_cli_path)

    def resolve_flags(self) -> tuple[str, ...]:
        """返回实际 flags：env > config > default，运行时读（与 ``resolve_cli_path`` 同构）。

        三态（**逐字按 plan Part B 实现**）：
          1. ``flags_env == ""``（无 override 通道，如 project profile 未设此字段）→ ``self.flags``。
          2. ``flags_env`` 显式设（**含空串** = 显式清空 flags，如 ``ORCA_OPENCODE_FLAGS=``）→
             ``tuple(shlex.split(env_value))``。``shlex.split('') == []`` round-trip 安全。
          3. ``flags_env`` 未设（不在 ``os.environ``）→ ``self.flags``（default）。

        优先级 shell env > config（启动期 ``apply_config_env`` 已 ``setdefault`` 进 env）>
        profile default。三态区分「未设 / 显式置空 / 显式置值」。

        依赖单向：只读 ``os.environ``（stdlib），**不** import iface.cli.config——profiles
        是依赖底层，env 注入逻辑在 iface 层（合法 iface→profiles 方向）。
        """
        if not self.flags_env:
            return self.flags
        if self.flags_env in os.environ:
            return tuple(shlex.split(os.environ[self.flags_env]))
        return self.flags

    def resolve_prompt_channel(self) -> Literal["stdin", "argv"]:
        """返回实际 prompt 投递方式：env > config > default，运行时读（与 ``resolve_flags`` 同构）。

        三态（同 ``resolve_flags``）：
          1. ``prompt_channel_env == ""``（无 override 通道）→ ``self.prompt_channel``。
          2. ``prompt_channel_env`` 显式设且 env 值合法（``stdin``/``argv``）→ 该值。
          3. env 值非法（用户手填错）→ warn + 回落 ``self.prompt_channel``（fail loud 但可恢复，
             不让一个坏值挂死整个 spawn）。

        双层校验：``apply_config_env`` 注入前已校验一次；此处在 resolve 层再校验（防 shell 直接
        ``export ORCA_OPENCODE_PROMPT_CHANNEL=garbage`` 绕过注入）。
        """
        if not self.prompt_channel_env:
            return self.prompt_channel
        if self.prompt_channel_env in os.environ:
            val = os.environ[self.prompt_channel_env]
            if val in _VALID_PROMPT_CHANNELS:
                return val  # type: ignore[return-value]
            logger.warning(
                "profile %r 的 prompt_channel env %s=%r 非法（必须 stdin|argv），回落 default %r",
                self.name, self.prompt_channel_env, val, self.prompt_channel,
            )
        return self.prompt_channel
