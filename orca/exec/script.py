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
import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from orca.chart._paths import artifacts_dir_for_run, chart_sock_path
from orca.exec.context import RunContext
from orca.exec.env import build_env_overlay
from orca.exec.error import ExecError
from orca.exec.interface import Executor
from orca.exec.registry import (
    ProcessRegistry,
    get_default_registry,
    spawn_kwargs_for_process_group,
)
from orca.exec.render import render_command
from orca.schema import Event, ScriptNode

logger = logging.getLogger(__name__)


class ScriptExecutor(Executor):
    """跑 shell 命令的 executor（SPEC §4.6 / §7.7）。

    phase-13 §11 #9「executor-agnostic」：与 ``ClaudeExecutor`` 对称，``__init__`` 接受可选
    ``runs_dir``（由 ``make_executor`` 从 orchestrator ``bus.tape.path.parent`` 推导透传）。
    spawn 子进程时构造 chart env overlay（4 个 ``ORCA_*``）合入 ``os.environ``，让 script
    内 ``orca.chart.render_chart`` 据身份路由推图到正确 run 的 ingestor。``runs_dir is None``
    或 resolved sock path 过长 → 退化为不注 chart env（向后兼容；script 端 §7.1 fail loud）。
    """

    def __init__(
        self,
        *,
        runs_dir: Path | None = None,
        registry: ProcessRegistry | None = None,
    ) -> None:
        # phase-13 §2：chart ingestor sock 父目录（``runs/<run_id>.sock`` 寻址用）。
        # None == 不注 ``ORCA_CHART_SOCK`` env（向后兼容，script 端 render_chart fail loud）。
        self._runs_dir = runs_dir
        # phase-11-process §1.2（ADR §4.7）：DI 注入 ProcessRegistry。
        # production 用 ``get_default_registry()``；测试可注入独立实例。
        self._registry: ProcessRegistry = registry or get_default_registry()

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

            # 4-5. subprocess + timeout（phase-13 §2：spawn 时注入 chart env overlay）
            # phase-11-process §1（铁律 1）+ §2.1（铁律 2）：spawn 必须经 registry.acquire
            # 登记 + 进程组隔离（start_new_session=True），cancel 时整组杀防孤儿。
            chart_sock = _resolve_chart_sock_path(self._runs_dir, ctx.run_id)
            # P8（plan 2026-07-21 §Phase 4-A）：注入产物权威目录，script 内 workflow 脚本据
            # ``$ORCA_ARTIFACTS_DIR`` 写产物（替代 workflow 自建 ``llm_artifacts/``）。
            artifacts_dir = _resolve_artifacts_dir(self._runs_dir, ctx.run_id)
            spawn_env = _build_spawn_env(
                node.name, ctx.run_id, session_id, chart_sock, artifacts_dir,
            )
            registry = self._registry
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=spawn_env,
                    # phase-11-process §2.1：进程组隔离（POSIX start_new_session /
                    # Windows CREATE_NEW_PROCESS_GROUP），cancel 时 killpg 整组杀。
                    **spawn_kwargs_for_process_group(),
                )
            except OSError as e:
                # shell 本身 spawn 失败（极少见，如系统资源耗尽）→ fail loud（spawn phase）
                raise ExecError(
                    phase="spawn",
                    message=f"无法 spawn shell 执行 command {cmd!r}：{e}",
                ) from e

            # phase-11-process §1：spawn 后立刻登记（铁律 1）。
            registry.acquire(
                proc, backend="script", run_id=ctx.run_id, node_id=node.name,
            )

            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=node.timeout
                )
            except asyncio.TimeoutError:
                # 超时：委托 registry.kill_one 三段式进程组 cancel（防 shell spawn 的
                # 孙子进程变孤儿）。grace_seconds=1.0（shell 命令通常无需长 grace）。
                try:
                    await asyncio.to_thread(
                        registry.kill_one, proc.pid, grace_seconds=1.0,
                    )
                except Exception:
                    logger.warning(
                        "registry.kill_one 异常（script 超时兜底吞，pid=%d）",
                        proc.pid, exc_info=True,
                    )
                raise ExecError(
                    phase="timeout",
                    message=(
                        f"script {node.name!r} 超时（timeout={node.timeout}s，"
                        f"command={cmd!r}）"
                    ),
                )
            finally:
                # phase-11-process §1：幂等 release，覆盖 normal / timeout / CancelledError
                # 三路径。kill_one 内部已 release 时此处为 no-op；CancelledError 路径
                # 若不 release，entry 会泄漏到 atexit 才清（不致命但不洁）。
                registry.release(proc.pid)

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
            err_data = {
                "kind": e.kind.value,
                "error_type": e.error_type,
                "message": e.message,
                "phase": e.phase,
            }
            yield _ev("node_failed", err_data)
            yield _ev("error", err_data)


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


