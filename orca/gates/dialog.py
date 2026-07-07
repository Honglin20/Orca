"""dialog.py —— DialogHandler：agent 跑完后的多轮追问（phase 11 §6）。

回答「workflow 跑完一个 agent node 后，用户想就它的 output 追问怎么办？」：用户按 ``d``
键 → DialogModal 弹出 → 用户每输一句，``DialogHandler.send_turn`` **重新 spawn 一个 claude -p**，
把「agent 之前的 output + 完整对话历史 + 这一句 user 输入」全部拼进 prompt，拿回 agent reply。
每轮结束 history 累积，下一轮再拼进去（``-p`` 路线无 in-process session，靠 prompt 拼历史模拟）。

**与 InterruptModal 的区别（SPEC §6.3）**：interrupt 是 node 跑**中**纠偏（SIGINT 杀 + 重 spawn
同 node）；dialog 是 node 跑**完后**追问（不杀原 agent——它已结束，重 spawn 一个**临时对话**
claude，仅用于回答用户的追问，不影响 DAG 推进）。

**3-method split（PLAN correction #7 / SPEC §6.2 deviation，Rule 7 裁定）**：
SPEC §6.2 伪代码写单一 ``run_dialog``（整体跑，内部循环）。但 Textual modal 需要在 agent reply
与下一轮 user 输入之间**交还控制给 UI**（单阻塞 ``run_dialog`` 无法在每轮 yield 给 UI 让用户
敲下一句）。故拆三方法：
  - ``start_dialog``：emit ``dialog_started`` + 初始化 per-dialog 状态（turn 计数、history 列表、
    初始 system context）。返回 ``dialog_id``（session 标识）。
  - ``send_turn``（async）：emit ``dialog_message(role=user)`` → 重 spawn claude（拼历史）→
    emit ``dialog_message(role=agent)`` → 返回 agent reply 文本。turn 计数 +1。
  - ``end_dialog``：emit ``dialog_ended{total_turns, conclusion}``。

per-dialog 状态在 handler 内 ``self._dialogs: dict[dialog_id, _DialogState]`` 维护。state 含
node / agent_output / turn_count / history（list[dict]）。``end_dialog`` 后清理（防泄漏）。

**事件归属（与 SPEC §9.6.4 validator 同裁定，Rule 7）**：本 handler **持 bus 且 emit**——
与 InterruptHandler 同 pattern（gates 层 emit 控制流事件，写 Tape）。这**不**违反铁律 2：
铁律 2 禁的是 ``exec/`` import 事件总线（executor 产 AsyncIterator，emit 归 orchestrator）；
``gates/`` 层本就是「控制流 + 事件」层（HumanGateHandler / InterruptHandler 都持 bus emit）。
dialog 与它们同层、同职责（用户交互控制流），持 bus emit 是 layer 一致的选择。

依赖单向（铁律 4）：本模块依赖 ``orca.exec.{runner, claude.result_extractor}``（spawn 复用，
DRY）+ ``orca.profiles.base``（CliProfile）+ ``orca.events.bus``（emit）+ ``orca.exec.context``
（RunContext 类型）+ ``orca.schema``（Event）。**不**依赖 ``orca.run`` / ``orca.iface``。
DialogModal（iface/）import 本模块是允许方向（iface → gates）。

Token 成本说明（SPEC §6.2）：每轮重 spawn + 拼全历史，token 成本随轮次线性增长。这是 ``-p``
路线的正确性代价（无 in-process session 保活）。用户在 DialogModal 看到「结束对话」按钮，
主动结束即停。不设硬上限轮次（YAGNI：用户自己知道何时问完）。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from orca.events.bus import EventBus
from orca.exec.env import build_env_overlay
from orca.exec.runner import CLIRunner, SpawnConfig

if TYPE_CHECKING:
    from orca.exec.context import RunContext
    from orca.profiles.base import CliProfile

logger = logging.getLogger(__name__)

# dialog_message 事件的 role 常量（SPEC §6：role ∈ {"user", "agent"}）。用常量防笔误
# （如写成 "assistant"），且让 emit 调用点意图清晰。dialog 只此两种 role。
_ROLE_USER = "user"
_ROLE_AGENT = "agent"

# Dialog 模式的 system prompt（参考 Conductor DIALOG_AGENT_SYSTEM_PROMPT，逐字改写适配 Orca）。
# 喂给重 spawn 的 claude，让它知道「你在帮用户就一个已完成的 agent output 做追问答疑」。
_DIALOG_SYSTEM_PROMPT = """你正在协助用户就一个已完成的 agent 产出进行追问对话。

