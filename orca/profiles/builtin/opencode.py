"""builtin/opencode.py —— opencode CLI profile（events 模式后端，真实二进制 v1.14.22）。

opencode 是与 claude 完全异协议的后端：调用 ``opencode run <prompt> --format json``，
stdout 是逐行 NDJSON（``part`` 信封），**无 result 终止行**。最终答案 = text 事件拼接；
usage 在 step_finish；错误是 error 事件。故走 ``TerminalContract(mode="events")``。

调用约定（实测）：
  - prompt 是**位置参数**（yargs，flag 可在前）：``opencode run <prompt> --format json ...``
  - profile 用 ``prompt_channel="argv"``（走 runner.py:231 既有分支，prompt 进 argv 末尾）。
  - flags = ``("run", "--format", "json", "--dangerously-skip-permissions")``。
  - ``--model <provider/model>`` 经 executor 的 extra_args 注入（与 claude 的 --model 一致路径）。

capabilities 保守（v1 实情）：mcp_tools=False（暂不透传 mcp config）、structured_output=
"prompt_injection"（非 native，靠 prompt 引导）、checkpoint_resume=False、usage_tracking=True
（step_finish 带 tokens/cost）、interrupt=True（SIGINT 友好）、concurrent_safe=True。

result_extractor 仍用 dummy：events 模式不经 profile.result_extractor（最终答案由
RunAccumulator 累积），保留是为 CliProfile 类型契约完整（同 claude profile 的设计理由）。
"""

from __future__ import annotations

from orca.profiles.base import CliProfile
from orca.profiles.capabilities import ProviderCapabilities
from orca.profiles.terminal import EVENTS
from orca.profiles.translators import opencode_translator


def _dummy_result_extractor(result_text: str) -> str:
    """占位 result_extractor（events 模式不经此字段，RunAccumulator 累积最终答案）。

    保留是为 CliProfile 类型契约完整（同 claude profile）；不为之硬接耦合（Simplicity First）。
    """
    return result_text


PROFILE = CliProfile(
    name="opencode",
    capabilities=ProviderCapabilities(
        mcp_tools=False,
        streaming_events=True,
        structured_output="prompt_injection",
        interrupt=True,
        checkpoint_resume=False,
        usage_tracking=True,
        concurrent_safe=True,
    ),
    cli_path_env="ORCA_OPENCODE_CLI",
    default_cli_path="opencode",
    flags=(
        "run",
        "--format",
        "json",
        "--dangerously-skip-permissions",
    ),
    prompt_channel="argv",  # prompt 是位置参数，进 argv 末尾（runner.py:231 既有分支）
    mcp_flag_template=None,  # opencode v1 不透传 mcp config
    env_overlay_prefixes=("OPENCODE_", "ANTHROPIC_"),
    stream_format="json",
    translator=opencode_translator,
    result_extractor=_dummy_result_extractor,
    terminal=EVENTS,
    flags_env="ORCA_OPENCODE_FLAGS",
    prompt_channel_env="ORCA_OPENCODE_PROMPT_CHANNEL",
    prompt_paradigm="minimal",
)
