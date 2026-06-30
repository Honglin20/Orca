"""script.py —— ScriptExecutor（确定性 shell 命令节点，SPEC §4.6）。

回答「怎么跑一个 shell 命令节点？」：subprocess + Jinja2 渲染 command + parse_json 降级。

执行流程（SPEC §4.6 / 计划 D.1）：
  1. ``session_id = uuid4().hex``（入口生成，铁律 5）
  2. ``yield node_started``
  3. ``cmd = render_command(node.command, ctx)``（Jinja2 渲染，失败 → ExecError(phase=render)）
  4. ``proc = await create_subprocess_shell(cmd, stdout=PIPE, stderr=PIPE)``
  5. ``stdout, stderr = await asyncio.wait_for(proc.communicate(), node.timeout)``
     （timeout=None 不限；超时 → ExecError(phase=timeout)）
  6. ``output = {stdout, stderr, exit_code}``；``node.parse_json`` → 额外 ``output["json"]``
     （解析失败 → None，**不 fail loud**，降级）
  7. **非零退出码不 fail loud**（业务语义，由路由判断，见 examples/nas.yaml evaluator 的
     ``output.exit_code == 0``）；正常 ``yield node_completed``

关键约束（SPEC §4.6 / §7.7）：
  - 非零退出码是**业务结果**（脚本可能是「检查」类，非零=条件不满足），不 fail loud。
  - **timeout 必须 fail loud**（emit node_failed + phase=timeout）。
  - parse_json 解析失败 → ``output["json"]=None``（降级，不阻断；SPEC §4.6）。

依赖单向：本模块依赖 ``orca.exec.{interface,context,error,render}`` + ``orca.schema``；
不依赖 events.bus/run/compile。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from orca.exec.context import RunContext
from orca.exec.error import ExecError
from orca.exec.interface import Executor
from orca.exec.render import render_command
from orca.schema import Event, ScriptNode

logger = logging.getLogger(__name__)


class ScriptExecutor(Executor):
    """跑 shell 命令的 executor（SPEC §4.6 / §7.7）。"""

    async def exec(self, node: ScriptNode, ctx: RunContext) -> AsyncIterator[Event]:
        session_id = uuid.uuid4().hex
        start = time.monotonic()

        def _ev(event_type: str, data: dict[str, Any]) -> Event:
            return Event(
                seq=0,  # 占位：orchestrator 在 tape.append 时重分配（决策 2）
                type=event_type,  # type: ignore[arg-type]
                timestamp=time.time(),
                node=node.name,
                session_id=session_id,
                data=data,
            )

        yield _ev("node_started", {"kind": "script", "command": node.command})

        try:
            # 3. 渲染 command（Jinja2 失败 → ExecError(phase=render)）
            cmd = render_command(node.command, ctx)

            # 4-5. subprocess + timeout
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as e:
                # shell 本身 spawn 失败（极少见，如系统资源耗尽）→ fail loud（spawn phase）
                raise ExecError(
                    phase="spawn",
                    message=f"无法 spawn shell 执行 command {cmd!r}：{e}",
                ) from e

            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=node.timeout
                )
            except asyncio.TimeoutError:
                # 超时：kill 子进程（SIGKILL 兜底，shell 命令不指望 grace）
                _kill_proc(proc)
                raise ExecError(
                    phase="timeout",
                    message=(
                        f"script {node.name!r} 超时（timeout={node.timeout}s，"
                        f"command={cmd!r}）"
                    ),
                )

            # 6. output 组装（非零退出码不 fail loud，SPEC §4.6）
            output: dict[str, Any] = {
                "stdout": stdout_b.decode("utf-8", errors="replace"),
                "stderr": stderr_b.decode("utf-8", errors="replace"),
                "exit_code": proc.returncode if proc.returncode is not None else -1,
            }
            if node.parse_json:
                # parse_json 失败 → None（降级，不 fail loud，SPEC §4.6 / §7.7）
                output["json"] = _try_parse_json(output["stdout"])

            elapsed = time.monotonic() - start
            yield _ev("node_completed", {"output": output, "elapsed": elapsed})

        except ExecError as e:
            elapsed = time.monotonic() - start
            err_data = {"error_type": e.error_type, "message": e.message, "phase": e.phase}
            yield _ev("node_failed", err_data)
            yield _ev("error", err_data)


def _kill_proc(proc: asyncio.subprocess.Process) -> None:
    """超时后 kill 子进程（SIGKILL 兜底；shell 命令通常无需 grace）。"""
    try:
        proc.kill()
    except ProcessLookupError:
        pass  # 已退出


def _try_parse_json(text: str) -> Any:
    """尝试解析 stdout 为 JSON，失败返回 None（SPEC §4.6 降级语义，不 fail loud）。

    strip 前后空白后 parse；解析失败记 debug log（不阻断，业务可经 output.json is None 判断）。
    """
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        logger.debug(
            "script parse_json 失败（output.json=None，降级不阻断）：前 200 字符=%r",
            stripped[:200],
        )
        return None
