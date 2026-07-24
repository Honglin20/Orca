"""claude_backend.py —— 真实 claude CLI 后端（``claude -p`` subprocess + ``--resume``）。

**SPEC 映射**：SPEC §2 task_id 捕获（CC 路径）= ``Task`` 工具返回 ``agentId``。本后端是
**headless / 独立 Python** 路径的等价物——它不是 CC in-session 工具，而是用
``claude -p --session-id <uuid>`` spawn、``claude -p --resume <uuid>`` 续跑。

**与 CC SendMessage 的差异（重要文档化）**：

| 维度 | CC in-session SendMessage | 本后端（claude -p --resume） |
|---|---|---|
| 调用方 | 主 agent（CC session 内） | Python driver（独立进程） |
| spawn | Task 工具 → CC 框架 spawn 子 agent | ``claude -p --session-id <uuid>`` subprocess |
| task_id | ``tool_response.agentId``（PostToolUse hook 拿到） | ``--session-id`` 自生成；恢复时用 ``--resume <uuid>`` |
| resume | ``SendMessage(task_id, msg)`` → CC 框架唤醒同 session | ``claude -p --resume <uuid> "<msg>"`` subprocess |
| 上下文保持 | CC session 内自动（同一 JSONL transcript） | claude CLI 自动 load 同一 session transcript |
| 工具权限 | 继承主 session 的 ``allowed-tools`` | 本后端单独传 ``--allowed-tools`` |

**两者捕获的本质一致**：claude CLI 的 session transcript = CC session JSONL；spawn +
resume 复用同一 transcript 就是「同一子 agent 上下文不丢」。SPEC §0 的「恢复同一子 agent」
在 headless 路径用 ``--resume`` 实现等价语义。

**Stage 3 headless TARS E2E harness 的两条路**：
1. 继续用本后端（``claude -p --resume``）——快、独立，能验证 driver 逻辑 + 子 agent
   prompt 是否真能按契约返回哨兵。
2. 跑在 CC in-session 里（driver 本身是个 CC agent）——能验证 Task/SendMessage 原语
   本身；需在 CC 内 spawn Python 子进程或直接让 driver 是个 skill。

本后端对应 (1)；(2) 是 TARS skill 落地后真实跑在 CC 里的形态。

**前置**：
- 本机 ``claude`` CLI 在 PATH（``ORCA_CLAUDE_CLI`` 可覆盖）。
- 配了 API key（``ANTHROPIC_API_KEY`` 等）。

**失败路径 fail loud**：CLI 不存在 / 非零退出 / 输出无 ``result`` 行 → raise。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import uuid
from typing import Any

from tests.spike_ask_user.backend import SubagentBackend, SubagentResult

logger = logging.getLogger(__name__)


class ClaudeCLINotAvailable(RuntimeError):
    """claude CLI 不在 PATH（或 ``ORCA_CLAUDE_CLI`` 指错）。fail loud。"""


class ClaudeCLIRunError(RuntimeError):
    """claude -p 子进程非零退出 / 超时 / 输出无法解析。fail loud。"""


# claude -p stream-json 的最终 result 行 type；其他行（assistant message / tool_call /
# thinking）是过程。driver 只关心最终消息。单值——直接字符串比较即可（无需 frozenset）。


class ClaudeCliBackend(SubagentBackend):
    """用 ``claude -p --output-format stream-json`` spawn、``--resume`` 续跑的后端。

    对 driver 暴露的接口与 Mock 一致（``spawn`` / ``resume`` + ``SubagentResult``）。
    task_id = claude session_id（``--session-id`` 注入 / ``--resume`` 复用）。

    usage / cost 经 ``result`` 行的 ``total_cost_usd`` / ``usage`` 字段透出到
    ``backend_specific``（driver / 测试可观测，但不解读）。
    """

    def __init__(
        self,
        *,
        claude_bin: str | None = None,
        timeout_s: float = 120.0,
        allowed_tools: tuple[str, ...] = ("Bash", "Read", "Write"),
        model: str | None = None,
        extra_args: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self.name = "claude-cli"  # 覆盖 ABC 类属性
        # ``ORCA_CLAUDE_CLI`` env 可覆盖二进制路径（与 tests/exec/claude 集成测试一致）。
        self._bin = claude_bin or os.environ.get("ORCA_CLAUDE_CLI", "claude")
        if shutil.which(self._bin) is None:
            raise ClaudeCLINotAvailable(
                f"claude CLI {self._bin!r} 不在 PATH；spike 真路径不可用。"
                f" ORCA_CLAUDE_CLI env 可覆盖，或退回 mock backend。"
            )
        self._timeout_s = timeout_s
        self._allowed_tools = allowed_tools
        self._model = model
        self._extra_args = extra_args
        # task_id -> call_index per task（给 SubagentResult.call_index 用）
        self._calls: dict[str, int] = {}

    def spawn(self, prompt: str) -> SubagentResult:
        # 自生成 session_id（claude CLI 要求 UUID 形态，带连字符）；
        # 真实 CC 路径是框架分配 agentId，这里我们 deterministic 派生等价句柄。
        session_id = str(uuid.uuid4())
        self._record_spawn(session_id)
        return self._run(session_id, prompt, resume=False)

    def resume(self, task_id: str, message: str) -> SubagentResult:
        if not self._task_known(task_id):
            raise KeyError(
                f"resume 收到 unknown task_id={task_id!r}；"
                f"已 spawn 的 task_ids={self.spawned_task_ids}"
            )
        self._record_resume(task_id)
        return self._run(task_id, message, resume=True)

    def _run(self, session_id: str, prompt: str, *, resume: bool) -> SubagentResult:
        # 用 ABC 的 _record_call 维护 calls_per_task / total_calls（与 mock 对齐）
        self._record_call(session_id)
        # SubagentResult.call_index 给 per-task 视角（_calls_per_task 已 +1，回退到本次索引）
        call_index = self._calls_per_task[session_id] - 1
        argv = self._build_argv(session_id, resume=resume)
        logger.info(
            "claude-backend %s session=%s call_index=%d resume=%s",
            self.name, session_id, call_index, resume,
        )
        try:
            # prompt 经 stdin 传（与 orca/orca/exec/claude/executor.py 一致）——
            # 避免 ``--allowed-tools`` variadic 把 prompt arg 误吞。
            proc = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise ClaudeCLIRunError(
                f"claude -p 超时（{self._timeout_s}s）session_id={session_id} resume={resume}"
            ) from e

        if proc.returncode != 0:
            raise ClaudeCLIRunError(
                f"claude -p 非零退出 rc={proc.returncode} session_id={session_id}"
                f" resume={resume}\n--- stderr ---\n{proc.stderr}"
            )

        output, backend_specific = self._parse_stream(proc.stdout, session_id)
        return SubagentResult(
            output=output,
            task_id=session_id,
            call_index=call_index,
            backend_specific=backend_specific,
        )

    def _build_argv(self, session_id: str, *, resume: bool) -> list[str]:
        argv: list[str] = [
            self._bin,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
        ]
        if resume:
            argv += ["--resume", session_id]
        else:
            # 首次 spawn：注入 session_id（resume 时 --resume 已带，不重复 --session-id）。
            argv += ["--session-id", session_id]
        if self._model:
            argv += ["--model", self._model]
        if self._allowed_tools:
            # 与 orca 一致：单 flag + 空格 join（spec §2.1）。prompt 走 stdin 不冲突。
            argv += ["--allowed-tools", " ".join(self._allowed_tools)]
        argv += list(self._extra_args)
        # 不 append prompt 到 argv——经 stdin 传（避免 variadic 误吞）。
        return argv

    def _parse_stream(self, stdout: str, session_id: str) -> tuple[str, dict[str, Any]]:
        """从 stream-json stdout 抽最终 result 文本。

        stream-json 每行一个 JSON 事件；我们关心 type=="result" 的最后一行（最终消息 +
        usage/cost）。其他行（assistant_text / tool_use / thinking）是过程，不进 driver。

        无 result 行 → fail loud（claude 异常退出但 rc=0 的兜底）。
        """
        result_obj: dict[str, Any] | None = None
        last_assistant_text: str = ""
        line_count = 0
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            line_count += 1
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                logger.debug(
                    "claude-backend session=%s 跳过非 JSON 行: %s",
                    session_id, line[:100],
                )
                continue
            ev_type = ev.get("type", "")
            if ev_type == "result":
                result_obj = ev
            elif ev_type in ("assistant", "assistant_message"):
                # 兜底：若 result 行缺，用最后一条 assistant 消息文本
                msg = ev.get("message") or {}
                content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            last_assistant_text = block.get("text", "")
                elif isinstance(content, str):
                    last_assistant_text = content

        if result_obj is None:
            raise ClaudeCLIRunError(
                f"claude -p stdout 无 result 行（session={session_id}，"
                f"解析了 {line_count} 行）；\n--- stdout tail ---\n{stdout[-400:]}"
            )

        # stream-json result 行的最终消息在 result 字段（claude CLI v1.x 约定）
        output = result_obj.get("result", "")
        if not isinstance(output, str) or not output:
            # 兜底用最后一条 assistant 文本
            output = last_assistant_text
        if not output:
            raise ClaudeCLIRunError(
                f"claude -p result 行无 result 文本（session={session_id}）；"
                f"result_obj keys={list(result_obj.keys())}"
            )

        backend_specific = {
            "backend": self.name,
            "session_id": session_id,
            "usage": result_obj.get("usage"),
            "cost_usd": result_obj.get("total_cost_usd"),
            "is_error": result_obj.get("is_error", False),
            "line_count": line_count,
        }
        return output, backend_specific

    # 诊断 helper（calls_per_task / total_calls）由 ABC 提供——基于 _record_call 维护。
