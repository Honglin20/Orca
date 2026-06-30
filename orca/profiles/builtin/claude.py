"""builtin/claude.py —— claude CLI profile（基准 backend）。

translator 真实现：``claude_translator``（在 ``orca/profiles/translators/claude.py``，
**归属 profiles 层**——决策 1，见 docs/releases/2026-06-30-phase4-exec.md）。
``result_extractor`` 暂留 dummy（决策 3：通用 extract_and_validate 在 exec/claude，
ClaudeExecutor 不经 profile.result_extractor；claude 的 per-backend 知识经 CLIRunner
on_result 钩子处理，避免引入新耦合）。

flags 取自 claude -p 调用约定（SPEC §2.1，重写自 AgentHarness 协议事实，不迁移代码）：
  ``-p --output-format stream-json --include-partial-messages --verbose
   --permission-mode auto --bare``

capabilities 全开（claude 是能力最全的基准 backend）。
"""

from __future__ import annotations

from orca.profiles.base import CliProfile
from orca.profiles.capabilities import ProviderCapabilities
from orca.profiles.translators import claude_translator


def _dummy_result_extractor(result_text: str) -> str:
    """占位 result_extractor（决策 3：本轮保持 dummy）。

    ClaudeExecutor 用 ``orca.exec.claude.result_extractor.extract_and_validate``（通用
    JSON 提取 + schema 校验），不经此字段。保留是为了 CliProfile 类型契约完整；不为对称性
    硬接（Simplicity First，避免 translator 之外的耦合）。
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
    translator=claude_translator,
    result_extractor=_dummy_result_extractor,
    prompt_paradigm="minimal",
)
