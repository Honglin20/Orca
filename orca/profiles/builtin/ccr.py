"""builtin/ccr.py —— ccr（claude-code-router）profile。

``default_cli_path="ccr code"``（shlex 拆分）。ccr 是 claude 兼容路由器，但能力与 claude
不同：不支持 mcp_tools（路由层不透传 mcp config）、不支持 native 结构化输出（走 prompt
注入）。

**translator 复用 claude_translator**：ccr 自述协议兼容 claude stream-json，故事件映射走
同一套规则（``claude_translator`` 对未知 ``type`` 返回 ``[]``，优雅降级）。result_extractor
仍用 dummy（phase 4 按实情落真实现）。

capabilities 按 ccr 实情（SPEC §4.4 + plan B.4）：mcp_tools=False（路由层限制）、
structured_output="prompt_injection"（非 native）。这使 validate 对 ``ccr + output_schema``
的组合触发 warning 而非 error（prompt_injection 仍能产出结构化输出，只是非原生）。
"""

from __future__ import annotations

from orca.profiles.base import CliProfile
from orca.profiles.capabilities import ProviderCapabilities
from orca.profiles.terminal import RESULT_LINE
from orca.profiles.translators import claude_translator


def _dummy_result_extractor(result_text: str) -> str:
    """占位 result_extractor（phase 4 落真实现）。"""
    return result_text


PROFILE = CliProfile(
    name="ccr",
    capabilities=ProviderCapabilities(
        mcp_tools=False,
        streaming_events=True,
        structured_output="prompt_injection",
        interrupt=True,
        checkpoint_resume=True,
        usage_tracking=True,
        concurrent_safe=True,
    ),
    cli_path_env="ORCA_CCR_CLI",
    default_cli_path="ccr code",
    flags=(
        "-p",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--permission-mode",
        "auto",
    ),
    prompt_channel="stdin",
    mcp_flag_template=None,  # ccr 路由层不透传 mcp config
    env_overlay_prefixes=("ANTHROPIC_", "CLAUDE_", "CCR_"),
    stream_format="json",
    translator=claude_translator,
    result_extractor=_dummy_result_extractor,
    terminal=RESULT_LINE,
    prompt_paradigm="minimal",
)