## 背景
之前的 agent 已经完成了它的任务并产出了下面的 output。用户的后续问题都是围绕这个 output
展开的（理解、追问、修正建议、解释某个字段为什么是某个值等）。你的角色是**就这个 output
回答用户的追问**，不是重新执行原任务。

## 之前的 agent output
{agent_output}

## 对话历史
{history}

## 用户最新的问题
{user_text}

## 你的任务
基于上面的 agent output 与对话历史，**如实、简洁**地回答用户最新的问题。如果 output 里没有
用户问的信息，明确说明（不要编造）。如果用户问的是修正建议，给出具体可执行的建议。
"""

# dialog_id 的 dialog 状态（handler 内 per-dialog 维护，不暴露给外部）。
# history 是 list[dict]（每条 {"role","text","turn"}），send_turn 累积、end_dialog 后随 state 一起丢。


@dataclass
class _DialogState:
    """单个 dialog 会话的运行时状态（handler 内部，不导出）。"""

    node: str
    agent_output: Any
    run_id: str
    profile: CliProfile
    turn_count: int = 0
    # 历史按时间序累积（user 与 agent 交替）。拼 prompt 时按序回放。
    history: list[dict[str, Any]] = field(default_factory=list)


class DialogHandler:
    """agent 跑完后多轮追问（重 spawn claude 拼历史，SPEC §6）。

    3-method split（见模块 docstring 的裁定）：``start_dialog`` / ``send_turn`` / ``end_dialog``。
    每轮 send_turn 重 spawn 一个临时 claude -p，prompt 含 agent_output + 完整历史 + 本轮 user 输入。

    与 InterruptHandler 的关系：独立类（语义不同——interrupt 是控制流纠偏，dialog 是 post-run
    追问）。**不**继承 BroadcasterMixin：dialog 是单壳 CLI 交互（一次一个用户），无需 fan-out
    广播（多壳 dialog 留后续 web phase）。持 bus 仅为 emit dialog_* 事件写 Tape（可观测）。
    """

    def __init__(self, profile: CliProfile, bus: EventBus) -> None:
        self._profile = profile
        self._bus = bus
        # dialog_id → state。end_dialog 后 pop 清理。理论上同时只有一个 dialog（单壳 CLI），
        # 但 dict 容纳多 dialog_id 不增加复杂度（YAGNI：不强制单例）。
        self._dialogs: dict[str, _DialogState] = {}

    # ── 生命周期三方法 ──────────────────────────────────────────────────────

    async def start_dialog(
        self, node: str, agent_output: Any, ctx: RunContext,
    ) -> str:
        """进入 dialog 模式：emit ``dialog_started`` + 初始化 per-dialog 状态。

        Args:
            node: 用户追问的目标 node 名（已完成 agent node）。
            agent_output: 该 node 的产出（任意可 JSON 序列化对象 / 自由文本）。
            ctx: 当前 RunContext（仅取 ``run_id`` 给 spawn env overlay 路由用）。dialog 的真相
                源在 tape 的 ``dialog_message`` 事件，**不在 ctx.dialog_history**（见 RunContext
                字段 docstring 的真相源裁定）——本 handler 不 mutate ctx（frozen）。

        Returns:
            dialog_id（uuid hex，标识本次 dialog 会话，send_turn/end_dialog 用它定位 state）。

        **不阻塞**：仅 emit（写 Tape）+ 建 state，不 spawn claude。后续 send_turn 由 modal 逐轮触发。
        ``emit`` 是 async（写 Tape + fan-out），故本方法 async——但语义上「不阻塞等 claude」。
        """
        dialog_id = uuid.uuid4().hex
        self._dialogs[dialog_id] = _DialogState(
            node=node,
            agent_output=agent_output,
            run_id=ctx.run_id,
            profile=self._profile,
        )
        # initial_prompt 是「开场 prompt」——dialog 第一轮 user 还没问，这里记 system context
        # 的摘要（agent_output 前 200 字符）让 tape 可观测「追问围绕什么展开」。
        try:
            output_preview = json.dumps(agent_output, ensure_ascii=False)[:200]
        except (TypeError, ValueError):
            output_preview = str(agent_output)[:200]
        await self._bus.emit("dialog_started", {
            "node": node,
            "session_id": dialog_id,  # dialog_id 即 session 标识（与 claude session 同概念粒度）
            "initial_prompt": output_preview,
        }, session_id=dialog_id)
        return dialog_id

    async def send_turn(
        self, dialog_id: str, user_text: str, ctx: RunContext,
    ) -> str:
        """处理一轮 user 追问：emit user message → 重 spawn claude → emit agent message。

        Args:
            dialog_id: ``start_dialog`` 返回的 id。未知 id → raise（fail loud，SPEC §6 / 铁律 4）。
            user_text: 用户这一轮的问题。
            ctx: 当前 RunContext（透传 run_id 给 spawn env，不 mutate）。

        Returns:
            agent reply 文本（claude 这一轮的 result_text）。

        **fail loud**：spawn 失败（claude binary 不存在 / 子进程崩）→ raise，不静默丢一轮。
        DialogModal 应捕获并在 UI 显示错误（让用户知道这轮没答上，可重试或结束）。
        """
        state = self._dialogs.get(dialog_id)
        if state is None:
            raise KeyError(
                f"dialog_id {dialog_id!r} 未找到（未 start 或已 end）——属调用方 bug",
            )

        turn = state.turn_count + 1

        # 1. emit user message（先记用户问了什么，再 spawn——即使 spawn 失败，user 话已在 tape）
        await self._bus.emit("dialog_message", {
            "role": _ROLE_USER, "text": user_text, "turn": turn,
        }, node=state.node, session_id=dialog_id)

        # 2. 组 prompt（agent_output + 完整历史 + 本轮 user 输入），重 spawn claude
        prompt = _build_dialog_prompt(state.agent_output, state.history, user_text)
        cfg = _build_dialog_spawn_config(state.profile, prompt)

        result_holder: dict[str, Any] = {
            "result_text": None, "is_error": False, "api_error_status": None,
        }

        def on_result(
            raw_result: str, usage: dict, cost: float, is_error: bool,
            api_error_status: int | None = None,
        ) -> None:
            result_holder["result_text"] = raw_result
            result_holder["is_error"] = is_error
            result_holder["api_error_status"] = api_error_status

        runner = CLIRunner(cfg, on_result=on_result)
        # 流式丢弃（dialog reply 不需 token 级流进 tape——dialog_message 事件记最终文本即可）。
        # spawn 失败 → stream() 内部会正常结束（属性已填），由下方判错 raise；二进制不存在等
        # create_subprocess_exec 异常会冒泡（fail loud，modal 捕获显示）。
        async for _line in runner.stream():
            pass

        # 3. 判错（fail loud）：非零退出 / result.is_error / 无 result → raise
        if runner.exit_code != 0 or result_holder["is_error"]:
            raise RuntimeError(
                f"dialog claude spawn 失败：exit_code={runner.exit_code}, "
                f"is_error={result_holder['is_error']}, "
                f"api_error_status={result_holder['api_error_status']}, "
                f"result={(result_holder['result_text'] or '')[:300]}, "
                f"stderr 末尾={runner.stderr[-300:]}",
            )
        result_text = result_holder["result_text"]
        if result_text is None:
            raise RuntimeError(
                f"dialog claude exit 0 但无 result 事件；stderr={runner.stderr[-300:]}",
            )

        # 4. emit agent message + 累积进 state.history（下一轮拼 prompt 用）
        await self._bus.emit("dialog_message", {
            "role": _ROLE_AGENT, "text": result_text, "turn": turn,
        }, node=state.node, session_id=dialog_id)
        state.history.append({"role": _ROLE_USER, "text": user_text, "turn": turn})
        state.history.append({"role": _ROLE_AGENT, "text": result_text, "turn": turn})
        state.turn_count = turn

        return result_text

    async def end_dialog(self, dialog_id: str, ctx: RunContext) -> None:
        """退出 dialog 模式：emit ``dialog_ended`` + 清理 per-dialog 状态。

        conclusion 是「用户主动结束」（无 LLM 判定——dialog 不自动判何时结束，由用户按按钮）。
        total_turns 即 ``state.turn_count``（send_turn 累积的轮次数）。

        未知 dialog_id → no-op + warning（end 可能被调多次 / start 未调，幂等优于 raise——
        end 是清理路径，不该因状态不一致阻塞 UI 退出）。
        """
        state = self._dialogs.pop(dialog_id, None)
        if state is None:
            logger.warning("end_dialog: dialog_id %r 未找到（已 end 或未 start），no-op", dialog_id)
            return
        await self._bus.emit("dialog_ended", {
            "node": state.node,
            "total_turns": state.turn_count,
            "conclusion": "user_ended",  # 用户主动结束（无自动判定，SPEC §6.2）
        }, node=state.node, session_id=dialog_id)


# ── spawn 辅助（DRY：与 validator._build_validator_spawn_config 同 pattern）──────────


def _build_dialog_prompt(
    agent_output: Any, history: list[dict[str, Any]], user_text: str,
) -> str:
    """组 dialog 这轮的 prompt：agent_output + 完整历史 + 本轮 user 输入。

    history 按时间序回放（user / agent 交替），拼成可读的对话 transcript。用 ``replace`` 注入
    避免 ``.format`` 与 output 自身 ``{}`` 冲突（与 validator prompt 同款处理）。
    """
    try:
        output_json = json.dumps(agent_output, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        # output 不可 JSON 序列化（自由文本 agent output）→ 原样喂入。
        output_json = str(agent_output)

    if history:
        lines = []
        for h in history:
            lines.append(f"[{h['role']}] (turn {h['turn']}): {h['text']}")
        history_text = "\n".join(lines)
    else:
        history_text = "（首轮，尚无历史）"

    return _DIALOG_SYSTEM_PROMPT.replace("{agent_output}", output_json).replace(
        "{history}", history_text,
    ).replace("{user_text}", user_text)


def _build_dialog_spawn_config(profile: CliProfile, prompt: str) -> SpawnConfig:
    """构造 dialog 这轮的 SpawnConfig（复用 profile，DRY —— 与 validator 同款，review C5）。

    与 ``ClaudeExecutor._build_spawn_config`` / ``validator._build_validator_spawn_config`` 共享
    profile 来源（cli_path / flags / env_overlay），保证 ccr 中转（``ORCA_CLAUDE_CLI=ccr code``）
    对 dialog 同样生效。

    argv 区别：dialog **不**加 ``--allowed-tools ""``——dialog agent 允许用工具（Read/Grep 等）
    去查 output 涉及的文件 / 代码，回答用户的追问（如「weights_path 这个文件存在吗」）。
    也不加 ``--mcp-config``（dialog 不需要 ask_user——用户已在 modal 里直接对话）。
    """
    return SpawnConfig(
        cli_path=profile.resolve_cli_path(),  # env > default，不硬编码 "claude"
        flags=profile.resolve_flags(),  # env > config > default（2026-07-07 executor CLI 扩展）
        extra_args=[],  # 不限制工具（dialog agent 可 Read/Grep 调查 output）
        mcp_flag_args=[],  # dialog 不挂 MCP（无 ask_user 需求）
        prompt=prompt,
        prompt_channel=profile.resolve_prompt_channel(),  # env > config > default
        env_overlay=build_env_overlay(profile.env_overlay_prefixes),
        timeout=None,  # dialog 由用户主动结束，不设总墙钟（单轮停滞由 CLIRunner 行间 timeout 管）
    )
