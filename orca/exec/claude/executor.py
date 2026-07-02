"""executor.py —— ClaudeExecutor（claude -p 子进程路线，SPEC §4.3）。

回答「怎么 spawn claude、翻译它的流、产出 Orca 事件流？」：CLIRunner（通用子进程）+
profile.translator（claude 协议，profiles 层）+ extract_and_validate（结构化提取）。

执行流程（SPEC §4.3）：
  1. ``session_id = uuid4().hex``（入口生成，全程复用，铁律 5）
  2. ``yield node_started``
  3. ``prompt = render_prompt(node, ctx)``
  4. ``cfg = _build_spawn_config(node, profile, prompt, agent_tools_server, run_id, session_id)``
     （argv 动态拼：--model / --allowed-tools / --mcp-config；flags 来自 profile）
  5. ``on_result`` 钩子收集 result 文本 / usage / cost（CLIRunner 检测 result 行时回调）
  6. ``runner = CLIRunner(cfg, on_result)``
  7. ``async for line in runner.stream(): for ev in profile.translator(line, session_id): yield ev``
  8. 有序互斥判定（SPEC §2.4）：timed_out → exit_code!=0 → result.is_error → 无 result
     → 各自 raise ExecError
  9. ``output = extract_and_validate(result_text, node.output_schema)``（SPEC §2.7）
  10. ``yield agent_usage``（若 on_result 收到 usage，补一个汇总——translator 已发过，
      但 executor 视角的 node_completed.data 也带 usage，供 orchestrator 聚合）
  11. ``yield node_completed(node, session_id, {output, elapsed, usage?})``
  12. ``except ExecError: yield node_failed + error``（fail loud，铁律 4）

argv 构造（SPEC §2.1，重写不迁移）：
  - flags 来自 ``profile.flags``（``-p --output-format stream-json ...``）
  - ``--model <m>``：仅当 ``node.model`` 显式指定
  - ``--allowed-tools "<t1 t2 ...>"``：仅当 ``node.tools`` 非 None（None=全开，不传该 flag）；
    **单 flag + 空格 join**（非 variadic，SPEC §2.1）
  - phase 11 §5.4：``--mcp-config <path>`` 仅当 ``agent_tools_server`` 注入（ask_user 挂载）；
    同时把 ``mcp__orca-agent-tools__ask_user`` 加进 ``--allowed-tools``（spike 验证：claude -p
    默认不给 MCP 工具授权，必须显式 allowed-tools 才能调 ask_user）。
  - ``--append-system-prompt "<agent md>"``：仅当加载了 agents/<name>.md（本阶段 prompt
    内联或 md 内容统一进 stdin，不拆 system-prompt；保留接口位）

错误映射（SPEC §6 / §2.4 有序互斥）：见 ``orca/exec/error.py``。

依赖单向：本模块依赖 ``orca.exec.{interface,context,error,render,runner}`` +
``orca.exec.claude.result_extractor`` + ``orca.schema`` + ``orca.profiles``（profile 类型）。
``AgentToolsMcpServer`` 仅 TYPE_CHECKING（避免 runtime 环依赖 exec/mcp_tools → gates → events）。
不依赖 events.bus/run/compile/iface。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from orca.exec.claude.result_extractor import extract_and_validate
from orca.exec.context import RunContext
from orca.exec.env import build_env_overlay
from orca.exec.error import ExecError
from orca.exec.interface import Executor
from orca.exec.render import render_prompt
from orca.exec.runner import CLIRunner, SpawnConfig
from orca.profiles.base import CliProfile
from orca.schema import AgentNode, Event

if TYPE_CHECKING:
    from orca.exec.mcp_tools.server import AgentToolsMcpServer

logger = logging.getLogger(__name__)

# phase 11 §5.4：claude -p 默认不给 MCP 工具授权。ask_user 工具的完整 allowed-tools 名
# （``mcp__<server>__<tool>``）。注入 agent_tools_server 时自动加进 --allowed-tools。
_ASK_USER_TOOL_NAME = "mcp__orca-agent-tools__ask_user"


class ClaudeExecutor(Executor):
    """claude -p 子进程路线的 executor（SPEC §4.3）。

    持有一个 ``CliProfile``（描述如何 spawn / 解析 claude）。profile.translator 是纯函数
    （profiles 层），executor 调它把 stream-json 行翻译成 Event。

    phase 11 §5.4：可选 ``agent_tools_server``——非 None 时 spawn claude 带
    ``--mcp-config``（暴露 ask_user），并在 spawn 成功后 ``registry.register`` 登记
    session_id → (run_id, node) 路由（HumanGateHandler 把 gate 答案送回正确 agent）。
    None == 既有行为（向后兼容）。
    """

    def __init__(
        self,
        profile: CliProfile,
        agent_tools_server: AgentToolsMcpServer | None = None,
    ) -> None:
        self.profile = profile
        self._agent_tools_server = agent_tools_server

    async def exec(self, node: AgentNode, ctx: RunContext) -> AsyncIterator[Event]:
        """执行 agent node，产出完整生命周期事件流（SPEC §4.3）。

        见模块 docstring 的 12 步流程。失败路径 emit ``node_failed`` + ``error``（fail loud）。
        """
        # 决策 2 / 铁律 5：session_id 入口生成，全程复用；seq=0 占位由 orchestrator 重分配。
        session_id = uuid.uuid4().hex

        def _ev(event_type: str, data: dict[str, Any], *, n: str | None = None) -> Event:
            return Event(
                seq=0,  # 占位：executor 不写 tape，phase 5 tape.append 重分配全局 seq（决策 2）
                type=event_type,  # type: ignore[arg-type]
                timestamp=time.time(),
                node=n if n is not None else node.name,
                session_id=session_id,
                data=data,
            )

        # 1-2. node_started
        yield _ev("node_started", {"executor": self.profile.name, "kind": "agent"})

        try:
            # 3. 渲染 prompt（render_prompt：内联或 agents/<name>.md，Jinja2 ctx）
            prompt = render_prompt(node, ctx)
            # phase 11 §5.3 / §5.6（决策 D4）：ask_user 挂载时，prompt 末尾拼一条 instruction，
            # 告诉 claude 调 ask_user 必带路由参 ``orca_run_id`` / ``orca_node``。确定性路由
            # 不依赖 MCP session（claude -p 不主动报）。无 server → 不动 prompt（向后兼容）。
            if self._agent_tools_server is not None:
                prompt = _append_ask_user_instruction(prompt, ctx.run_id, node.name)

            # phase 11 §2.2 / §10.2 item3 B5：spawn 前发 prompt_rendered 让 guidance 注入
            # 可观测（preview=末尾 ~200 字符，含 [User Guidance] 段时直观可见）。
            yield _ev("prompt_rendered", {
                "node": node.name,
                "session_id": session_id,
                "preview": prompt[-200:],
            })

            # 4. 构造 spawn config（argv 动态拼 + env overlay + 可选 mcp-config）
            cfg = _build_spawn_config(
                node, self.profile, prompt, self._agent_tools_server,
                run_id=ctx.run_id, session_id=session_id,
            )

            # phase 11 §5.5（review B2）：register debt —— spawn 前（写 mcp-config 之后）
            # 把 (session_id → run_id, node.name) 登记进 registry，让 HumanGateHandler 能
            # 把 gate 答案回流到正确 agent。session_id 即本 executor 入口生成的 uuid（全程
            # 复用），与 mcp-config 文件名 / ask_user 的 session_id 派生约定一致。
            if self._agent_tools_server is not None:
                self._agent_tools_server.register_session(
                    session_id=session_id, run_id=ctx.run_id, node=node.name,
                )

            # 5. on_result 钩子收集 result 文本 / usage / cost / API 错误码
            result_holder: dict[str, Any] = {
                "result_text": None,
                "usage": None,
                "cost": 0.0,
                "is_error": False,
                "api_error_status": None,  # claude result 行顶层 HTTP 错误码（如 529）
            }

            def on_result(
                raw_result: str, usage: dict, cost: float, is_error: bool,
                api_error_status: int | None = None,
            ) -> None:
                result_holder["result_text"] = raw_result
                result_holder["usage"] = usage
                result_holder["cost"] = cost
                result_holder["is_error"] = is_error
                result_holder["api_error_status"] = api_error_status

            # 6-7. CLIRunner 跑子进程，逐行喂 translator
            runner = CLIRunner(cfg, on_result=on_result)
            async for line in runner.stream():
                for ev in self.profile.translator(line, session_id):
                    # translator 是纯函数，只设 session_id（SPEC §3.2）；node 字段由 executor
                    # 富化（SPEC §4.2「所有事件顶层带 node + session_id」）。translator 不知
                    # node 名（纯函数无 ctx），故此处补 node=node.name。
                    yield ev.model_copy(update={"node": node.name})

            # phase 11 §4.2：用户 SIGINT 中断优先判定（在 timed_out / exit_code 之前）。
            # was_interrupted=True 表示用户 Ctrl+G 主动中断（非子进程崩）→ 不当 error：
            # emit node_failed{was_interrupted:true} 让 orchestrator 在 node 边界决定
            # continue/skip/abort，retry 也据此短路（SPEC §9.5.2 error_type 对齐表）。
            if runner.was_interrupted:
                yield _ev("node_failed", {
                    "error_type": "Interrupted",
                    "message": "claude 子进程被用户 SIGINT 中断（Ctrl+G）",
                    "phase": "interrupted",
                    "was_interrupted": True,
                })
                return

            # 错误诊断摘要（SPEC §6 可观测性）：claude 把 API 错误（如 529 overloaded）写在
            # stdout 的 result 行（is_error + api_error_status + result 文本），**不在 stderr**。
            # 故 node_failed 的 message 必须带上 result 诊断，否则 stderr 空时（典型 529 早退
            # 场景）用户完全看不到失败原因。DRY：4 个 ExecError 分支共用此摘要。
            def _result_diag() -> str:
                parts: list[str] = []
                status = result_holder.get("api_error_status")
                if status is not None:
                    parts.append(f"HTTP {status}")
                if result_holder.get("is_error"):
                    parts.append("result.is_error=true")
                rt = result_holder.get("result_text")
                if rt:
                    parts.append(f"result={str(rt)[:300]!r}")
                if runner.stderr:
                    parts.append(f"stderr末尾={runner.stderr[-300:]!r}")
                return "；".join(parts) if parts else "（无 stderr / result 详情）"

            # 8. 有序互斥判定（SPEC §2.4）：timed_out → exit_code → is_error → no_result
            if runner.timed_out:
                raise ExecError(
                    phase="timeout",
                    message=(
                        f"claude 子进程超时（timeout={cfg.timeout}s，elapsed="
                        f"{runner.elapsed:.1f}s）；{_result_diag()}"
                    ),
                )
            if runner.exit_code != 0:
                raise ExecError(
                    phase="spawn",
                    message=(
                        f"claude 子进程非零退出（exit_code={runner.exit_code}）；{_result_diag()}"
                    ),
                )
            # result.is_error=true（SPEC §2.4 第 3 项 / §6 phase=stream）：claude 自己报错
            # （如 API error）。on_result 透传 is_error + api_error_status，executor 据此
            # 走 stream 错误路径并把 HTTP 错误码带进 node_failed。
            if result_holder["is_error"]:
                raise ExecError(
                    phase="stream",
                    message=f"claude 流报错（result.is_error=true）；{_result_diag()}",
                )
            if result_holder["result_text"] is None:
                raise ExecError(
                    phase="result_parse",
                    message=(
                        f"claude exit 0 但流里无 result 事件（result_text 缺失）；{_result_diag()}"
                    ),
                )

            # 9. 结构化提取（SPEC §2.7）
            output = extract_and_validate(result_holder["result_text"], node.output_schema)

            # 10-11. node_completed（带 output / elapsed / usage?）
            completed_data: dict[str, Any] = {
                "output": output,
                "elapsed": runner.elapsed,
            }
            if result_holder["usage"] is not None:
                completed_data["usage"] = _normalize_usage(
                    result_holder["usage"], result_holder["cost"]
                )
            yield _ev("node_completed", completed_data)

        except ExecError as e:
            # 12. fail loud：node_failed + error 双发（SPEC §6 / 铁律 4）
            err_data = {
                "error_type": e.error_type,
                "message": e.message,
                "phase": e.phase,
            }
            yield _ev("node_failed", err_data)
            yield _ev("error", err_data)


# ── helpers ──────────────────────────────────────────────────────────────────


def _build_spawn_config(
    node: AgentNode,
    profile: CliProfile,
    prompt: str,
    agent_tools_server: AgentToolsMcpServer | None = None,
    *,
    run_id: str = "",
    session_id: str = "",
) -> SpawnConfig:
    """按 SPEC §2.1 拼动态 argv + env overlay + 可选 --mcp-config（phase 11 §5.4）。

    - ``--model <m>``：仅当 ``node.model`` 显式指定（None 不传）
    - ``--allowed-tools "<t1 t2 ...>"``：``node.tools`` 非 None 时取其声明；
      ``node.tools is None``（全开）且注入 agent_tools_server 时，把 ask_user 工具名
      显式加入（claude -p 默认不给 MCP 工具授权，spike 验证必须显式 allowed-tools）。
      若 ``node.tools`` 非 None（用户已声明白名单），把 ask_user 工具名 append 进去
      （否则用户的白名单会把 ask_user 屏蔽掉）。
    - phase 11 §5.4：``--mcp-config <path>`` 仅当 ``agent_tools_server`` 注入；
      ``path`` 由 ``agent_tools_server.write_config`` 产出（SSE url 指向 loopback port）。
    - env overlay：profile 声明的前缀（claude = ANTHROPIC_ / CLAUDE_）对应 os.environ 子集
    """
    # ── 1. tools：仅当注入 agent_tools_server 时合并 ask_user 权限 ──────────
    # spike 验证：claude -p 默认不给 MCP 工具授权，必须显式 ``--allowed-tools`` 才能调
    # ask_user。无 server 时不动 tools（保持 SPEC §2.1 既有行为：None=全开不传 flag，
    # 非 None=声明白名单），向后兼容。
    extra_args: list[str] = []
    if node.model is not None:
        extra_args.extend(["--model", node.model])
    if agent_tools_server is not None:
        if node.tools is None:
            # 全开 → 仅需显式声明 ask_user（其余 claude 内置工具默认可用）
            tools_list: list[str] = [_ASK_USER_TOOL_NAME]
        else:
            # 用户声明了白名单 → append ask_user（若未在内），否则白名单会屏蔽 ask_user
            tools_list = list(node.tools)
            if _ASK_USER_TOOL_NAME not in tools_list:
                tools_list.append(_ASK_USER_TOOL_NAME)
        extra_args.extend(["--allowed-tools", " ".join(tools_list)])
    else:
        # 既有行为（SPEC §2.1）：None=全开不传 flag；非 None=声明白名单单 flag + 空格 join
        if node.tools is not None:
            extra_args.extend(["--allowed-tools", " ".join(node.tools)])

    # ── 2. mcp-config（phase 11 §5.4）：注入 server 时写 SSE config 文件 ──
    mcp_flag_args: list[str] = []
    if agent_tools_server is not None:
        if not run_id or not session_id:
            # 编程错误：agent_tools_server 注入了但 run_id/session_id 没带 → fail loud。
            raise RuntimeError(
                "_build_spawn_config: agent_tools_server 注入但 run_id/session_id 为空"
                "（无法写 mcp-config）"
            )
        config_path = agent_tools_server.write_config(
            session_id=session_id, run_id=run_id, node=node.name,
        )
        mcp_flag_args = ["--mcp-config", str(config_path)]

    env_overlay = build_env_overlay(profile.env_overlay_prefixes)
    cli_path = profile.resolve_cli_path()  # env > default，运行时读（SPEC §2.6）

    return SpawnConfig(
        cli_path=cli_path,
        flags=profile.flags,
        extra_args=extra_args,
        mcp_flag_args=mcp_flag_args,
        prompt=prompt,
        prompt_channel=profile.prompt_channel,
        env_overlay=env_overlay,
        timeout=None,  # 本阶段不做单 node 超时（retry/interrupt 归 phase 5，SPEC §5）
    )


def _normalize_usage(usage: dict, cost: float) -> dict[str, Any]:
    """把 claude result.usage 归一成 agent_usage 的 payload（SPEC §3.3）。

    cache_tokens = usage.cache_read_input_tokens；cost_usd = 顶层 total_cost_usd。
    """
    return {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_tokens": usage.get("cache_read_input_tokens", 0),
        "cost_usd": cost,
    }


def _append_ask_user_instruction(prompt: str, run_id: str, node: str) -> str:
    """phase 11 §5.3 / §5.6：prompt 末尾拼 ask_user 调用 instruction（确定性路由）。

    告诉 claude：要问用户就调 ``ask_user`` 工具，**必须**带 ``orca_run_id=<run_id>`` /
    ``orca_node=<node>``（路由参，缺失则工具抛）。确定性路由不依赖 MCP session（claude -p
    不主动报）。把具体值填进 instruction，claude 直接复制即可——降低它「自作主张省略参」
    的概率（spike 验证：路由参必填，否则 fail loud）。
    """
    return (
        prompt.rstrip()
        + "\n\n[Orca ask_user tool]\n"
        + "If you need to ask the user a question, call the `ask_user` MCP tool. "
        + "You MUST pass these two routing parameters exactly:\n"
        + f"  - orca_run_id: {run_id}\n"
        + f"  - orca_node: {node}\n"
        + "Call signature: ask_user(prompt=<your question>, options=[...optional...], "
        + f"orca_run_id={run_id!r}, orca_node={node!r})."
    )
