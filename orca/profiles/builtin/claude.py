"""builtin/claude.py —— claude CLI profile（基准 backend）。

phase 3 用 dummy translator / result_extractor 占位（真实现 phase 4 从 AgentHarness
``translator/stream_json.py`` 迁移）。dummy 须类型匹配含 ``session_id`` 的 ``Event``。

flags 取自 AgentHarness claude 调用约定（SPEC §4.5）：
  ``-p --output-format stream-json --include-partial-messages --verbose
   --permission-mode auto --bare``

capabilities 全开（claude 是能力最全的基准 backend）。
"""

from __future__ import annotations

import time

from orca.profiles.base import CliProfile
from orca.profiles.capabilities import ProviderCapabilities
from orca.schema import Event


def _dummy_translator(line: str, session_id: str) -> list[Event]:
    """占位 translator（phase 4 落真实现）。

    类型签名匹配契约：``stream-json 一行 → list[Event]``，Event 含 session_id。
    phase 3 不解析真实 claude stream-json，仅返回空列表（保证类型正确 + 不产出假事件）。
    """
    # phase 4：解析 line 为 claude stream-json，映射成 agent_message/thinking/tool_call 等。
    # 当前仅保证类型匹配，不产出事件（避免假数据污染 tape）。
    _ = (line, session_id)  # 显式标记未使用，防 linter 误报
    return []


def _dummy_result_extractor(result_text: str) -> str:
    """占位 result_extractor（phase 4 落真实现）。

    当前直接返回原文（自由文本输出场景即整段 result）。
    """
    return result_text


PROFILE = CliProfile(
    name="claude",
    capabilities=ProviderCapabilities(
        mcp_tools=True,
        streaming_events=True,
        structured_output="native",
        interrupt=True,
        checkpoint_resume=True,
        usage_tracking=True,
        concurrent_safe=True,
    ),
    cli_path_env="ORCA_CLAUDE_CLI",
    default_cli_path="claude",
    flags=(
        "-p",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--permission-mode",
        "auto",
        "--bare",
    ),
    prompt_channel="stdin",
    mcp_flag_template="--mcp-config {path}",
    env_overlay_prefixes=("ANTHROPIC_", "CLAUDE_"),
    stream_format="json",
    translator=_dummy_translator,
    result_extractor=_dummy_result_extractor,
    prompt_paradigm="minimal",
)