# ── phase-13 §2 chart env overlay helpers（与 ClaudeExecutor 对称）────────────


def _resolve_chart_sock_path(runs_dir: Path | None, run_id: str) -> str:
    """phase-13 §2 / §7.7（2026-07-08 短路径化）：算 chart ingestor socket 绝对路径。

    与 ``orca.exec.claude.executor._resolve_chart_sock_path`` 逐字同语义（DRY：两处共实现
    会绕开 executor-agnostic 契约，SPEC §11 #9 要求两 executor 对称；保持函数级对称）。

    socket 走 ``<tmp>/orca-<sha1(run_id)[:10]>.sock``（``orca.chart._paths.chart_sock_path``），
    与 runs 目录解耦——规避深服务器路径致 ``sun_path`` 超限。两端（RunManager bind + 此处
    env 注入）同源，run_id 派生确定性短路径。

    - ``runs_dir is None`` → 返回空串（不注 ``ORCA_CHART_SOCK`` env，向后兼容；
      script 端 render_chart 会 fail loud 提示）。
    - 路径恒短（temp 目录 + 10 hex），不再有"过长退化"分支。
    """
    if runs_dir is None:
        return ""
    return str(chart_sock_path(run_id).resolve())


def _resolve_artifacts_dir(runs_dir: Path | None, run_id: str) -> str:
    """P8（plan 2026-07-21 §Phase 4-A）：算 workflow 产物权威目录绝对路径。

    与 ``orca.exec.claude.executor._resolve_artifacts_dir`` 逐字同语义（SPEC §11 #9 两 executor
    对称，DRY 函数级 mirror）。

    目录走 ``<runs_dir>/<run_id>/artifacts/``（``orca.chart._paths.artifacts_dir_for_run`` 派生），
    与 ``runs/<run_id>/`` per-run 资源根同源（``orca_env.sh`` / ``prompts/`` 等兄弟）。bootstrap
    ``mkdir -p`` 创建；workflow 脚本据 ``$ORCA_ARTIFACTS_DIR`` 写产物（替代 ``llm_artifacts/``）。

    - ``runs_dir is None`` → 返回空串（不注 ``ORCA_ARTIFACTS_DIR`` env，向后兼容；workflow
      脚本读 env 时自处理缺省）。
    - 非空 → ``resolve()`` 返绝对路径（与 ``ORCA_CHART_SOCK`` / ``ORCA_AGENT_RESOURCES`` 同
      resolve 契约：subagent 切目录后绝对路径仍正确）。
    """
    if runs_dir is None:
        return ""
    return str(artifacts_dir_for_run(runs_dir, run_id).resolve())


def _build_spawn_env(
    node: str, run_id: str, session_id: str, chart_sock: str, artifacts_dir: str = "",
) -> dict[str, str]:
    """phase-13 §2 + P8：构造 spawn 子进程 env（``os.environ`` + chart 路由 + 产物目录 overlay）。

    - 4 个 ORCA_* chart 路由全注：script 子进程内 ``orca.chart.render_chart`` 从 env 读身份路由。
    - chart_sock 空（runs_dir 缺 / sock path 过长）→ 仍注 run_id / node / session_id
      （其余 3 个非路径信息，script 端 §7.1 fail loud 提示缺 ``ORCA_CHART_SOCK``）。
    - artifacts_dir 空（runs_dir 缺）→ 不注 ``ORCA_ARTIFACTS_DIR``（向后兼容；workflow 脚本
      读 env 时需自处理缺省）。非空 → 注绝对路径（P8：workflow 据此写产物，替代自建目录）。
    - 空 prefix 元组（script executor 不绑特定 backend，不透传 ANTHROPIC_/CLAUDE_）：
      保持子进程继承 ``os.environ``（除 ORCA_* overlay 外），让 script 看到正常 shell env。
    """
    overlay = build_env_overlay(
        (),
        run_id=run_id,
        node=node,
        session_id=session_id,
        chart_sock=chart_sock,
        artifacts_dir=artifacts_dir,
    )
    return {**os.environ, **overlay}
